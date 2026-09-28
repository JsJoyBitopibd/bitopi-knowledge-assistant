# Roadmap: fast, user-friendly Bitopi Knowledge Assistant at scale

The speed / UX / scale plan (Phases A–E, released as v1.1.0–v1.5.0) and the plan after it
(**Phases F–I**, at the end of this file). This file is the design; **what is done and what is next
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

After v1.5.0 (Phases F–I below): **H0** (hotfix) → **F4** (request drafted; the DBA works in parallel) → **F1** → F2 (when the DBA is done) → G1–G4 → H1–H4 → I1 → I2–I5.

## Verification (after each phase)

1. `pytest -q` — existing 99 + new tests (`test_guard.py` additions, `test_keyword.py`, `test_schema_index.py`, `_classify`).
2. `python scripts/hit_rate.py` — retrieval-only, seconds; primary gate for A1, B4, D1, D2.
3. `python scripts/eval.py --limit 20 --kind <phase>` then full 62 cases before merge; compare `hit`, `faithful`, `correct`, `citation_valid` with the last `eval/results/*.json`; >5-point drop blocks. Target correctness ≥ 85% after A3.
4. `python scripts/check_catalog.py --live` after any catalog/fixed-tool/discovery change.
5. Timing: `logs/calls.csv` (route→answer gap = retrieval+rerank, target ≤ 6 s; LLM `seconds`), `logs/sql.csv` (`ms`, `cache:` rows). Targets: fixed-tool question < 0.5 s and 0 LLM calls; document question P50 ≤ 8 s, first token ≤ 3 s; generated-SQL question ≤ 2 LLM calls.
6. Manual: follow-up with a pronoun (rewrite fires) vs without (skipped); bad API key → friendly quota message, no traceback; click a PDF reference in the browser; ask the same data question twice → same `as_of`, `cache:` row; two browser tabs concurrently (pool); the "blood group" probe → not-found.
7. Freshness (Phase E): add a PDF while the app runs → cited after one worker pass with no restart; delete it → not-found on the next pass; insert-then-ask on the DB side is immediate (no ingest involved).

---

# Phases F–I — after v1.5.0

Merged on 2026-09-28 from two sessions' proposals: the other session's plan first (F: ready for more
users, G: polish), this session's code findings after it (H: ingestion robustness, I: speed), and the
empty-folder wipe fix pulled forward as hotfix H0. Status lives in `docs/PROGRESS.md`. Each phase ends
with the gate above (tests, `hit_rate.py`, full eval with no drop > 5 points) and becomes a version.

**The GitHub repo is public.** New documents that map internal systems (the F4 DBA request: logins,
the DENY list of sensitive tables) go into the git-ignored `private/` folder and are handed to the
user; they are not committed.

## H0 — Hotfix: cautious deletes (PRD FR-2.14) → v1.5.1

Problem: `ingest_folder()` (`src/ragbot/ingest/pipeline.py`) deletes every registry source that the
current walk did not see. `Path.rglob` on a missing folder returns nothing (no error), so one pass
over an offline share or a wrong mount empties the index, and the worker triggers that pass by
itself as soon as its snapshot changes.

- **Missing or empty folder**: if `pdf_root` is not a directory, or the walk finds no PDFs while the
  registry knows at least one source → log an error, put an `alert` in the summary and in
  `ingest_state.json`, change nothing.
- **Two-scan rule**: `document.missed_scans` (added with `ALTER TABLE` on older registries). A full
  scan resets it to 0 for every file it sees and adds 1 for every known file it misses; a file is
  deleted when the count reaches 2. It lives in the registry, so `ingest_worker.py --once` from Task
  Scheduler behaves exactly like the loop.
- **Mass-disappearance stop**: if the missing files exceed `max(ingest.removal_alert_min_files,
  ingest.removal_alert_fraction × known)` (defaults 3 and 5 %) → alert, and neither count nor delete
  (a transient outage must not count toward the two-scan rule). The floor keeps normal deletions
  possible in a small corpus.
- **Same area**: `sha256()` moves inside the per-file `try` (an unreadable file becomes a `failed`
  row instead of aborting the run and killing the worker); the summary's `failed` is this run's count
  (it was overwritten by the registry-wide total).
- Tests in a throwaway index: missing root, empty root, one file gone for one then two scans, file back
  after one missed scan, more than the threshold missing, unreadable file.

## Phase F — Ready for more users → v1.6.0

