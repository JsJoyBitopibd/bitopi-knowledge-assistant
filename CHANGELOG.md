# Changelog

All notable changes to the Bitopi Knowledge Assistant. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Each released version has a git tag (`vX.Y.Z`).

Where things stand right now (done / in progress / next) is in `docs/PROGRESS.md`. The design behind
each phase is in `docs/ROADMAP.md`.

## [Unreleased]

### Added
- I1: every answer carries a request id and per-stage timings (`src/ragbot/trace.py`). The id is written
  to `chat.csv`, `calls.csv` and `sql.csv`, so one question can be followed through all three.
  `chat.csv` also records the seconds per stage, and administrators see them under each answer.
  `scripts/latency.py` prints p50/p95 per stage, end to end and to the first token, over 51 fixed
  questions, each run cold.

- I3: while an answer is being written, the progress line names its sources ("Reading: Bitopi_IT_SOP_Manual_v2
  p. 12; …") as soon as the search is done, at 2.1 s (p50). The provider sends each answer in one burst, so
  the first word cannot come sooner than about 5.7 s.

### Changed
- I2: 8 rerank candidates instead of 10, and a reranker window of 384 tokens instead of 512 (hit rate
  unchanged, 29/30): reranking 3.2 s → 1.9 s. Document answers 7.45 s → 5.71 s (p50).
- I4: the ingest scripts give the CPU to the chat app: below-normal priority, 4 embedding threads and a
  pause while the app is searching (`ingest.priority`, `ingest.embed_threads`, `ingest.yield_to_app` in
  `config/settings.yaml`). While the worker ingested, the app's document search slowed by 131% (p95);
  now by 12%.
- I2: the question embedding went from 0.46 s to 0.09 s (p50). onnxruntime no longer keeps its threads
  spinning after a rerank, and torch no longer uses every logical CPU. Ingestion is 54% faster for the
  same reason: 16 threads were slower than 8.
- I1: `chat.csv`, `calls.csv` and `sql.csv` gained columns (`request_id`; `chat.csv` also `seconds` and
  `timings`). The first write after the upgrade renames each old file to `<name>.<timestamp>.csv`, as for
  earlier header changes.

### Fixed
- I2: Windows power throttling (EcoQoS) ran the models 6–9× slower in processes it treats as background —
  the app as a service, the worker, the scripts. Each of them now opts out when it loads the models.

## [1.8.0] — 2026-09-29 — Phase H: ingestion robustness

Eval (62 cases, `eval/results/20260929T0919.json`, vs v1.7.0 on the same cases): hit rate 98% → 96%,
correctness 92% → 91%, faithfulness, citation validity, not-found and refuse 100%. The hit-rate dip is
case 28, whose answer call got the provider's per-minute quota error (429); asked again on this code it
gives v1.7.0's answer and cites the expected page. Case 2 lost a second sentence (run-to-run variance,
as in v1.3.0); case 37 went back to 1.0. Leak suite: all 34 cases, including the 7 that need the model,
0 leaks (`eval/scope/`). H4 (the scheduled tasks) is the operator's step; the README has the commands.

### Added
- H2: `scripts/inspect.py --check` compares the registry, the keyword index and the vector store (orphans
  each way, superseded flag and access tags that differ from the document's, half-written documents)
  and exits 1 on any mismatch; `scripts/ingest.py --redo <file>` rewrites a document from the embedding
  cache to repair it.
- H3: failed files are retried on the next two passes, then left until they change
  (`inspect.py --failed` shows the attempts and whether the previous version is still served). The
  sidebar shows when the folder was last checked, and administrators see files that failed or were
  skipped.
- H4: README runbook for running the worker and the aggregates refresh as scheduled tasks.

### Changed
- H3: a pass that finds nothing new takes seconds. Unchanged files (same path, size and modification
  time) are not read, and the embedding model loads only when something needs embedding.
  `ingest_state.json`, whose change makes the app drop its caches, is rewritten only when the index
  changed; every pass writes `data/index/last_scan.json` instead.
- H3: `scripts/ingest.py` and `scripts/reindex.py` take the worker's index lock (exit 3 while another
  ingest writes). The holder refreshes the lock every minute, so a long bulk ingest is never taken over,
  and a lock whose process died is taken over at once.

