# Roadmap: fast, user-friendly Bitopi Knowledge Assistant at scale

The speed / UX / scale plan (Phases A–E). This file is the design; **what is done and what is next
lives in `docs/PROGRESS.md`**, and each release is recorded in `CHANGELOG.md`. All paths below are
relative to the repo root.

## Context

The assistant works (M0–M6 done, 99 tests, 83% correctness on the 62-case golden set) but is slow and fragile. The user now wants it to scale to **60K+ pages of PDFs** and a **huge SQL Server database with full read-only SELECT permission (no DDL)**, running on the **CPU-only Docker box**, staying on the **Gemini free tier** (500 req/day/model). Goal: fastest possible answers and a friendlier UI.

Where the time goes today (from `logs/calls.csv`, `logs/sql.csv`, and code):

| Stage | Today | Cause |
|---|---|---|
| CPU reranker, 20 candidates × max_length 1024 | **30–60 s** (~90% of wall time) | `embed.py Reranker` |
| First question after start | +60–120 s | 6.5 GB models load lazily inside the spinner |
| LLM calls: route / rewrite / answer / SQL | 1.3 / 1.4 / 2.9 / 3.7 s each, 3–4 per question | none skipped; thinking_budget 1024 on answers |
| Quota exhausted (429) | minutes of hang, then raw traceback | `max_retries=8`, `LLM_TIMEOUT=90`, no try/except in `app.py` |
| SQL execution | p50 167 ms, p90 344 ms — **not** the bottleneck | 2 timeouts only, on `dbo.ExportOrderBack` (4M rows, unindexed) |
| SQL overhead | catalog YAML re-read + sqlglot re-validate, whole catalog in prompt, new pyodbc connection, every question | `data/catalog.py`, `data/tools.py`, `data/connectors.py` |
| Chroma | new `PersistentClient` per query | `store.get_store()` not cached |

Two consequences of the user's answers: free tier → the plan must **cut LLM calls per question** (target: 0 for fixed-tool questions, ≤2 otherwise); read-only → **no views/indexes on the server**, so heavy work moves to our side (precomputation, caching, schema RAG).

Non-negotiables preserved throughout: bge-m3 embedding model; reranker local (may be disabled); ≤8 chunks / ≤50 rows to LLM; `guard()` + rollback-only connections are the only write barrier — never bypassed by caching; no vendor SDK imports outside `src/ragbot/llm/`; golden cases never deleted; >5-point eval drop blocks.

---

## Phase A — Quick wins (≈2 days). Expected: 40–60 s → 5–8 s per question

**A1. Reranker: keep on, make it ~10× cheaper** — `src/ragbot/embed.py`, `config/settings.yaml`
- `rerank_candidates: 20 → 10` (final_top_k is 6; candidates 11–20 rarely make it).
- `CrossEncoder(RERANK_MODEL, max_length=int(os.getenv("RERANK_MAX_LENGTH","512")))`; `predict(pairs, batch_size=len(pairs))`; `torch.set_num_threads(os.cpu_count())` once. Add `RERANK_MAX_LENGTH=512` to `.env.example`.
- Gate: `python scripts/hit_rate.py` before/after (30 cases, fast). Keep `RERANK_ENABLED=false` as demo escape hatch only.

**A2. Warm start + singletons** — `src/ragbot/store.py`, `src/ragbot/app.py`, `src/ragbot/data/catalog.py`, `Dockerfile`
- `get_store()` → `@lru_cache(maxsize=1)` (clear in `scripts/reindex.py`).
- `app.py`: `@st.cache_resource def _warm()` calling `get_embedder()`, `get_reranker()`, `get_keyword_index()`, `get_store()`, `load_catalogs()` under `st.status("Loading models…")` on first render; `@st.cache_data(ttl=300)` on `_categories()`.
- `load_catalogs()` / `load_fixed_tools()`: cache keyed on folder file mtimes (edits auto-invalidate; `check_catalog.py` unchanged).
- Dockerfile: `HEALTHCHECK --start-period=180s CMD curl -f http://localhost:8501/_stcore/health`; `TOKENIZERS_PARALLELISM=false`.