### F4 — DBA request (first, so the DBA works in parallel)
`private/DBA_REQUEST.md`, assembled from `docs/DATA_ACCESS.md` and `docs/schema/rag_views_*.sql`:
schema `rag` and its views, each exposing a `Factory` column and filtering on
`SESSION_CONTEXT(N'factories')` (row-level security as the alternative) — the same predicate F1
applies in code; login `rag_reader`; `IX_ExportOrderBack_ExportOrderID`. Two access options for the
DBA, because the virtual views, generated SQL over raw tables, `discover_schema.py`,
`check_catalog.py --live` and `refresh_aggregates.py` all read base tables today: (a) SELECT on `rag`
plus the base tables with explicit DENY on HR / credential / sensitive tables, or (b) strict FR-3.1
views-only, in which case raw-table answers are switched off (remove
`config/catalog/discovered/*.json`) and the maintenance scripts get a separate login.

### F1 — Login + per-user scoping (PRD FR-4.1, 4.3, 4.5–4.8, FR-6.2)

Today identity is a free-text "Your name" box (`src/ragbot/app.py`) that only reaches the logs;
retrieval never sees the user; the caller's `where` overrides the default filters
(`retriever.py`); the keyword search silently ignores filter keys other than
`superseded`/`category`/`source`; 8 of the 15 fixed tools have no factory filter and the other 7 take
the factory from the question text; both caches carry a TODO that scope must join the key; the admin
toggle is open to everyone; the app listens on 0.0.0.0:8501 with no auth layer.

**Scope model** — `src/ragbot/auth/models.py`: frozen `Scope(factories, departments,
confidentiality_max, buyer_codes=None (= all), is_admin)` with `unrestricted()`, `all_factories`,
`db_factories()`, `allowed_levels()`, `allows_factory()` and `key()` (canonical, order-independent;
used in cache keys and logs); `User(name, groups, scope)`. `ALL` / `Common` are always included.
`scope` is a **required keyword-only argument** of `answer()`, `answer_stream()`, `retrieve()` and
`answer_from_data()` — a forgotten call site fails loudly instead of silently seeing everything;
`scripts/ask.py`, `eval.py`, `hit_rate.py` pass `Scope.unrestricted()` explicitly. (Worker threads
do not inherit a ContextVar, so an explicit argument is also the only reliable way through the
`ThreadPoolExecutor`s in the orchestrator and the retriever.)

**Sign-in** — `src/ragbot/auth/providers.py`: `LdapProvider` (`ldap3`; NTLM bind as `DOMAIN\user`
over LDAPS/StartTLS; an empty password is refused before binding, because AD accepts it as an
anonymous bind; the username is escaped in the search filter; direct `memberOf` groups) and
`LocalProvider` (`config/users.yaml`, bcrypt; dev and tests only). `src/ragbot/auth/scopes.py`:
`config/scopes.yaml` maps AD groups to partial scopes, a user's scope is the union over their groups,
a user in no mapped group is not enrolled (login refused); `admins:` lists admin groups. Settings
`auth.provider: ldap|local`; `.env` `LDAP_HOST`, `LDAP_DOMAIN`, `LDAP_BASE_DN`, `LDAP_USE_SSL`.
`app.py`: login form first, `st.session_state.user`, logout, per-session attempt throttle,
**history and pending questions cleared on login/logout** (earlier turns re-render their references
and PDF buttons), admin details only for admins. Passwords are never logged or stored. Later option:
Streamlit's `st.login()` (OIDC) if the company has Entra ID.

**Documents (FR-4.3, 4.5, 4.6)** — chunk and document attributes `factory` (`TAL|RHL|BGL|KTL|CKDL|ALL`),
`department` (all `Common` in the pilot — plumbing only), `confidentiality`
(`public|internal|restricted`, default `internal`), `buyer_code` (empty for staff documents).
`src/ragbot/ingest/meta.py: attributes_for(path, root)`: factory from the first folder when it is a
known factory code, otherwise `ALL`; an optional `meta.yaml` in any folder (inherited downwards)
overrides, which is how restricted folders are marked. Six places must change together or the keyword
path ignores scope: `Chunk` fields + `metadata()`, `chunk_fts` DDL (four new UNINDEXED columns),
both FTS inserts, `_chunk_from_record`, the `document` table, and the keyword filter (which becomes
a generic list-column filter) — one round-trip test covers all six. `chunk_fts` is recreated when
`PRAGMA table_info` shows the old shape (FTS5 cannot add columns) and reseeded through `seed_fts`,
which reads the attributes from `document`. An unchanged file whose attributes changed (moved folder,
edited `meta.yaml`) is retagged without re-embedding (`Registry.retag`,
`ChromaStore.set_attributes`, generalising `set_superseded`). `scripts/tag_documents.py` (idempotent,
`--dry-run`) tags the existing index and **must run before scope is enabled**: untagged chunks fail
closed (a Chroma `where` on a missing key and a NULL FTS column both exclude them). It refuses
same-named files with different attributes.

