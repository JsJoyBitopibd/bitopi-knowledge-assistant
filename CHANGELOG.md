# Changelog

All notable changes to the Bitopi Knowledge Assistant. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Each released version has a git tag (`vX.Y.Z`).

Where things stand right now (done / in progress / next) is in `docs/PROGRESS.md`. The design behind
each phase is in `docs/ROADMAP.md`.

## [Unreleased] — Phase C: large database, schema RAG (target v1.3.0)

Work in progress on branch `feature/phase-c-schema-rag`. Entries are added here as each task is
verified; see `docs/PROGRESS.md` for per-task status and evidence.

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
- Each `[D#]` source now shows the query's filter (factory, dates, order id) to the answering model
  and in the judge's view; `verify()` also accepts the row count and parameter values.

### Changed
- Prompts v5–v6 (`prompts/CHANGELOG.md`): SQL values as literals (only six date parameters exist);
  the router treats all ERP records as data; "pcs" only for garment quantities; multi-part answers
  answer the covered parts instead of replying not-found.

### Fixed
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