**A3. Fewer LLM calls (free-tier critical)** — `src/ragbot/agent/router.py`, `agent/rewrite.py`, `agent/orchestrator.py`
- Router decision ladder, no LLM when unambiguous: `_WRITE` → refuse; new `_DATA_STRONG` (EO/FileRef codes like `TAL-25-1493`, `EO 25-1234`, `PCD`, `PPM meeting`, `export order`, `ship date`) and no `_DOC_HINT` → `data`; `_DOC_HINT` and no `_DATA_STRONG` → `documents`; else small-model call as today.
- Fix the 83%→85% gap: extend `_DOC_HINT` with policy nouns (`licen[cs]es?|laptops?|desktops?|mailboxes?|servers?|backups?|retention|working days|rto|rpo`); add one sentence to `prompts/router.txt` ("how many" about licences/laptops/retention/backups is *documents*); bump `prompts/CHANGELOG.md`; add `expected_route` golden cases for the 4 known misroutes.
- In `orchestrator.answer()`: if `match_fixed_tool()` hits and no `_DOC_HINT`, set `r="data"` directly (skips route call).
- `rewrite.standalone_question`: `_needs_rewrite()` heuristic — only call the LLM when the follow-up has pronouns/ellipsis (`it|that|this|they|them|same|also|what about…`) or is <6 words; otherwise pass through.
- `.env`: answer `thinking_budget 1024 → 0` (extractive prompt; saves 1–2 s and tokens). Re-check `faithful`/`correct` in eval.

**A4. Reliability & friendly errors** — `src/ragbot/llm/openai_compat.py`, `llm/base.py`, `models.py`, `orchestrator.py`, `app.py`
- `LLM_MAX_RETRIES` default 2, `LLM_TIMEOUT` default 30.
- `llm/base.py`: `class LLMError(RuntimeError)` with `kind ∈ {quota,timeout,auth,other}`; each adapter exposes `_classify(exc)` (vendor imports stay in adapters).
- `Answer.error_kind: str = ""`; orchestrator catches `LLMError` and DB errors (already `QueryResult.error`) → friendly text via new `src/ragbot/data/errors.py:friendly()`: quota → "Daily AI quota reached — try again after midnight PT or ask IT to enable billing"; `HYT00`/timeout → "This query is heavy — try narrowing to one factory or month"; sensitive → "That information is restricted." Raw error stays in `warnings`/logs.
- `app.py`: last-resort try/except → `st.error(...)` + traceback to `logs/errors.log`.

**A5. UX polish** — `src/ragbot/app.py`, `config/settings.yaml`
- Suggested-question chips (`st.pills`) on empty history from `ui.suggestions:`; sidebar **Clear chat**; debug caption (route/tokens) behind `st.toggle("Admin details")`.
- PDF links: replace broken `file://` with Streamlit static serving (`.streamlit/config.toml` `enableStaticServing=true`, mount `data/pdfs` → `src/ragbot/static/pdfs`, link `app/static/pdfs/<Category>/<file>#page=N`); fallback `st.download_button`. Cache the source→path map (`_pdf_link` currently `rglob`s on every render).

---

## Phase B — Streaming, parallelism, caching (≈3–4 days). Expected: first token ≤ 3 s

**B1. Streaming in the LLM layer** — `llm/base.py` `stream()` → abstract `_stream()` (default: yield full `_chat` text so `anthropic.py` keeps working); `openai_compat.py` `_stream()` with `stream=True, stream_options={"include_usage": True}`; log one `calls.csv` row at completion.

**B2. `answer_stream()` generator in `orchestrator.py`** yielding `Stage(name)` / `Token(text)` / `Replace()` / `Final(answer)`. `answer()` becomes a thin wrapper draining it, so `scripts/eval.py` and `scripts/ask.py` are untouched. Split `_attempt_answer` into `_build_user_msg()` + `_finalize()` (the existing verify + references logic) so both paths share verification. First attempt streams; if `verify()` fails → `Replace`, run strict attempt. Streamed text never enters history/logs until verify passes. Track replace frequency in `verify_failures.csv`; if >10%, switch to verify-then-fake-stream.