**Retrieval** — `src/ragbot/auth/filters.py: scope_where(scope)` (empty for unrestricted users; the
factory list always contains `ALL`). `retrieve()` rejects scope keys in the caller's `where` and merges
`scope_where(scope)` last, so a caller can only narrow; `_passes` checks the same fields as a second
line of defence. The sidebar category list is filtered per scope for convenience only.

**Data (FR-4.4): the scope predicate lives in the view, not around the query.** Wrapping the executed
SQL as `SELECT * FROM (<sql>) q WHERE q.Factory IN (…)` does not work: aggregates (`COUNT(*)`,
`DISTINCT Buyer`) have no Factory column to filter on, the guard's `TOP (200)` runs before the outer
filter (a TAL user would get "TAL rows among everyone's first 200"), and the guard runs before the
virtual-view rewrite, so it could not see the predicate. Instead `rewrite_virtual()`
(`src/ragbot/data/virtual.py`) — which every executed statement already passes through (fixed tools,
generated SQL, `refresh_aggregates.py`) — emits each view as
`rag_vw_X AS (SELECT * FROM (<definition>) v WHERE v.<scope_column> IN ('TAL'))`. The values come
from `config/scopes.yaml` and are validated (`^[A-Z0-9]{2,6}$`), never from user input.
`View.scope_column` is declared per view in the catalog YAML; `assert_scoped()` re-checks the
rewritten SQL (same pattern as the existing DENY re-scan) and raises when a restricted user touches a
view without a scope column (fail closed). This is byte-for-byte what the DBA's SESSION_CONTEXT view
will do, so switching to the server views later is config only (`data.scope_mode: session_context`;
the connector then sets the session context on every pool checkout inside the retry loop, with
`@read_only = 0`, and clears it on release).
- Views without a factory today (`vw_ExportOrderColorSize`, `vw_PCDChangeHistory`,
  `vw_PPMMeetingReschedule`, `vw_PPMDepartmentChecklist`) get `Factory` through joins (to
  `FileRef` / `PPMMeetings`), also in `docs/schema/rag_views_*.sql`. `vw_CancelledExportOrder` only
  has a free-text `FileRefNo`; until the DBA confirms a join, the two cancelled-order tools are for
  unrestricted users only.
- Fixed tools that take a factory from the question validate it against the scope and return an
  explicit "outside your scope" answer (otherwise the view filter would produce a false "RHL has 0
  meetings").
- Raw discovered tables have no common scope column: for restricted users the allow-list is empty and
  schema selection is skipped, so the model is not invited to write SQL the guard will refuse.
- The local aggregate copy (`pcd_history`) gets a Factory column at its next refresh; the local query
  is scoped the same way, and falls back to the live query when the column is missing.
- Scope values never go into `QueryResult.params` (shown in "show SQL", sent in the prompt, and
  treated as citable by `verify()`).

**Caches** — `scope.key()` joins the documents-answer key (looked up before routing, so every route
is affected) and the SQL result-cache key (`src/ragbot/data/cache.py`), which the predicate would
cover implicitly but the session-context mode would not.

**Audit and admin (FR-4.8, FR-6.2)** — `src/ragbot/logs.py: append_row(name, header, row)`, which
rotates a file whose header differs (never rewrites in place); used for `chat.csv` (fixing today's
header mismatch), `sql.csv` and `calls.csv`. `chat.csv` and `sql.csv` gain `scope`. Admin sidebar:
own effective scope, scope lookup by username, export of a user's `chat.csv` rows.

**Tests** — scope union and keys; `scope_where` for Chroma and FTS (an RHL chunk never comes back for
a TAL user, a code that exists only in a restricted chunk returns nothing); retriever spy (the scope
reaches both indexes, reserved keys are rejected); virtual-view predicate (every CTE scoped, a view
without a scope column fails closed, injection-shaped values rejected, output still parses and passes
the DENY re-scan); fixed-tool factory check with no DB call; both cache keys per scope; local and LDAP
providers (fake `ldap3`: empty password never binds, filter escaped); log rotation; `meta.yaml`
inheritance and retagging. Adversarial suite `tests/scope_cases.jsonl` (≥ 30 for the pilot, 50 by
PRD Phase 3) run by `scripts/eval.py --kind scope` with local users (`tal_internal`, `rhl_internal`,
`all_internal`, `admin`): cross-factory document and data questions, restricted documents, a forced
category filter, follow-ups, prompt injection ("ignore your scope…", `Factory IN ('RHL')` in the
question), the cache (unrestricted user asks, TAL user asks the same), a raw-table probe, a superseded
restricted revision. Pass = no out-of-scope reference, row or source text in any answer. **Zero leaks
is the release condition.**

Order inside F1: F4 → scope model, scopes.yaml, local provider → document attributes, FTS reshape,
`tag_documents.py` → retrieval + caches → view predicate, fixed tools, aggregates, raw-table gate →
LDAP provider, login UI, admin panel → audit columns → adversarial suite → gate. New dependency:
`ldap3` (pure Python; `bcrypt` is already installed).

**As built (v1.6.0, 2026-09-28) — where it differs from the design above:**
- Sign-in binds as `user@domain` with SIMPLE authentication over LDAPS or StartTLS (never plain),
  not NTLM: NTLM in ldap3 needs MD4, which OpenSSL 3 no longer provides.
- No `scripts/tag_documents.py`: the ingest pass itself retags any unchanged file whose tags differ
  from its folder's, and a registry from before F1 gets NULL tags, so the first `ingest.py` run (or
  worker pass) after the upgrade tags everything without re-embedding. The vector store is updated
  before the registry, so a failure is retried on the next pass.
