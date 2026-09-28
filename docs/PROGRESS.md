# Progress: what is done, what is next

**Read this first in a new session.** It is the single place that says where the work stands.
Design: `docs/ROADMAP.md` (Phases A–E). Release history: `CHANGELOG.md`. Milestones M0–M8:
`docs/PLAN.md`.

## Snapshot

| | |
|---|---|
| Released | **v1.5.1** (hotfix H0: cautious deletes), tag `v1.5.1`, on `main` |
| Plan | **Phases F–I** (below; design in `docs/ROADMAP.md` "Phases F–I"), merged 2026-09-28 from two sessions' proposals. Order: **H0 hotfix → F → G → H → I** |
| In progress | nothing |
| Next task | **F1** — login + per-user scoping (F4 DBA request sent 2026-09-28; F2 waits for the DBA) |
| Last eval | 2026-09-27, `eval/results/20260927T1710.json`: correctness 90%, faithful 100%, hit 96% (one rate-limit miss), citations 100% |
| Tests | 282 passing (`.venv\Scripts\python -m pytest -q`) |
| Last updated | 2026-09-28 |

## How to update this file (every session)

A task is marked ✅ only after it is **verified**: tests pass and the task's check in the table
below has been run. Then, in the same commit as the code:

1. Set the task's status to ✅ and fill in *Evidence* (test names, a measured timing, an eval file).
2. Move **Next task** in the snapshot to the next ⬜ row; update *Tests*, *Last eval*, *Last updated*.
3. Add a line under `[Unreleased]` in `CHANGELOG.md`.
4. When a phase is complete: move its `[Unreleased]` entries under a new version heading, merge
   the branch to `main`, tag `vX.Y.Z`, and set the snapshot's *Released* row.

Status key: ✅ done and verified · 🔄 in progress · ⬜ not started · ⏸ blocked (reason in *Evidence*)

## Phase A — Quick wins (v1.1.0) ✅

| ID | Task | Status | Evidence |
|---|---|---|---|
| A1 | Reranker 10 candidates @ 512 tokens, one batch | ✅ | `hit_rate.py` 29/30; commit `48e2595` |
| A2 | Warm start, cached store / catalogs | ✅ | commit `48e2595` |
| A3 | Fewer LLM calls (regex router, skip rewrite, thinking 0) | ✅ | eval correctness 85% |
| A4 | Friendly LLM / DB errors, fast fail | ✅ | commit `48e2595` |
| A5 | UX polish (chips, Clear chat, admin toggle, PDF download) | ✅ | commit `48e2595` |

## Phase B — Streaming, parallelism, caching (v1.2.0) ✅

Targets: first token ≤ 3 s on a document question; `both` questions ~4 s faster; repeat questions instant.