**B3. `app.py` rendering** — `st.status` stages (Understanding → Searching / Querying DB → Writing), `st.empty()` container updated per token, `Final` → refs + `st.feedback` immediately (drop the `st.rerun()`).

**B4. Parallelism** — `retriever.retrieve()`: BM25 search in a `ThreadPoolExecutor(2)` while embedding+Chroma runs; orchestrator `both` route: `retrieve()` ∥ `answer_from_data()` (saves ~4 s).

**B5. Caching** — query-embedding `lru_cache(512)` in `retriever.py`; documents-route exact-match answer cache (key: normalized q + where, TTL 1 h, invalidated on `data/index/ingest_state.json` mtime, skipped when history non-empty); SQL result cache new `src/ragbot/data/cache.py` (TTL `data.result_cache_ttl_seconds: 180`, key = engine+conn+normalized sql+params, returns original `QueryResult` so `as_of` is honest; `refresh=True` bypass threaded from UI; hits logged to `sql.csv` as `cache:<tool>`).

**B6. DB connection speed** — `data/connectors.py`: small thread-safe per-DB pool (`queue.LifoQueue`, max 4, `SELECT 1` health check on acquire; **rollback on release stays**), `SET NOCOUNT ON` once per connection, `fetchmany(max_rows+1)` instead of `fetchall()`, timeout tiers (`data.timeout_seconds_heavy: 30` only for fixed tools flagged `heavy: true`). Test with two browser tabs.

---

## Phase C — Huge DB with read-only access: "schema RAG" (≈4–5 days)

Today 8 curated views are the whole catalog and ~20% of data questions in `logs/chat.csv` miss it ("how many suppliers", "buyer names"). With full read, the catalog becomes hundreds of tables; it cannot all go in the prompt. Curated views stay the gold tier; add an auto-discovered tier selected per question.

**C1. Offline discovery (read-only, once)** — extend `scripts/discover_schema.py` to emit `config/catalog/discovered/<db>.json`: tables, columns/types, PK, FKs, row counts (`sys.partitions`), `MS_Description`, sample values for low-cardinality varchar columns (≤30 distinct; skipped for sensitive columns). Optional `--describe`: one-line table descriptions, small model, batched 20 tables/call, generated **once** and cached (10–30 calls total — fits free tier). Never runs at question time.

**C2. Catalog model** — `data/catalog.py`: `Table` dataclass; `Catalog.tables`, `Catalog.sensitive`, `allowed_names` (curated views ∪ discovered tables minus YAML `exclude_tables:`); `render(selected=None)` renders curated views always and discovered tables only if selected, compact form with FK line and ≤40 columns.

**C3. Schema index** — new `src/ragbot/data/schema_index.py`: one doc per table → `ChromaStore(collection="schema_<db>")` (reuse `store.py`, `get_embedder()`) + `KeywordIndex` pickle (exact column names). `select_tables(question, cat, k=8)`: hybrid search → `hybrid.rrf()` → top-k → **expand 1-hop FK neighbours** (cap k+6) → return names + `join_hints`. Rebuilds lazily when the JSON sha256 changes. `scripts/index_schema.py` builds it. Offline test `tests/test_schema_index.py` with a hashing fake embedder.

**C4. Guard at scale** — `data/guard.py`: `guard(..., allowed_tables=frozenset(), sensitive=())`. Allowlist = **full** discovered catalog (security), prompt = selected subset (relevance). Keep `sys.`/`INFORMATION_SCHEMA`/three-part denials; add `FOR XML/JSON`, `suser_*`, `host_name`, `@@*`. Sensitive tier: reject any column/table matching `data.sensitive_patterns` (`salary|wage|bank|nid|passport|password|token|blood|religion|phone|mobile|email|address|dob`); **reject `SELECT *` on discovered tables** (allowed only on curated `rag.*`). Cost guard: tables with rows > `data.big_table_rows` (1M) must have a `WHERE`. Default args keep the existing 40 guard tests green; add ~20 adversarial cases (a real probe "A+ blood group … with number" exists in the logs — must return not-found, never rows).

