# Bitopi Knowledge Assistant (RAG chatbot)

Answers questions from PDFs in `data/pdfs/` and from read-only production databases, and
cites every answer (PDF file + topic + page, or database + view + row key + SQL).
If nothing is found it says: **System doesn't have the data.**

Built with Claude Code from the brief in `CLAUDE.md`. Plan and acceptance tests: `docs/PLAN.md`.
Database rules and the semantic layer (including how this build runs without any DDL on the
server): `docs/DATA_ACCESS.md`. Citation format: `docs/REFERENCE_FORMAT.md`.

## Quick start (Windows)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config\settings.example.yaml config\settings.yaml
copy .env.example .env            # fill LLM key(s) and DB connection strings — see below
# put PDFs under data\pdfs\<Category>\  (e.g. data\pdfs\SOP\, data\pdfs\TAL\)
python scripts\ingest.py          # index PDFs (re-run any time; only changed files are processed)
python scripts\ask.py "Who approves a PCD change after fabric in-house?"
streamlit run src\ragbot\app.py   # chat UI
python scripts\eval.py            # golden-set scores
pytest -q                         # unit tests (guard, chunker, citations, rrf, virtual views)
```

Tesseract (with `eng` and `ben` language packs) must be installed and on PATH for scanned PDFs.
Without it, scanned pages are skipped with a warning in `logs/ingest.log` rather than failing the run.

### LLM provider

`.env` chooses the provider (`LLM_PROVIDER=openai_compat` or `anthropic`) and the two models
(`LLM_MODEL` for answering/SQL, `LLM_SMALL_MODEL` for rewrite/routing). This build is configured
for **Google Gemini via its OpenAI-compatible endpoint** (`LLM_BASE_URL=https://generativelanguage.
googleapis.com/v1beta/openai/`) — no vendor SDK is used outside `src/ragbot/llm/`, so switching to
Azure OpenAI or Anthropic direct is a `.env` change only. Model IDs are pinned (not `-latest`
aliases) so `scripts/eval.py`'s regression gate stays meaningful across runs. If a Gemini model
starts reasoning/"thinking" and eats the whole token budget, tune `LLM_EXTRA_BODY` /
`LLM_SMALL_EXTRA_BODY` (thinking budget) or `LLM_REASONING_EFFORT` — see the comments in
`.env.example`.

## Folders

- `data/pdfs/` — drop PDFs here. Subfolder = category tag used for filtering and shown in references.
- `data/index/` — generated index (Chroma, BM25, registry). Delete to rebuild from scratch.
- `config/catalog/` — one YAML per database describing its `rag.*` views (business language only —
  this is the only description of the database the model ever sees).
- `config/fixed_tools.yaml` — hand-written parameterised SQL for the most common data questions.
- `prompts/` — system prompts as text files (never inline prompt text in `.py`).
- `tests/golden.jsonl` — end-to-end cases used by `scripts/eval.py`. `tests/retrieval_cases.jsonl` —
  retrieval-only cases used by `scripts/hit_rate.py`.
- `logs/` — every LLM call (`calls.csv`), SQL statement (`sql.csv`), chat turn and feedback (`chat.csv`),
  ingest run (`ingest.log`), citation-verification failure (`verify_failures.csv`).
- `docs/schema/` — read-only metadata dumps (`discover_schema.py`) and the DDL script a DBA can run
  to materialise the `rag.*` views for real (`gen_rag_views.py`); nothing here is ever executed by the app.

## Runbook

- **Add documents**: copy PDFs into `data/pdfs/<Category>/`, run `python scripts/ingest.py`. Re-runs
  are idempotent (unchanged files are skipped by SHA-256; changed files are re-chunked; removed
  files are purged from the index).
- **Add a database view**: this build has no DDL access, so every catalog view carries a `definition:`
  field (a plain SQL `SELECT` over the real tables) instead of a real `CREATE VIEW` — see "Database
  views without DDL" below. Once the DBA creates the real view, delete `definition:` and the same SQL
  runs directly. Either way: describe the view in `config/catalog/<db>.yaml`, run
  `python scripts/check_catalog.py --live`, add golden-set cases, run `python scripts/eval.py`.
- **Add a fixed tool**: add an entry to `config/fixed_tools.yaml` (regex `match`, `params`, `sql`
  referencing `rag.<view>`), run `python scripts/check_catalog.py` and add an
  `example_params:` block for `--live` smoke testing.
- **Change LLM provider**: edit `LLM_PROVIDER`, `LLM_MODEL`, `LLM_SMALL_MODEL`, `LLM_BASE_URL`, key
  in `.env`; run `python scripts/eval.py` and compare against the previous run (it blocks on any
  metric dropping more than 5 points).
- **Rotate keys / connection strings**: edit `.env` only — it is git-ignored and never logged
  (`logs/calls.csv` records token counts and model names, never prompt text or secrets).
- **Change embedding model**: NOT a plain config change — the vector index is tied to the model
  that built it (`store.py` refuses to open a mismatched index). Set `EMBED_MODEL` in `.env`, then run
  `python scripts/reindex.py --yes` for a full rebuild (there is no separate `--model` flag; the model
  comes from `.env`).
- **Restore**: delete `data/index/` and re-run `python scripts/ingest.py` — the registry (SQLite) and
  vector store can always be rebuilt from the source PDFs plus the databases; nothing else holds state.

### Database views without DDL

Writes to the production SQL Server are **strictly prohibited** for this build. Instead of real
`rag.*` views, `config/catalog/*.yaml` gives each view a `definition:` (a SELECT over the real
tables) that the model never sees, and `src/ragbot/data/virtual.py` wraps the guarded SQL as a CTE
at execution time — the SQL shown to the user (`Reference.sql`) is exactly what was written; only the
value logged internally as `sql_executed` differs. See `docs/DATA_ACCESS.md` §6 for the full design.
`python scripts/gen_rag_views.py` renders the DDL a DBA can run later to create the real views —
this script only writes a `.sql` file, it never connects to a database.

### Verifying a database connection (read-only, rolls back)

```powershell
python scripts\db_ping.py                 # every catalog's database
python scripts\discover_schema.py SQLSERVER_CONN_BITOPISPLINT BitopiSplint --schemas dbo
python scripts\check_catalog.py --live    # runs every view's definition + every fixed tool's example
```

### Asking about any table: schema discovery and the schema index (Phase C)

Beyond the curated `rag.*` views, generated SQL may use any discovered table the question needs —
never a sensitive or excluded one (`data/sensitive.py`, `exclude_tables:` in the catalog YAML), and
always through the guard. Refresh after the database schema changes:

```powershell
python scripts\discover_schema.py SQLSERVER_CONN_BITOPISPLINT BitopiSplint --json   # sys.* only, ~10 s
python scripts\discover_schema.py SQLSERVER_CONN_PRODUCTION Production --json
python scripts\index_schema.py            # embeds one short document per table (~15 min on CPU)
python scripts\index_schema.py --check    # recall on tests/schema_cases.jsonl (10/11 at k=6, 2026-09-27)
python scripts\index_schema.py --try "how many suppliers do we have?"
```

`--samples` on discovery also reads up to 30 distinct values of short text columns in small tables.
A full pass is ~6,700 small queries on production, so it is off by default.

### Aggregates: heavy queries answered from a nightly local copy (Phase C6)

`config/aggregates.yaml` lists curated queries too slow to run per question (PCD history scans the
4.2M-row, unindexed `dbo.ExportOrderBack`). `scripts\refresh_aggregates.py` copies each into
`data\index\aggregates.db` with one read-only scan; fixed tools with `aggregate:` answer from the copy
and show its as-of time. "Refresh data" in the UI, or a missing copy, runs the query live instead.
Schedule the refresh nightly, off-hours:

```powershell
python scripts\refresh_aggregates.py                 # once (Task Scheduler: daily 02:00)
python scripts\refresh_aggregates.py --every 24      # or keep it running
```

The DBA index on `dbo.ExportOrderBack(ExportOrderID)` (`docs/schema/rag_views_bitopisplint.sql`)
would make the live query fast too; the copy is the no-DDL workaround until then.