| ID | Task | Status | Check before ✅ | Evidence |
|---|---|---|---|---|
| B1 | `ChatModel.stream()` + OpenAI-compatible streaming; one `calls.csv` row per stream | ✅ | unit test with fake client; live Gemini stream | `tests/test_stream.py` (4). Live 2026-09-27: 8 deltas, first at 2.6–3.4 s, one `calls.csv` row. Found + fixed: provider errors crashed instead of raising `LLMError`; `thinking_budget: 0` is rejected (HTTP 400) by flash-lite |
| B2 | `answer_stream()` events (Stage / Token / Replace / Final); `answer()` drains it; verify before Final | ✅ | unit tests: event order, Replace on failed verify, LLM error mid-stream | `tests/test_answer_stream.py` (6). Live 2026-09-27 (rerank off): 2 doc questions verified with correct `[P#]`; first token 3.9–6.1 s. Gemini sends short answers in 1–2 bursts, so first token ≈ full answer; model latency after "writing" is 3.6–3.8 s |
| B3 | UI renders stages + tokens live; no extra rerun | ✅ | manual in browser | Browser 2026-09-27, rerank ON: stage label updates live, chips clear on ask, "Answered in 19.2 s" / "17.9 s" (follow-up "Who approves it?" rewritten to Form IT-03), references + thumbs shown at once, thumbs-up logged to `chat.csv`, no app errors |
| B4 | Parallel: BM25 ∥ embedding; documents ∥ database for `both` | ✅ | unit test; timing | `tests/test_parallel.py` (2): two 0.4 s steps finish in < 0.6 s. `hit_rate.py` (rerank on) 29/30, same as baseline |
| B5 | Caches: query embedding, documents-answer cache, SQL result cache (+ Refresh data) | ✅ | unit tests: TTL, invalidation, `as_of` kept, refresh bypass | `tests/test_cache.py` (8). Browser 2026-09-27: repeat document question 32.2 s → **0.1 s** (same verified answer + `[P1]`); fixed tool 474 ms → cache hit 5 ms with identical `as_of`, logged `cache:<tool>`; ↻ Refresh data re-ran `eo_by_id` live (`sql.csv`) |
| B6 | SQL Server connection pool, `NOCOUNT`, `fetchmany`, heavy-tool timeout tier | ✅ | unit test with fake connections; live DB ping | `tests/test_pool.py` (6). `db_ping.py`: both DBs OK. Live: count tool 266 ms fresh → 4 ms pooled; `eo_by_id` 382 ms → 124 ms (≈120 ms is the query itself). No `SELECT 1` health check (one more round trip per query); stale session retried once instead |
| B✓ | Phase gate: full eval, no metric drops > 5 points | ✅ | `scripts/eval.py` (62 cases) | `eval/results/20260927T1214.json` (rerank on): correctness 85% → 85%, faithful 100%, citations 100%, not-found 100%, refuse 100%, hit 93% → 91% (−2, within the 5-point rule). Changed cases: 37 and 54 better; 7 (table, judge 1.0 → 0.5) and 55 (200-row PCD list answered not-found) worse. Case 55 rerun 3× directly: 3/3 answered — model variance on large results, addressed by C8 |

## Phase C — Large database: schema RAG (v1.3.0) ✅

Goal: answer data questions beyond the 8 curated views by letting SQL generation see the few raw
tables a question needs, under a stricter guard. Order (ROADMAP): C1 → C5, then C7, C8, C6.

The eval misses the roadmap attributed to Phase C are not missing-table problems — the curated views
cover all three (checked 2026-09-27): **56** "How many TAL orders ship next week?" — the fixed tool
returns the count, the model calls it not-found (→ C7 templated answers); **60** a `both` question
where rows and documents each held half the answer and the model gave up; **61** a `both` question
routed to `data` only (router).