**C5. Plug into generation** — `data/tools.py generate_and_run`: `pick_database()` (keyword score + schema-index top score) → `select_tables()` per DB → `render(selected)` → guard with `allowed_tables` → `rewrite_virtual` unchanged (only touches `rag.*`). Extend `_views_in`/`_keys_for` so `[D#]` row keys use `table.pk` for raw tables. Retry path: if the guard rejects a table that *is* discovered but wasn't selected, add it and retry once. Reorder `prompts/sql_generate.txt` so static text (rules, curated views, examples) is first and selected tables + date + question last (prompt-prefix stability for Gemini implicit caching); add rules "prefer rag.* views; never SELECT *; filter large tables; join only along listed paths".

**C6. Read-only workarounds for heavy queries (no DDL)** — new `scripts/refresh_aggregates.py` run on a schedule (Windows Task Scheduler / second container): whitelisted aggregate SQLs from `config/aggregates.yaml` through the same `guard`+`run`, results into local SQLite `data/index/aggregates.db` with `as_of`; fixed tools may declare `engine: local`. `pcd_history_by_eo` gets `heavy: true` (30 s tier). **Still send the DBA request** (`docs/schema/*.sql` views, `IX_ExportOrderBack_ExportOrderID` covering index, `rag_reader` login replacing `sa`, server-side `DENY SELECT` on HR/credential tables) as an optional upgrade; when granted, delete `definition:` fields — no other code change.

**C7. Zero-LLM data answers** — more fixed tools in `config/fixed_tools.yaml` mined from `logs/chat.csv` (upcoming PCDs, buyer list, cancelled orders this month, PPM meetings today, each with `example_params`); new `src/ragbot/agent/templated.py try_template()` for fixed-tool results (scalar → `"{col}: {val} [D1]"`; ≤50 rows → "N rows found — see table [D1]"), still run through `verify()`; `get_sql_chat()` reading `LLM_SQL_MODEL` (flash-lite is enough with a <3k-token schema subset). Rule-based `needs_clarification()` ("Which factory and period?") when a fixed tool matches but a required param is missing → `route="clarify"`.

**C8. Data UX** — `Answer.results` (rows capped at `max_rows`); `show_refs` renders `st.dataframe` + "N rows · as of · view" caption + CSV `st.download_button` + existing "show SQL" expander; `st.bar_chart` when 2 columns and ≤25 rows; **Refresh data** button (`refresh=True`); per-tool `follow_ups:` as pills.

---

## Phase D — Scale documents to 60K pages (≈1–2 weeks, incremental)

**D1. BM25 → SQLite FTS5** — `rank_bm25` scores every chunk in Python (O(n)) and the pickle is ~1–2 GB at 60K chunks. Add `chunk_fts` FTS5 virtual table to the existing `data/index/registry.db` (`store.py SCHEMA`, `unicode61` tokenizer, pre-tokenized with the existing `keyword.tokenize()` so `PCD-02` keeps matching; `category`/`superseded` as UNINDEXED columns for pushdown). `Registry.replace_chunks`/`remove_document` maintain it incrementally; `keyword.KeywordIndex.search()` becomes `SELECT id, bm25(chunk_fts) … MATCH ? LIMIT ?` (OR-joined tokens, negate score). Class/function names unchanged so `retriever.py` is untouched; drop the full `rebuild_keyword_index` from `pipeline.py`. Add `tests/test_keyword.py` (exact codes, a Bangla word). Gate: `hit_rate.py` ≥ baseline.

**D2. Chroma at 60K vectors** — fine on CPU (~250 MB vectors + HNSW ≈ 500 MB). Set HNSW `ef_construction 200, ef_search 100, M 32` on collection creation (needs `reindex.py`). Push list filters down with `{"category": {"$in": [...]}}` instead of post-hoc `_passes` (which today silently shrinks the candidate set).