**Upgrading:** run `python scripts/ingest.py` (or let the worker pass) once. It records where each indexed
file lives, which the download button now needs. Nothing is re-embedded.

### Fixed
- H1: two PDFs with the same file name in different folders overwrote each other in the index. The copy
  already indexed is kept and the other is skipped and reported until one is renamed. The PDF download
  button found files by name and could serve the skipped copy from a folder the user may not see. It now
  reads the exact path that was indexed.
- H2: a failed update of a document deleted its vectors first, leaving the old version half-searchable.
  The new version is now parsed and embedded in full before the old one is replaced. A pass killed while
  writing a document is redone by the next pass (verified with hard kills).
- H3: a PDF that failed to open stayed locked by the ingest process (the logged error held pymupdf's
  file handle), so on Windows nobody could replace or delete it. A failed file was never retried until
  it changed. A moved file kept its old category. The sidebar's category list, status line and PDF
  bytes ignored the index version and refreshed only every 5–10 minutes.

## [1.7.0] — 2026-09-29 — Phase G: polish

Eval (62 cases, `eval/results/20260929T0040.json`, vs v1.6.0 on the same cases): hit rate 98% → 98%,
faithfulness 100%, correctness 93% → 92% (case 37: 1.0 → 0.5, a case that has swung between runs before),
citation validity, not-found and refuse 100%. Leak suite on this code: the 27 cases that need no model call,
0 leaks (`eval/scope/scope_20260929T0042.json`). G2's live sampling run (about 122 read-only queries) is
still to do, with the user's go-ahead.