| ID | Task | Status | Check before ✅ | Evidence |
|---|---|---|---|---|
| C1 | `discover_schema.py --json` → `config/catalog/discovered/<db>.json` (tables, columns, PK, FK, rows, descriptions; optional samples) | ✅ | unit tests; live read-only run on both DBs | `tests/test_discovery.py` (25). Live 2026-09-27 (sys.* only, ~10 s): BitopiSplint 1,129 tables + 214 views, 22,698 columns, 160 FKs, 371 sensitive columns; Production 274 + 5, 50 FKs, 13 sensitive. Markdown mode still works. **Samples not run on production**: a full `--samples` pass = ~6,700 DISTINCT queries; to be done later only for tables the index selects |
| C2 | Catalog tiers: curated views + discovered tables, `exclude_tables`, sensitive/empty tables never offered | ✅ | unit tests; real catalogs load | `tests/test_catalog_tiers.py` (4). Real catalogs: `render()` byte-identical to v1.2.0 on both DBs (prompt unchanged until C5). Offered: BitopiSplint 1,069 of 1,343, Production 222 of 279 (sensitive, empty, backup/`$`/temp/test tables removed). Load 0.42 s |
| C3 | Schema index: hybrid table search + FK neighbours + join hints; `scripts/index_schema.py` | ✅ | unit tests; vectors built; live selection spot-check | `tests/test_schema_index.py` (7). Vectors built 2026-09-27: BitopiSplint 1,069 tables in 877 s, Production 222 in 147 s (one-off per discovery). `index_schema.py --check` on `tests/schema_cases.jsonl` (11 labelled questions): recall@6 **hybrid 10/11 (91%)**, keyword-only 9/11; a table-name signal lifted hybrid from 9/11. The miss, "List all buyer names", is honest: names live in `Contact_Master.ContactName`, which the curated `rag.vw_ExportOrder` already exposes as `Buyer` |
| C4 | Guard at scale: raw-table allow-list, no `SELECT *` on raw, WHERE on >1M-row tables, sensitive names, new deny tokens | ✅ | ~20 adversarial tests; existing guard tests green | `tests/test_guard_scale.py` (30: 7 allowed, 21 denied incl. the real "blood group … with number" probe, defaults, curated examples) + the 41 pre-C4 tests green. Found + fixed: the curated "orders per factory" few-shot example (GROUP BY + ORDER BY, no TOP) was always rejected by the guard — every imitation cost a failed attempt |
| C5 | SQL generation uses the selected tables (prompt v4) + full allow-list guard | ✅ | live data questions outside the curated views; data eval cases | `tests/test_schema_rag.py` (3). Live 2026-09-27, raw tables with no curated view: "How many suppliers…" → `dbo.SupplierData` 1,022; "suppliers evaluated in 2026" → `tblSupplierEvaluationMaster` 135; "sample indents in Sep 2026" → join over the 6.8M-row `tblSampleIndent` with a date filter → 0, **cross-checked** independently (last request 17 Aug 2026). Curated views still preferred when they cover it; "suppliers' phones and emails" returns nothing. Fixed along the way: the catalog's `@from/@to` rule produced unresolvable parameters (now literal dates); the router sent ERP questions beyond orders/PPM to documents; "pcs" was attached to record counts |
| C7 | Zero-LLM data answers: templated fixed-tool results, more fixed tools, clarification | ✅ | unit tests; eval | Templated answers ✅: `tests/test_templated.py` (10), `tests/test_fixed_tools.py` (9). Data + both eval cases (13): correctness **9/13 → 13/13** vs Phase B on the same cases (`eval/results/20260927T1307.json`); cases 55 and 56 answered with no model call. Also fixed: `eo_by_po` matched "IT **po**licy" as PO 'licy' (case 60), `both` questions with a policy noun missed the documents (case 61), multi-part answers collapsed to not-found (case 60). **More fixed tools** mined from `logs/chat.csv` (18 real questions): upcoming PCDs (all / by buyer / by factory), buyer list, cancelled orders (count / list) — live 0.1–0.5 s, **0 model calls**; "0 cancelled this month" cross-checked (none in Sep 2026; last one dated 5 Oct 2026 — a future date, worth flagging to the data owners). **Clarification**: `config/clarify.yaml` (which order? which factory?) asks instead of guessing; no golden question triggers it. 230 tests |
| C8 | Data UX: result table, CSV download, chart, follow-ups | ✅ | browser | `tests/test_present.py` (5). Browser 2026-09-27: "orders per factory this month" → table + bar chart (BGL/RHL/TAL) + Download CSV in the `[D1]` card; "What is the next PCD?" → table + CSV, no chart (7 columns); two turns, unique widget keys, no errors. Fixed: "Row key: ; ;" on aggregates; duplicate PDF-download keys when two turns cite the same chunk; a pointless 27.6 s rewrite call on short self-contained follow-ups (now skipped when a fixed tool matches). **Deferred**: per-tool follow-up suggestion chips |
| C6 | Pre-computed aggregates in local SQLite for heavy queries | ✅ | unit tests; live refresh | `tests/test_aggregates.py` (12). Live 2026-09-27: `pcd_history_by_eo` was **10.3 s + HYT00 timeout** (cold); `refresh_aggregates.py` copied 4,235,069 rows in 33–35 s (one read-only scan, 363 MB); lookups now 28–134 ms, **5/5 orders identical to live**. Found + fixed: order IDs are stored in mixed case (`tal-22-523-81` / `TAL-22-523-81`) and SQL Server matches both — the copy compared case-sensitively (7 vs 9 rows); columns are now `COLLATE NOCASE`. `docker-compose.yml` service `aggregates` refreshes every 24 h; README runbook added |
| C✓ | Phase gate: full eval, no metric drops > 5 points | ✅ | `scripts/eval.py` (62 cases) | `eval/results/20260927T1347.json` vs v1.2.0 on the same 62 cases: correctness **85% → 89%**, hit **91% → 98%**, faithful 100% → 98%, citations 100%, not-found 100%, refuse 100%. Better: 55, 56, 60, 61 (0 → 1). Worse: 6, 22, 33, 37 (1.0 → 0.5, document answers terser — follow-up below), 4 (faithful, judge noise: same answer as Phase A). `eval.py` fixed to compare on the same cases (it printed a false BLOCK) |