- `vw_CancelledExportOrder` gets its factory through `ExportOrderID` → `ExportOrder` → `FileRef` (every
  row resolved live), so the cancelled-order tools work for factory-limited users.
- Factory codes are validated as `^[A-Z0-9]{1,8}$` (or `*`); raw unit codes such as `01` are valid.
- `append_row` writes `chat.csv` and `sql.csv` (both gained `scope`); `calls.csv` is unchanged.
- The adversarial suite is `scripts/scope_check.py`, which builds its own throwaway index from generated
  PDFs (so the live index never holds test documents) and uses in-code scopes rather than local users.
- A fixed tool that refuses a factory answers at once ("System doesn't have the data." + the access
  sentence), without the document fallback.
- Found by the independent review and fixed: the guard now accepts a bare name as a CTE only where SQL
  binds it to one (after its declaration, or in the main query), since SQL Server binds a forward
  reference to the real base table; `assert_scoped` checks every table of the final statement; a chunk
  without tags no longer inherits the permissive model defaults.

### F2 — `sa` → `rag_reader` (after the DBA delivers)
`.env` login, restart `app` and `aggregates`, `python scripts/db_ping.py` (prints the login name),
`check_catalog.py --live`, delete the `definition:` fields once the server views exist,
`data.scope_mode: session_context`; update `docs/DATA_ACCESS.md` and README.

### F3 — Gemini billing (user)
Paid key in `.env` (PRD FR-8.9: free-tier keys are never used with company data). Check: one
`scripts/ask.py` call logged; an eval run with no 429.

## Phase G — Polish → v1.7.0

- **G1 raw-table codes → names.** Read-only check that a code such as `C/09/7` is a
  `dbo.Contact_Master.ContactID` (the PK, varchar; `ContactNo` is an int, which is why the earlier probe
  failed). Then an optional `hints:` key in the curated catalog YAML (`{table, column, joins, note}`),
  merged into the discovered `Table` objects in `data/catalog.py` so `join_hints()` / `fk_neighbours()`
  (`data/schema_index.py`) pull `Contact_Master` in; the hints' hash joins the schema-index cache key.
- **G2 samples for selected tables.** `discover_schema.py --samples --tables …` that merges samples
  into the existing JSON (today a run without `--samples` wipes them); the table list comes from the
  raw tables in generated SQL (`logs/sql.csv`) plus a new `logs/schema_select.csv` written by
  `generate_and_run()`. Then `index_schema.py` for that database. Live run only with the user's
  go-ahead.
- **G3 follow-up chips.** `follow_ups:` templates per fixed tool (filled by `templated._fill`),
  `Answer.follow_ups`, buttons under the latest turn reusing the `pending_q` mechanism, kept in the
  history entry; `example_params` on every fixed tool.
- **G4 noise and log hygiene.** `.streamlit/config.toml` (`server.fileWatcherType = "none"`), added to
  the Dockerfile COPY list; `width="stretch"` instead of `use_container_width`; every log file goes
  through `append_row` (introduced in F1).

## Phase H — Ingestion robustness (rest of PRD FR-2.14, PLAN M7) → v1.8.0