**D3. Ingestion throughput** — bge-m3 fp32 on CPU ≈ 3–6 chunks/s → 60K pages (~60–100K chunks) ≈ 4–8 h once; OCR (eng+ben) dominates for scans. Add `embedding_cache(hash, model, vec)` table in registry keyed by `sha256(chunk.text)`; chunk-level incremental in `Registry.replace_chunks` (diff by id+hash, don't re-embed unchanged chunks); background worker `scripts/ingest_worker.py` (the planned M7) as a second container sharing the `data/index` volume, touching `ingest_state.json` when done; app clears `get_keyword_index`/answer cache on mtime change. Docker memory limit 8 GB. Optional: `EMBED_BACKEND=onnx` int8 bge-m3 via `optimum` (~2–3× faster) behind the CLAUDE.md parity test (`scripts/parity.py`, cosine ≥ 0.99 on 200 chunks) — one backend per index.

**D4. Reranker at scale** — candidate pool stays 10–20 regardless of corpus size, so A1 holds. If still >4 s: ONNX int8 `bge-reranker-v2-m3` behind `RERANK_BACKEND=onnx` (~1–1.5 s for 10×512).

---

## Phase E — Data freshness: new DB rows and new PDFs reach the chatbot without anyone touching it (≈1–2 days; pull forward, before Phase D)

How the two halves stay current today, and what changes:

| Source | Today | After Phase E |
|---|---|---|
| **SQL Server rows** | Always live: every data question runs SQL against the server at that moment (`data/connectors.py run_sqlserver`); `QueryResult.as_of` records the read time. | Unchanged. The Phase B5 result cache adds ≤180 s staleness, always shown in the `[D#]` caption, bypassed by the **Refresh data** button; `data.result_cache_ttl_seconds: 0` disables it entirely. |
| **PDFs** | Manual, three steps: drop file in `data/pdfs/<Category>/`, run `python scripts/ingest.py`, **restart the app**. The running Streamlit process holds the BM25 pickle (`retrieve/keyword.py get_keyword_index`, `lru_cache`) and the Chroma client (`store.get_store`, now cached) in memory, so an ingest done in another process is invisible until restart. | Automatic: a watcher ingests new/changed/removed files on a schedule and the app hot-reloads its indexes when the ingest finishes — no command, no restart. |

**E1. Ingestion worker (the planned M7)** — new `scripts/ingest_worker.py`
- Loop: every `ingest.watch_interval_seconds` (setting, default 300) scan `pdf_root` and compare `(name, size, mtime)` against the previous scan; only when something differs call the existing `ingest_folder()` (`src/ragbot/ingest/pipeline.py:112`), which is already idempotent by SHA-256 (new files added, changed files re-parsed/re-embedded, removed files deleted, `superseded` flags recomputed, `ingest_state.json` written). Log the returned summary; on exception log and continue (never die on one bad PDF — `ingest_folder` already records per-file `failed`).
- `--once` flag for a one-shot run (replaces remembering `scripts/ingest.py`; keep that script as is). `--interval N` overrides the setting.
- Single-writer lock: `data/index/ingest.lock` (create-exclusive; stale if older than 6 h) so a manual `scripts/ingest.py` and the worker never write Chroma/registry concurrently.
- Deployment: second container from the same image, `command: python scripts/ingest_worker.py`, sharing the `data/index`, `data/pdfs` and `/models` volumes with the app — add `docker-compose.yml` (services `app`, `ingest-worker`; app gets `mem_limit: 8g`). On the Windows box without compose: a Task Scheduler entry running `python scripts/ingest_worker.py --once` every 10 min. Document both in `README.md` runbook ("Add documents").

**E2. Hot reload in the app** — `src/ragbot/retrieve/keyword.py`, `src/ragbot/store.py`, `src/ragbot/agent/orchestrator.py`
- New `src/ragbot/index_version.py`: `index_version() -> int` returns `ingest_state.json` mtime_ns (0 if missing). `refresh_if_changed()` compares to the last seen value and, on change, calls `get_keyword_index.cache_clear()`, `get_store.cache_clear()` and clears the documents answer cache (Phase B5) so a superseded document's cached answers vanish too. Chroma's `PersistentClient` re-reads its sqlite/HNSW segments on open, so re-creating the client is what makes the worker's writes visible.
- Call `refresh_if_changed()` at the top of `orchestrator.answer()` (cheap: one `stat()`). The next question after an ingest sees the new PDF; nothing else changes.
- `app.py`: `_categories()` (`st.cache_data(ttl=300)` from A2) keyed on `index_version()` so a new category folder shows up in the sidebar filter; sidebar caption shows "Index updated <time>, N documents" from `ingest_state.json` instead of the current "run `python scripts/ingest.py`" instruction.

**E3. Cheaper re-ingest of revised documents** (the part that matters at 60K pages; shared with D3)
- Embedding cache table in `data/index/registry.db`: `embedding_cache(hash TEXT PRIMARY KEY, model TEXT, vec BLOB)`, keyed by `sha256(chunk.text)`. `pipeline.py` looks up before `emb.embed()`; a revised 400-page manual re-embeds only the pages whose text changed (today: all of it, at 3–6 chunks/s on CPU).
- Chunk-level diff in `Registry.replace_chunks` (`store.py`): delete/upsert only chunk ids whose text hash changed.
- Because BM25 is still a full rebuild (`rebuild_keyword_index`) until D1 lands, the worker's ingest of one new file at 60K chunks costs a full pickle rebuild (~minutes). D1 (FTS5) removes that; sequence E before D1 anyway — correctness first, then speed.

**E4. What "new version of a document" does** — no code change, document it: dropping "SOP v4" beside "SOP v3" makes the pipeline mark v3 `superseded`; `retrieval.default_filters.superseded: false` hides it from answers; references still show `superseded` when an admin filter includes it. State this in the README "Add documents" runbook so users know to keep the old file or delete it (deleting removes it from the index on the next worker pass).

Verification: with the app running, copy a PDF into `data/pdfs/General/`, wait one worker interval (or run `--once`), ask a question only that PDF answers — it must be cited without restarting the app; `logs/ingest.log` shows the run; sidebar "Index updated" time changes. Delete the file → next pass removes it → the same question returns not-found. Drop a revised copy → only changed chunks are re-embedded (log `embedded N / cached M`). Run `scripts/ingest.py` while the worker is mid-run → the second one exits with "index locked".

---

## Order of execution

A1 → A2 → A4 (one day: biggest pain relief) → A3 → A5 → **E1 → E2** (documents become self-updating) → B1–B3 → B4–B6 → C1–C5 → C7 → C8 → C6 → **E3** → D1 → D2 → D3 → D4. Send the DBA request (C6) at the start; it runs in parallel.

## Verification (after each phase)

1. `pytest -q` — existing 99 + new tests (`test_guard.py` additions, `test_keyword.py`, `test_schema_index.py`, `_classify`).
2. `python scripts/hit_rate.py` — retrieval-only, seconds; primary gate for A1, B4, D1, D2.
3. `python scripts/eval.py --limit 20 --kind <phase>` then full 62 cases before merge; compare `hit`, `faithful`, `correct`, `citation_valid` with the last `eval/results/*.json`; >5-point drop blocks. Target correctness ≥ 85% after A3.
4. `python scripts/check_catalog.py --live` after any catalog/fixed-tool/discovery change.
5. Timing: `logs/calls.csv` (route→answer gap = retrieval+rerank, target ≤ 6 s; LLM `seconds`), `logs/sql.csv` (`ms`, `cache:` rows). Targets: fixed-tool question < 0.5 s and 0 LLM calls; document question P50 ≤ 8 s, first token ≤ 3 s; generated-SQL question ≤ 2 LLM calls.
6. Manual: follow-up with a pronoun (rewrite fires) vs without (skipped); bad API key → friendly quota message, no traceback; click a PDF reference in the browser; ask the same data question twice → same `as_of`, `cache:` row; two browser tabs concurrently (pool); the "blood group" probe → not-found.
7. Freshness (Phase E): add a PDF while the app runs → cited after one worker pass with no restart; delete it → not-found on the next pass; insert-then-ask on the DB side is immediate (no ingest involved).