## Phase D — 60K pages (v1.5.0) ✅

| ID | Task | Status | Check before ✅ | Evidence |
|---|---|---|---|---|
| D1 | Keyword search on SQLite FTS5 (`chunk_fts` in `registry.db`), maintained per document; filters inside the search | ✅ | `tests/test_keyword.py`; `hit_rate.py` ≥ baseline | `tests/test_keyword.py` (9: codes, Bangla, per-document update/removal, filters, FTS-syntax safety, upgrade seeding, stopwords). `hit_rate.py` **29/30** (baseline). Live index seeded in 1.3 s; vs the old pickled BM25 on 30 questions: top-20 overlap 0.82, same top-1 26/30. **At 60K chunks**: rank_bm25 = 22.9 s full rebuild after every ingest, 691 MB RAM building, 291 ms/query; FTS5 = no rebuild, nothing held in the app, 17 ms/query (stopwords left out of the query: 57 → 17 ms, identical top-20). Also fixed: a re-ingested file that was already superseded came back visible (its fresh chunks were written unsuperseded and never re-marked) — verified live |
| D2 | Chroma at 60K: HNSW parameters; list filters pushed into the vector query | ✅ | `hit_rate.py`; filter test | **HNSW measured, left at defaults**: 60K vectors (real embeddings + noise), 50 queries vs exact search — Chroma defaults (M16/efC100/efS100) recall@20 **0.999**, 1.1 ms/query; the roadmap's M32/efC200 also 0.999 but 1.7 ms and slower builds, so no change and no reindex needed. **Filter pushdown**: category lists now go into the vector query as `$in` (they were applied after it, silently dropping most of the top-k); `tests/test_keyword.py` (2 new). For today's queries (only `superseded`) the Chroma filter is byte-identical, so hit_rate is unaffected |
| D3 | Ingestion throughput (parallel parse/OCR, batched embedding; ONNX int8 bge-m3 behind the parity test) | ✅ | throughput numbers; parity cosine ≥ 0.99 | **Profiled first**: embedding is 97% of ingest time (parse + tables ≈ 5 s per PDF), so a parallel parse pool would gain nothing. torch int8 dynamic quantization: 0.81× (slower) — rejected. **Batch size is the lever**: every text in a batch is padded to the longest; grid on 48 real chunks × 2 runs: batch 1 = 0.96 chunks/s, 32 = 0.52. Default now 1 on CPU / 32 on GPU (`EMBED_BATCH_SIZE`). Cold ingest of the 60-page manual **243.4 s → 126.5 s** (0.40 → 0.77 chunks/s); embeddings identical (cosine ≥ 0.9999999). Projection for 60K pages (~2.4 chunks/page ≈ 145K chunks) on this CPU: ~52 h once — a GPU (or ONNX, below) is the realistic path for the bulk load; revisions stay cheap via E3 |
| D4 | ONNX int8 reranker behind `RERANK_BACKEND=onnx` | ✅ | latency; `hit_rate.py` | **Done without new dependencies**: reranker batch 10 → 1 (same finding as D3): 10 pairs 18.7 s → 10.1 s; scores identical (max diff 1.3e-7, same order); `hit_rate.py` 29/30 in 387 s (the previous run did not finish within 600 s); document answers 18–32 s → 14–28 s. **ONNX int8** (approved 2026-09-27, reranker only): `scripts/export_reranker_onnx.py` (torch export + onnxruntime int8, 56 s → `data/models/bge-reranker-v2-m3-int8/`, 570 MB). `--check` on 6 questions × 10 real candidates: **12.5 s → 4.6 s (2.7×)**, same top-1 6/6, full order identical 3/6 (max score diff 0.149). `hit_rate.py` with `RERANK_BACKEND=onnx`: **29/30** (same miss) in 174 s. **Install incident**: `optimum` downgraded transformers 5.17 → 4.57 and huggingface-hub 1.32 → 0.36 (breaking sentence-transformers 6); reverted to the lock file, optimum removed, `pip check` clean, stored vectors reproduce at cosine 1.0000. Only `onnx` + `ml_dtypes` added |
| D✓ | Phase gate: full eval | ✅ | `scripts/eval.py` | `eval/results/20260927T1710.json` (62 cases, `RERANK_BACKEND=onnx`) vs v1.3.0 on the same cases: correctness 89% → **90%**, faithfulness 98% → **100%**, citations 100%, not-found 100%, refuse 100%, hit 98% → 96%. The only miss (case 61) was a one-off Gemini rate-limit (429 → the friendly quota message, as designed); re-run: `both` cases 3/3. Cases 2, 22, 33, 37 are complete again (v1.3.0's "terser answers" was run-to-run variance). ONNX made the default in `.env`; app check: "Who must approve a new user account request?" **10.1 s** (28.0 s with PyTorch earlier today, 130–160 s at the start of the day) |

## Phase E — Freshness ✅

| ID | Task | Status | Evidence |
|---|---|---|---|
| E1 | Ingestion worker (`scripts/ingest_worker.py`) | ✅ | shipped in v1.1.0 |
| E2 | Hot reload of new PDFs (`index_version.py`) | ✅ | shipped in v1.1.0 |
| E3 | Embedding cache + chunk-level diff | ✅ | `tests/test_embedding_cache.py` (6). Live 2026-09-27 in a throwaway index, 60-page IT SOP manual (98 chunks): cold ingest **243 s** (98 embedded) → one-page revision **6.9 s** (1 embedded, 97 cached, 1 stale row pruned) → unchanged re-run 0.0 s; revised text indexed on p12; vectors = registry chunks = 98. **Decision:** no chunk-level diff in the vector store — with cached vectors, replacing a document's rows costs milliseconds and keeps every chunk's metadata (`doc_hash`, `ingested_at`) consistent, which a diff would leave stale. Upgrade path: an index built before E3 seeds the cache from its stored vectors on the first ingest (live index 2026-09-27: 250/250 chunks seeded, 0 re-embedded) |
| E4 | Document the "new version of a document" behavior in README | ✅ | README "Revising a document" + "Add documents". Claims verified live in the throwaway index: `_v3` beside `_v2` → v2 superseded in registry and vectors, added in 6.4 s with 0 chunks embedded (all cached); deleting `_v3` → v2 restored |

## Plan after v1.5.0 — Phases F–I

Merged on 2026-09-28 from two sessions' proposals, with the other session's plan first (F: ready for
more users, G: polish) and this session's code findings after it (H: ingestion robustness, I: speed).
One exception agreed with the user: the empty-folder wipe fix (H0) goes first as a hotfix, so the
ingestion worker can run unattended while F1 is built. Design per task: `docs/ROADMAP.md`
"Phases F–I". Order: H0 → F4 (request drafted, DBA works in parallel) → F1 → F2 (when the DBA is
done) → G1–G4 → H1–H4 → I1 → I2–I5.

### Hotfix H0 — cautious deletes (v1.5.1) ✅

| ID | Task | Status | Check before ✅ | Evidence |
|---|---|---|---|---|
| H0 | PRD FR-2.14: a missing or empty PDF folder deletes nothing; a file leaves the index only after **two consecutive scans** miss it; more than 5 % of known files missing in one scan → no deletions, alert in `ingest.log` + `ingest_state.json`. Same area: one unreadable file no longer aborts the whole run (the worker exited); the summary's `failed` counts this run, not the registry total | ✅ | unit tests (missing root, empty root, two-scan rule, counter reset, > threshold, unreadable file); live `ingest_worker.py --once` against a throwaway index with its folder renamed → alert, `inspect.py --stats` unchanged | `tests/test_cautious_deletes.py` (9): missing folder, empty folder (3 passes: nothing removed, nothing counted), two-scan removal, counter reset when a file comes back, 4 of 5 files gone → nothing removed until `--confirm-removals`, `--confirm-removals` never empties from an empty folder, unreadable known/new file, per-run `failed`, pre-H0 registry migrated. **Live 2026-09-28** in a throwaway clone of the live index (real Chroma + registry, embedder stubbed to fail if called): folder renamed → alert, 250 vectors kept; Policy Book deleted → scan 1 pending (250 vectors), scan 2 removed (98); folder emptied → alert, 98 kept; a Bangla-named copy of the SOP manual added from the embedding cache (98 cached, 0 embedded) and logged in UTF-8. `reindex.py --yes` against a missing folder → exit 2, index untouched (it used to delete first). Real `ingest_worker.py --once` on the live index: "no change", exit 0, stats unchanged (2 documents, 250 chunks). `hit_rate.py` **29/30** (same miss as v1.5.0; the run took ~20 min on a busy machine vs 174 s on 27 Sep — retrieval code unchanged). Also fixed: `ingest.log` was written in the Windows code page (a Bangla file name could not be logged); `reindex.py` deleted the index before checking the folder; the worker absorbed a change that arrived while the index was locked. Full eval not run: no retrieval or answer code changed. 282 tests |

### Phase F — Ready for more users (v1.6.0) ⬜

| ID | Task | Status | Check before ✅ | Evidence |
|---|---|---|---|---|
| F4 | DBA request: `rag.*` views carrying a `Factory` column and the SESSION_CONTEXT scope predicate (PRD FR-4.4), `rag_reader` login, `IX_ExportOrderBack_ExportOrderID`. Written to the git-ignored `private/` folder (the repo is public) and sent by the user | ✅ | the user has the document | Sent to the user 2026-09-28: `private/DBA_REQUEST.md`, `rag_reader_option_a.sql` (SELECT + DENY on 31 sensitive tables and 343 sensitive columns in 122 objects, from discovery), `rag_reader_option_b.sql` (schema `rag` only), `docs/schema/rag_views_*.sql`. **Every view now has a factory column** (`scope_column:` in the catalog; 5 views gained one through joins to FileRef / PPMMeetings): `check_catalog.py --live` 0 problems; every row resolves to a factory (0 missing; full count on the 3 smaller views, 20–50K-row samples on the large ones); the history view's join costs nothing warm (0.32 s vs 0.37–0.51 s). The generated views filter on `SESSION_CONTEXT(N'rag_factories')` (`src/ragbot/data/scope_sql.py`, CHARINDEX so any compatibility level works); proven on the real engine with plain SELECTs (session value replaced by a literal): `TAL,RHL` → only those factories on all 8 views, `*` → all, unset → 0 rows. `tests/test_scope_sql.py` (3). Waiting on the DBA → F2 |
| F1 | Login with AD username + password (LDAP) and per-user scoping (PRD FR-4.1, 4.3, 4.5–4.8): scope is a pre-filter in vector and keyword search, a view-level predicate on every data query, part of both cache keys, and logged with every request; admin details for admins only | ⬜ | unit tests; `eval.py --kind scope` **0 leaks** on ≥ 30 adversarial cases; browser: two users with different factories get different documents/rows for the same question; full eval, no drop > 5 | |
| F2 | Switch SQL Server from `sa` to `rag_reader` once the DBA delivers F4 (config + catalog: `definition:` fields removed) | ⏸ | `db_ping.py` shows `rag_reader`; `check_catalog.py --live`; data eval cases | Blocked on the DBA |
| F3 | Gemini billing: a paid key. PRD FR-8.9 forbids free-tier keys with company data, so this is needed now, not only at rollout | ⏸ | one `ask.py` call logged in `calls.csv`; an eval run without 429s | User action |
| F✓ | Phase gate | ⬜ | full eval, no drop > 5 points; scope leaks 0 | |

### Phase G — Polish (v1.7.0) ⬜

| ID | Task | Status | Check before ✅ | Evidence |
|---|---|---|---|---|
| G1 | Raw-table codes → names (e.g. `tblSampleRequestMaster.Buyer` = `C/09/7`): an optional `hints:` key in the catalog YAML feeding the schema index's join hints | ⬜ | read-only check that the code is a `Contact_Master.ContactID`; unit test; `index_schema.py --try` shows the join | |
| G2 | `discover_schema.py --samples` only for the tables the schema index selects (a full pass is ~6,700 production queries); samples merged into the JSON, not wiped by the next run | ⬜ | unit test; live run only with the user's go-ahead (read-only production queries) | |
| G3 | Follow-up question chips per fixed tool; `example_params` on every fixed tool so `check_catalog.py --live` smoke-tests them (it tests none today) | ⬜ | unit test; browser | |
| G4 | Streamlit noise: `.streamlit/config.toml` (also copied into the Docker image), deprecated `use_container_width`; `chat.csv` header on disk is missing the `error_kind` column | ⬜ | clean startup log; `chat.csv` parses with a header that matches its rows | |
| G✓ | Phase gate | ⬜ | full eval, no drop > 5 points | |

### Phase H — Ingestion robustness (v1.8.0) ⬜

| ID | Task | Status | Check before ✅ | Evidence |
|---|---|---|---|---|
| H1 | Two PDFs with the same file name in different folders overwrite each other (`source` is the bare name): the second is refused and listed as failed | ⬜ | unit test | |
| H2 | `inspect.py --check` (registry vs Chroma vs `chunk_fts`, exit 1 on mismatch); a failed re-ingest keeps the old version (today the vectors are deleted first and the old registry rows stay) | ⬜ | tests with a planted orphan in each store; exit 0 on the live index | |
| H3 | Worker: `ingest.py` and `reindex.py` take the same lock; resume after a crash; failed files retried up to 3 times; `ingest_state.json` written atomically and only when something changed (today every `--once` pass flushes the app's caches) + a heartbeat file; sidebar status line. Found in H0: every `--once` pass loads the 2 GB embedding model (and contacts huggingface.co) even when nothing changed — load it only when a file needs embedding | ⬜ | tests; kill the worker mid-batch → restart → `--check` clean | |
| H4 | Run the worker and the nightly aggregates refresh as scheduled tasks (runbook in README; the tasks are created by the user). The local copy was last refreshed 27 Sep 14:59 | ⏸ | `schtasks /query` lists both; aggregates `as_of` is from last night | User action |
| H✓ | Phase gate | ⬜ | full eval, no drop > 5 points | |

### Phase I — Speed (v1.9.0) ⬜

| ID | Task | Status | Check before ✅ | Evidence |
|---|---|---|---|---|
| I1 | Measure: per-stage timings and a request id in the logs; `scripts/latency.py` prints p50/p95 per stage, end-to-end and first token over 50 questions | ⬜ | baseline row in `docs/tuning_log.md` | |
| I2 | Document answers p50 ≤ 8 s (about 10 s today) | ⬜ | `latency.py`; `hit_rate.py` ≥ 29/30; eval | |
| I3 | First token ≤ 3 s (3.9–6.1 s measured in B2), or the provider limit measured and recorded | ⬜ | `latency.py` first-token column | |
| I4 | Load test: chat p95 rises < 20 % while the worker ingests 100–1,000 PDFs; throughput recorded | ⬜ | `latency.py` during an ingest in a throwaway index | |
| I5 | Bulk ingestion: `embed_backend` stored with the index and checked at start; int8 ONNX bge-m3 behind the parity test (needs the user's approval, as D4 did) | ⬜ | parity cosine ≥ 0.99 on 1,000 chunks; `hit_rate.py` drop ≤ 1 | |
| I✓ | Phase gate | ⬜ | full eval, no drop > 5 points | |

## Small follow-ups noticed along the way

The open follow-ups from Phases A–E are now tasks above: torchvision noise → G4, follow-up chips →
G3, `--samples` for selected tables → G2, raw-table code → name → G1, Gemini rate limits → F3.

- ~~Data answers wrongly reported as not-found (a count of 0, a 200-row list)~~ — fixed in v1.3.0 by
  C7 templated answers (eval cases 55, 56).
- ~~Document answers got terser in v1.3.0~~ — run-to-run variance: cases 22, 33, 37 (and 2) were
  complete again in the v1.5.0 gate with the same prompts. Watch it, no change needed now.

## Open items outside the code

- **Data anomaly to report**: `dbo.CancellExportOrderList` has a cancellation dated **5 Oct 2026**
  (in the future on 2026-09-27).
- **The GitHub repo is public.** It already shows internal schema details (`docs/schema/*.sql`,
  `config/catalog/*.yaml`) and that the app connects as `sa`. Recommended: make it private. New
  internal-system documents (F4) are kept in the git-ignored `private/` folder.
- Gemini billing → F3; DBA request → F4/F2; auth and per-user scoping → F1.
- **Unit codes without a name** (found in F4): the data also holds orders and files of units with raw
  company codes `01`, `02`, `03`, `07` (no TAL/RHL/... short name in the view mapping). A user limited
  to named factories never sees them; to scope users of those units, IT must say which units they are.
- Not planned yet: buyer portal accounts (PRD FR-4.2, PRD Phase 3) and MySQL row scope; the
  chunk-id / `source` format for same-name files (H1 refuses the duplicate instead — changing the
  format is a decision); choosing the production LLM provider (golden-set eval across providers).

## Session log

| Date | What happened |
|---|---|
| 2026-09-22 | v1.0.0: M0–M6 pilot complete |
| 2026-09-23 | v1.1.0: Phase A done, eval correctness 85% |
| 2026-09-27 | Phase A merged to `main`; PRD v1.2 spec committed; tags v1.0.0/v1.1.0; tracking docs added; Phase B started |
| 2026-09-27 | v1.2.0: Phase B done (B1–B6 + gate), merged to `main`, tagged. Phase C drafted (C1–C5 in working tree) |
| 2026-09-27 | v1.3.0: Phase C done (C1–C8 + gate; correctness 85% → 89%), merged to `main`, tagged. E3 next |
| 2026-09-27 | Aggregates refreshed (4.2M rows, 32 s). v1.4.0: E3 embedding cache (one-page revision 243 s → 6.9 s) + E4 README; live index seeded; hit_rate 29/30. D1 next |
| 2026-09-27 | v1.5.0: Phase D done (D1 FTS5, D2 filter pushdown, D3/D4 batch size + int8 ONNX reranker; correctness 90%, faithful 100%). Document answers ~10 s. Roadmap A–E complete |
| 2026-09-28 | Two sessions' plans merged into Phases F–I (H0 hotfix first, then F → G → H → I), written here and in ROADMAP. GitHub Releases created for v1.2.0–v1.5.0 (Phases B–D had tags only). H0 next |
| 2026-09-28 | v1.5.1: hotfix H0 — an empty or offline PDF folder can no longer empty the index (two-scan removal rule, mass-disappearance stop, `reindex.py` checks the folder first). F4 next |