- **H1 same-name PDFs.** A name seen twice in one walk → the second file is a `failed` row ("duplicate
  file name, already indexed from <folder>"). Changing `source` to the relative path would change
  every chunk id (CLAUDE.md fixes the format), so that stays a decision.
- **H2 `inspect.py --check`** compares registry `chunk` ids, Chroma ids and `chunk_fts` ids, reports
  orphans each way, ok documents with no chunks and superseded-flag mismatches, exits 1 on any
  mismatch. `ingest_folder` deletes a document's old vectors only after the new chunks are embedded, so
  a failed re-ingest leaves the old version whole.
- **H3 worker.** Lock helpers move to `src/ragbot/ingest/lock.py` and are taken by `ingest.py` and
  `reindex.py` too; `ingest_run.status` marks a crashed run; `document.attempts` retries failed files
  up to 3 times; `ingest_state.json` is written atomically and only when something changed, plus a
  `last_scan.json` heartbeat; sidebar "Index updated … · last scan … · N failed".
- **H4 services.** README runbook for two Task Scheduler entries (worker `--once` every 10 min,
  `refresh_aggregates.py` nightly) and the docker-compose services; the user creates them.

**As built (2026-09-28) — where it differs from the design above:**

- A skipped same-name file cannot be a `failed` row (the document table is keyed by the file name
  it shares with the indexed copy): it is listed in the run summary and `last_scan.json`, which
  `inspect.py --failed` and the admin sidebar line read. The copy already indexed is kept, recognised
  by its recorded path (`document.rel_path`).
- `document.rel_path` also fixes a leak path: the PDF download button found the file by name
  (`rglob`) and could serve the skipped copy, from a folder the user may not be allowed to see. It now
  reads the exact path that was indexed.
- `doc_hash` now means "the version that is indexed"; a failed attempt goes to `failed_hash` +
  `attempts`, so a failed update keeps the old version searchable and is retried (3 tries per version).
  A `dirty` flag is set while a document's chunks are replaced and cleared by the final document write:
  a pass killed mid-write, even a `--redo` of an unchanged file, is rewritten by the next pass
  (verified with hard kills of a real rewrite). `ingest.py --redo` is the repair for `--check`.
- Unchanged files are no longer read: same path, size and mtime as when the indexed version was hashed
  → skipped (the CLAUDE.md M7 rule). With the lazily loaded embedding model, a pass that finds nothing
  new takes seconds.
- The lock's holder refreshes its mtime every minute, so a lock is abandoned after 15 minutes without
  a refresh (any host) or at once when its process is gone (same host). A long bulk ingest is never
  taken over, and a process removes only a lock it holds.
- Found on the way: a PDF that failed to open stayed locked while the logged exception lived (Windows:
  nobody could replace or delete it); a moved file's category was never updated; the sidebar's
  `st.cache_data` functions ignored the index version (arguments starting with `_` are left out of the
  key).

## Phase I — Speed (PLAN M8) → v1.9.0

- **I1 measure.** `Answer.timings` and `Answer.request_id`; `perf_counter()` around rewrite, route,
  embed, vector, keyword, rerank, SQL, LLM (with time to first token) and verify; `request_id` in
  `chat.csv`, `calls.csv`, `sql.csv`; `scripts/latency.py` over `tests/latency_cases.jsonl` (50 cases
  from the golden set) prints p50/p95 per stage, end-to-end and first token into
  `eval/latency/<ts>.json`. New fields only — the Stage names are pinned by
  `tests/test_answer_stream.py`.
- **I2 documents p50 ≤ 8 s**, levers chosen from I1: fewer or shorter rerank candidates (gate
  `hit_rate.py` ≥ 29/30), onnxruntime thread settings, more router short-cuts, a prefix-stable answer
  prompt for Gemini's implicit caching.
- **I3 first token ≤ 3 s**: if Gemini's one-or-two-burst streaming is the limit, show the references as
  soon as retrieval finishes and record the limit; the provider itself is chosen by the golden-set eval.
- **I4 load test**: 100–1,000 generated PDFs (copies with a changed cover page) into a throwaway index
  while `latency.py` runs; p95 rise < 20 %; throughput into `docs/tuning_log.md`.
- **I5 bulk ingestion**: `embed_backend` stored next to `embed_model` and checked at start;
  `scripts/export_embedder_onnx.py` (same recipe as the reranker, no `optimum`) and `scripts/parity.py`
  (1,000 chunks, cosine ≥ 0.99, hit-rate drop ≤ 1) before any switch — with the user's approval.