### Added
- G3: follow-up question chips under fixed-tool answers (`follow_ups:` in `config/fixed_tools.yaml`,
  filled with the answer's parameters; each reaches a fixed tool, so a click needs no model call). Every
  fixed tool has an `example:` question: `check_catalog.py` checks it reaches that tool and `--live`
  runs all of them (it ran none before).
- G1: catalog `hints:` for code columns in raw tables — a `Buyer` code such as `C/09/7` points the SQL
  model at `dbo.Contact_Master.ContactName` (listed per table: the same column name means different
  things elsewhere).
- G2: `discover_schema.py --samples --tables-from-logs` samples only the raw tables the SQL model has
  needed (logged in `logs/schema_select.csv`; about 122 queries instead of ~6,700); earlier samples are
  kept when discovery runs again.

### Fixed
- G4: Streamlit's file watcher logged ~50 torchvision tracebacks at every start; it is off in
  `.streamlit/config.toml` (also in the Docker image). `st.dataframe` no longer uses the deprecated
  `use_container_width`.

## [1.6.0] — 2026-09-28 — Phase F: sign-in and per-user scope

Eval (62 cases, `eval/results/20260928T1820.json`, as an unrestricted user, vs v1.5.0 on the same cases):
correctness 90% → **93%**, hit rate 96% → **98%**, faithfulness 100%, citation validity 100%, not-found
100%, refuse 100%. Leak suite (`scripts/scope_check.py`): 34 adversarial cases, **0 leaks**. F2 (switch to
`rag_reader`) waits for the DBA; F3 (Gemini billing) for the user.

**Upgrading an existing installation:** copy `config/scopes.example.yaml` to `config/scopes.yaml` with the
real AD groups, set `auth.provider` and the `LDAP_*` values, then run `python scripts/ingest.py` once to tag
the documents already indexed (nothing is re-embedded). Until then, factory-limited users see no documents.

### Added
- **F1: sign-in and per-user scope** (PRD FR-4.1, 4.3–4.8, 6.2). Users sign in with their Active
  Directory account (LDAPS/StartTLS; `auth.provider: local` with bcrypt users for development); AD groups
  map to a scope in `config/scopes.yaml` (factories, departments, highest confidentiality; union over
  groups; not in a group = cannot sign in). Documents carry factory / department / confidentiality /
  buyer tags (first folder name, or a folder's `meta.yaml`), stored in the registry, the vector store and
  the keyword index; changing a tag retags in place without re-embedding. The scope is a required
  argument of every search and query: it filters the vector query and the keyword search, every SQL view
  is emitted filtered to the user's factories inside itself and the final statement is re-checked before
  it runs, raw tables and unscoped views are unavailable to factory-limited users, a fixed tool naming
  another factory is refused at once, the nightly copy is read through the same filter, and both caches
  are keyed on the scope. Signing in or out clears the conversation; admin details are admin-only; admins
  get an access and audit panel. `chat.csv` and `sql.csv` record every request's scope.
  `scripts/scope_check.py` + `tests/scope_cases.jsonl`: 34 adversarial cases, 0 leaks.
- F4: every catalog view declares `scope_column:` (its factory column); the five views without one
  (`vw_ExportOrderColorSize`, `vw_PCDChangeHistory`, `vw_CancelledExportOrder`,
  `vw_PPMMeetingReschedule`, `vw_PPMDepartmentChecklist`) gained a `Factory` / `FactoryID` column
  through joins. `scripts/gen_rag_views.py` generates the DBA's views with the per-user row filter on
  `SESSION_CONTEXT(N'rag_factories')` (`src/ragbot/data/scope_sql.py`); `check_catalog.py` flags a view
  without a scope column. The DBA request and the `rag_reader` permission scripts are kept in the
  git-ignored `private/` folder, because the repository is public.

### Fixed
- **The SQL guard accepted a name used before its CTE was defined.** In `WITH a AS (SELECT … FROM
  ExportOrder), ExportOrder AS (…) SELECT … FROM a`, SQL Server binds `ExportOrder` inside `a` to the real
  `dbo.ExportOrder` (a CTE can only use CTEs declared before it), so generated SQL could read any base
  table past the view allow-list, the raw-table rules and the user's scope. A bare name now counts as a
  CTE only where SQL binds it to one, a WITH inside a subquery is refused, and the scope check re-verifies
  every table of the final statement. Found by the F1 review; present since the guard was written.
- A retag or superseded flag that failed half-way (registry updated, vector store not) was never retried,
  leaving the vector store with the old, broader tags; the store is now updated first.
- `logs/chat.csv` had 10 header columns and 11 per row; every log now goes through one writer that renames
  a file with an older header aside.
- `scripts/inspect.py --search` would have failed now that a scope is required.
- `eval.py` compared a run with whatever file sorted last in `eval/results/` (the gate run crashed on a
  leak-suite report); it now compares only with earlier eval runs, `--compare <file>` re-compares a saved
  run, and a judge call the provider cannot answer leaves the case unscored instead of losing the run.
  Leak-suite reports moved to `eval/scope/`.

## [1.5.1] — 2026-09-28 — Hotfix H0: cautious deletes

### Fixed
- **An empty or offline PDF folder emptied the index.** Every ingest pass deleted each indexed file
  it did not see, and a missing folder simply yields no files, so one pass over an unmounted share
  removed every document — and the worker ran that pass by itself as soon as the folder changed. Now
  (PRD FR-2.14): a missing or empty folder changes nothing; a file leaves the index only when two
  consecutive passes miss it; if more than 5 % of the indexed files (and more than 3) vanish in one
  pass, nothing is removed. Each case logs an `ERROR`, puts an `alert` in `ingest_state.json`, shows
  "⚠ Index not updated" in the app's sidebar, and makes `ingest.py` / `ingest_worker.py --once`
  exit with code 2.
- `scripts/reindex.py` deleted the index before looking at the PDF folder; it now refuses (exit 2)
  when the folder has no PDFs.
- One unreadable PDF (locked by another program, no permission) aborted the whole ingest run and
  stopped the worker. It is now a failed file and the run continues; an already indexed file that
  cannot be read keeps its indexed version.
- The ingest summary's `failed` showed the registry-wide total; it is now this run's count
  (`failed_total` holds the total).
- `logs/ingest.log` was written in the Windows code page, so a Bangla file name could not be logged;
  it is UTF-8 now.
- The worker absorbed a folder change that arrived while the index was locked by a manual ingest; that
  pass is now retried at the next interval.

### Added
- `python scripts/ingest.py --confirm-removals` for a deliberate bulk removal; settings
  `ingest.removal_alert_fraction` (0.05) and `ingest.removal_alert_min_files` (3); registry column
  `document.missed_scans` (added automatically to existing registries). README "Remove documents".

### Docs
- Plan for Phases F–I (after v1.5.0) in `docs/PROGRESS.md` and `docs/ROADMAP.md`, merged from two
  sessions' proposals: hotfix H0 (an empty or offline PDF folder must not empty the index), F ready
  for more users (login + per-user scoping, `rag_reader`, billing, DBA request), G polish, H ingestion
  robustness, I speed. The open follow-ups and open items from Phases A–E are now tasks in it.

## [1.5.0] — 2026-09-27 — Phase D: 60K pages

Eval (62 cases, `RERANK_BACKEND=onnx`, `eval/results/20260927T1710.json`, vs v1.3.0 on the same cases):
correctness 89% → **90%**, faithfulness 98% → **100%**, citation validity 100%, not-found 100%, refuse
100%, hit rate 98% → 96% (the one miss was a Gemini rate-limit; it passes on re-run). Document answers
in the app: ~10 s (130–160 s before v1.1.0).

### Added
- `RERANK_BACKEND=onnx` is now set in `.env.example` (export the model first); `docker-compose.yml`
  mounts `data/models`; README "Faster reranking" runbook.

### Changed
- D1: keyword search moved from a pickled in-memory `rank_bm25` index (rebuilt in full after every
  ingest) to the `chunk_fts` SQLite FTS5 table in `registry.db`, updated per document by the registry.
  Category and superseded filters are applied inside the keyword search. English stopwords are left
  out of the query. At 60K chunks: no 22.9 s rebuild, no 691 MB in memory, 291 → 17 ms per query.
  Existing indexes are seeded once from the vector store on first use; `bm25.pkl` is no longer read.
- D2: category filters are pushed into the vector query (`$in`) instead of being applied after it,
  so every one of the top-k vector hits is usable. HNSW settings were measured at 60K vectors and
  left at Chroma's defaults (recall@20 0.999; the roadmap's larger graph gained nothing).
- D3/D4: the embedder and the reranker run in small batches on CPU (`EMBED_BATCH_SIZE`,
  `RERANK_BATCH_SIZE`; default 1 on CPU, 32 on GPU). Padding every text to the longest in a batch
  made batch 32 about half as fast. Cold ingest 243 s → 127 s for the 60-page manual; reranking 10
  candidates 18.7 s → 10.1 s; document answers 18–32 s → 14–28 s. Results unchanged (scores within
  1.3e-7, same order).
- D4: int8 ONNX reranker (`RERANK_BACKEND=onnx`, `OnnxReranker`), exported by
  `scripts/export_reranker_onnx.py` to `data/models/` (git-ignored). 2.7× faster than the PyTorch
  reranker (12.5 s → 4.6 s for 10 candidates), same top result on 6/6 questions, hit rate 29/30.
  New dependency: `onnx` (export only). `optimum` is deliberately not used: its current release pins
  transformers < 4.58.

### Fixed
- `embed.py` read `EMBED_MODEL`, `RERANK_MODEL` and `RERANK_BACKEND` without loading `.env`, so a
  script importing it directly ignored `.env` (the app worked only because it imported `config` first).
- Re-ingesting a file that was already superseded made it visible again: its fresh chunks were
  written as not superseded and never re-marked, because the document row had not changed.

## [1.4.0] — 2026-09-27 — Phase E3–E4: cheaper re-ingest

Ingestion-only release (the answer path is unchanged): 261 tests; retrieval `hit_rate.py` 29/30
(reranker on), same as v1.1.0–v1.3.0.

### Added
- E3: embedding cache in `registry.db` (`embedding_cache`, keyed by SHA-256 of the chunk text + model):
  a re-ingested document embeds only chunks whose text changed. 60-page manual: cold 243 s, one-page
  revision 6.9 s (1 embedded / 97 cached). `logs/ingest.log` and `ingest_state.json` report
  `embedded` / `cached` / `cache_pruned`; unused cache rows are pruned after updates and removals.
  `chunk.text_hash` column (older registries are migrated on open). An index built before E3 seeds
  the cache from the vectors already in the store on its first ingest — nothing is re-embedded
  (live index: 250 of 250 chunks seeded).
- E4: README "Revising a document": replace in place vs. add `…_v3.pdf` beside `…_v2.pdf`
  (older revision superseded and hidden from answers), and how the running app picks either up.

### Changed
- `ingest_folder()` accepts a store, registry and index folder (default: the app's), and
  `rebuild_keyword_index()` an index folder, so an ingest can run against a throwaway index.

## [1.3.0] — 2026-09-27 — Phase C: large database, schema RAG

Eval (62 cases, reranker on, `eval/results/20260927T1347.json`, vs v1.2.0 on the same cases):
correctness 85% → **89%**, hit rate 91% → **98%**, faithfulness 100% → 98%, citation validity 100%,
not-found 100%, refuse 100%. Data + both cases 9/13 → 13/13. Known cost: four document answers
are terser (cases 6, 22, 33, 37; see `docs/tuning_log.md`).

### Added
- C1: `scripts/discover_schema.py --json` writes `config/catalog/discovered/<db>.json` (git-ignored):
  every table and view with columns and types, primary and foreign keys, row counts and
  `MS_Description` text, from `sys.*` only. Optional `--samples` reads up to 30 distinct values of
  short text columns in small tables, never from a sensitive column. `data/sensitive.py`: one
  word-based definition of sensitive names (`EmpNID`, `DOB`, `BasicSalary` yes; `ManID` no).
- C2: catalogs have two tiers: the curated views (always in the prompt) and the discovered tables
  (`Catalog.tables`, rendered only when selected via `render_selected()`, sensitive columns never
  shown, at most 40 columns each). `exclude_tables:` patterns in the catalog YAML drop history
  copies, spreadsheet imports and scratch tables; sensitive and empty tables are never offered.
- C3: schema index (`data/schema_index.py`): picks the few discovered tables a question needs by
  merging vector search (bge-m3, precomputed by `scripts/index_schema.py` into
  `data/index/schema/<db>.npz`), BM25 over table documents, and a table-name match, then adds
  foreign-key neighbours and likely joins (shared key names). Works keyword-only when the vectors
  are missing or stale. `index_schema.py --check` measures recall on `tests/schema_cases.jsonl`:
  10/11 at k=6.
- C4: the SQL guard can allow raw discovered tables (`allowed_tables`, the catalog's full offered
  set) while forbidding `SELECT *` on them and requiring a `WHERE` on tables over
  `data.big_table_rows` (1M). Always on, for every query: no column or table with a sensitive name,
  and no `FOR XML/JSON`, `@@` globals, `SUSER_*`, `SYSTEM_USER`, `HOST_NAME`, `ORIGINAL_LOGIN`.
- C5: generated SQL can use raw tables. Per question, the selected discovered tables and likely joins
  go in a `{tables}` section after the static catalogs (stable prompt prefix), and the guard checks
  against each catalog's full offered set. Settings `data.schema_rag`, `data.schema_rag_k`,
  `data.big_table_rows`. Answers questions no curated view covers, e.g. supplier counts.
- C7 (part): fixed-tool results are answered from templates with no model call
  (`agent/templated.py`; `answer:` / `answer_list:` in `config/fixed_tools.yaml`): a count as one
  sentence, rows as a Markdown table, still checked by `verify()`. A date window keeps its phrase
  ("next week") as a parameter. Data + both eval cases: 9/13 → 13/13 correct.
- C7: six fixed tools mined from real questions in `logs/chat.csv` — upcoming PCDs (all, per buyer,
  per factory), the buyer list, cancelled orders in a window (count, list). Answered in 0.1–0.5 s
  with no model call. `config/clarify.yaml`: when a data question names no order ("status of the
  order") or no factory ("how many PPM meetings this week"), the assistant asks which one instead of
  guessing (route `clarify`).
- C8: database answers show their rows inside the `[D#]` reference card: a sortable table, a bar
  chart when the result is a label → number list (2–25 rows), and a Download CSV button (UTF-8 with
  BOM, so Excel shows Bangla correctly). `Answer.results` carries the rows; `data/present.py`
  decides table / chart / file name.
- C6: pre-computed aggregates. `config/aggregates.yaml` + `scripts/refresh_aggregates.py` copy
  results too slow to compute per question into `data/index/aggregates.db` (one read-only streaming
  scan, `guard.assert_read_only`, atomic swap). `pcd_history_by_eo` now answers from the copy:
  10 s with timeouts → 28–134 ms; "Refresh data" or a missing copy runs it live with the 30 s heavy
  timeout. `docker-compose.yml` gains an `aggregates` service (every 24 h); README runbook.
- Each `[D#]` source now shows the query's filter (factory, dates, order id) to the answering model
  and in the judge's view; `verify()` also accepts the row count and parameter values.

### Changed
- Prompts v5–v6 (`prompts/CHANGELOG.md`): SQL values as literals (only six date parameters exist);
  the router treats all ERP records as data; "pcs" only for garment quantities; multi-part answers
  answer the covered parts instead of replying not-found.

### Fixed
- `eval.py` compared a `--kind` subset run with the previous run's totals and printed false BLOCKs;
  it now compares with the most recent earlier run covering the same cases, scored on those cases.
- Aggregate results showed "Row key: ; ;" (the view's key columns are not in a GROUP BY result).
- Two chat turns citing the same PDF chunk created duplicate download-button keys (a Streamlit error).
- A short, self-contained follow-up ("What is the next PCD?") was sent through the rewrite model call;
  one such call took 27.6 s. Skipped now when a fixed tool already matches the question.
- `eo_by_po` matched "IT **po**licy" as PO number 'licy', hijacking any question that mentioned a
  policy and an order (eval case 60). "PO" must now be a whole word and the value contain a digit.
- `both` questions with an order code and a password/IT-policy noun were routed to `data` only
  (eval case 61).
- The catalog rule "col >= @from AND col < @to" taught generated SQL parameters that do not exist
  (only six date names are resolved), so such queries always failed.
- The curated "orders per factory shipping this month" few-shot example had `GROUP BY … ORDER BY`
  without `TOP`, which the guard always rejects, so any generated query imitating it failed once
  and cost an extra LLM call. It now has `TOP (50)`, and a test keeps every example guard-clean.
- `discover_schema.py` read the connection string from `os.environ` before `.env` was loaded.

## [1.2.0] — 2026-09-27 — Phase B: streaming, parallelism, caching

Eval (62 cases, reranker on, `eval/results/20260927T1214.json`): correctness 85% (unchanged),
faithfulness 100%, citation validity 100%, not-found 100%, refuse 100%, hit rate 93% → 91%.
Document answers 130–160 s before v1.1.0 → 18–32 s; repeat questions 0.1 s; pooled SQL 266 ms → 4 ms.

### Added
- B1: `ChatModel.stream()` yields text deltas then a final `ChatReply`; the OpenAI-compatible
  adapter streams with usage included; non-streaming adapters fall back to one delta. One
  `calls.csv` row per stream.
- B2: `orchestrator.answer_stream()` yields `Stage` / `Token` / `Replace` / `Final` events
  (`agent/events.py`). The draft is provisional: a draft that fails citation verification is voided
  with `Replace` (logged to `verify_failures.csv`) and only the verified `Final` enters history and
  `chat.csv`. `answer()` drains the stream, so `eval.py` and `ask.py` are unchanged.
- B3: the chat UI shows each stage live ("Searching documents…", "Writing the answer…"), types
  the draft as it streams, then shows the verified answer with "Answered in N s". References and
  thumbs appear immediately (no extra page rerun); suggestion chips clear when a question is asked.
- B4: keyword search runs alongside embedding + vector search; `both` questions search documents
  while the database query (and its SQL-generation call) runs.
- B5: caches. Query embeddings (LRU 512). Verified documents answers (`answer.cache_ttl_seconds`,
  default 1 h; key = normalized question + filter + index version; follow-ups and data answers never
  cached): a repeat question went 32 s → 0.1 s. SQL results (`data/cache.py`,
  `data.result_cache_ttl_seconds`, default 180 s; keyed on the guarded statement + parameters; hits
  keep the original `as_of`, logged as `cache:<tool>`). **↻ Refresh data** button under database
  answers re-runs the query live.
- B6: SQL Server connection pool (`data/connectors.py`): session settings once per connection,
  rollback on every release, a failed connection is never reused, a dead pooled session is retried
  once; `fetchmany(max_rows)`; fixed tools marked `heavy: true` get `data.timeout_seconds_heavy`
  (30 s). Pooled query 266 ms → 4 ms.

### Fixed
- When a database answer came back not-found and was retried against the documents, the voided
  draft stayed on screen during the document search; it is now cleared first.
- A failed LLM call raised a pydantic `ValidationError` instead of `LLMError`, so the friendly
  quota / timeout message from v1.1.0 never appeared (`ChatReply.text` now defaults to "").
- `.env.example`: `thinking_budget: 0` is rejected by gemini-3.5-flash-lite (HTTP 400), so it is now
  128; values unquoted and comments moved to their own lines (Docker `--env-file` keeps both);
  model names updated to gemini-3.5-flash-lite.

## [1.1.0] — 2026-09-23 — Phase A: speed and UX quick wins

Commit `48e2595`. Eval (62 cases, `eval/results/20260923T1743.json`): correctness 83% → **85%** (target
met), faithfulness 100%, hit rate 93%, citation validity 100%, not-found 100%, refuse 100%.

### Changed
- Reranker: 10 candidates at `max_length` 512 (was 20 at 1024), single batch with all CPU threads.
  Retrieval hit rate 29/30.
- Answer thinking budget 1024 → 0 (extractive prompt; faster, no quality loss in eval).
- LLM client: retries 8 → 2, timeout 90 s → 30 s, so a quota failure surfaces in seconds.

### Added
- Models, index and catalogs load once at app start (`st.cache_resource`); `get_store()` and the
  catalog / fixed-tool loaders are cached (catalogs keyed on file mtime).
- Router answers unambiguous questions from regex with no LLM call; a fixed-tool match routes to
  `data` directly; rewrite is skipped for self-contained follow-ups.
- `LLMError` (quota / timeout / auth / other) mapped to friendly UI messages (`data/errors.py`);
  last-resort error guard in the UI logs tracebacks to `logs/errors.log`.
- UI: suggested-question chips, Clear chat, Admin details toggle, PDF download buttons.
- Self-updating index: `scripts/ingest_worker.py` + `index_version.refresh_if_changed()` pick up new
  PDFs without restarting the app; `docker-compose.yml` runs app + worker.

### Fixed
- Count-phrased policy questions ("how many licences does the Group have?") no longer misroute to
  the database.

## [1.0.0] — 2026-09-22 — Pilot (milestones M0–M6)

Commit `27ecd09`. RAG over IT SOP PDFs (250 chunks) and live SQL Server with a citation on every fact;
Streamlit UI; Docker image `bitopi-assistant:1.0`; 99 unit tests. Eval: correctness 83%.
