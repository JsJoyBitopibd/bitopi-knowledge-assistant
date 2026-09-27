# Build plan — milestones and acceptance tests

Build in this order (M0–M8). Each milestone ends with a runnable command and a test that either passes
or fails. Do not start the next milestone until the current one passes. Keep `tests/golden.jsonl`
growing from M2 onward.

| Milestone | Deliverable | Acceptance test |
|---|---|---|
| M0 Setup | venv, `requirements.txt` installed, `config/settings.yaml`, `.env`, `ChatModel` adapter answers `hello` for the configured provider and logs the call to `logs/calls.csv` | `python -c "from ragbot.llm import get_chat; print(get_chat().chat([{'role':'user','content':'Say OK'}]).text)"` prints `OK`; a row appears in `logs/calls.csv` |
| M1 Ingest | `scripts/ingest.py` indexes every PDF in `data/pdfs/` into Chroma + BM25 + registry, idempotently | Run twice on 3 PDFs: second run reports `0 changed`. `python scripts/inspect.py --stats` shows docs, pages, chunks, tables, OCR pages. 10 random chunks read as one topic each with a heading prefix. |
| M2 Retrieve | `retrieve/retriever.py` returns top-6 chunks for a question with page/section metadata; hybrid + rerank | `scripts/hit_rate.py` on 20 questions ≥ 85% (expected page in top-6). Questions with a code (form number, SOP id) are found. |
| M3 Answer (PDF) | `scripts/ask.py` answers from PDFs with `[P#]` references rendered per `docs/REFERENCE_FORMAT.md`; not-found path works | `scripts/eval.py` on 30 PDF cases: correctness ≥ 85%, faithfulness ≥ 95%, citation validity 100%, all 3 not-found cases return the exact phrase |
| M4 Data | Catalog for ≥ 5 SQL Server views (+ MySQL when available), guard, fixed tools, text-to-SQL; `[D#]` references with db/view/row keys/SQL | 20 data questions: fixed tools 100%, generated SQL ≥ 80% correct result sets; 20 adversarial statements (DROP, UPDATE, EXEC, comments, UNION to sys tables, INTO OUTFILE) 0/20 pass the guard |
| M5 Agent + UI | Rewrite, router, combined answers, Streamlit app with references panel, thumbs up/down to `logs/chat.csv`, sliding-window memory | A question needing both sources ("PCD for EO X and the approval rule") answers in one turn with `[D1]` and `[P1]`; a 5-turn conversation with two follow-ups is resolved correctly |
| M6 Hardening | Category filter from folder names, superseded revisions, OCR confidence flag, ingest of 10k PDFs without OOM, `eval.py` compares with last run and prints BLOCK on > 5-point drop, README complete | `scripts/eval.py` full golden set ≥ 50 cases; 10k-PDF ingest completes; runbook in README |
| M7 Ingestion worker | `scripts/ingest_worker.py`: an always-on process, separate from the chat app, that picks up new/changed/removed PDFs from a job queue and indexes them incrementally (metadata pre-check → SHA-256, embedding cache by chunk-text hash, parallel parse/OCR pool, batched embedding, incremental keyword index, bulk vector load). The chat app sees new documents without a restart. | (a) Drop 1,000 PDFs into `data/pdfs/` while the app is serving: no restart, chat p95 latency rises < 20 %, new documents answerable once the worker reports the batch done. (b) Re-scan of an unchanged tree hashes 0 files (size+mtime match). (c) Edit one page of a 40-page PDF: only that document is re-chunked and ≥ 90 % of its chunks are embedding-cache hits. (d) Kill the worker mid-batch, restart: it resumes; no duplicate or orphan chunk ids (new `inspect.py --check`: registry vs vector store vs keyword index). (e) Throughput (files/h, pages/min, OCR pages/min, chunks/min) recorded in `docs/tuning_log.md` with a projected time for 60,000 PDFs. (f) `hit_rate.py` does not drop > 1 point versus M6. |
| M8 Latency | Per-stage timings in `logs/chat.csv`; document and DB retrieval run concurrently for `both`; DBA script for indexes behind the `rag.*` views; connections pointed at a readable secondary/replica where one exists; SQL Agent summary tables for heavy aggregates (exposed as `rag.*` views); short-TTL result cache | 50-question latency run (`scripts/latency.py`) prints p50/p95 per stage and end-to-end; proposed targets p95 ≤ 8 s documents, ≤ 10 s data/both (confirm on the pilot server). A cached result shows its original as-of time in `[D#]`; cache keys include the user's scope, so a buyer never receives a staff-scoped cached result. Guard and citation tests still 100 %. |

## Notes per milestone

**M0.** Implement `llm/openai_compat.py` first (works for Azure OpenAI, Azure AI Foundry, OpenAI,
and any OpenAI-compatible endpoint via `LLM_BASE_URL`). Then `llm/anthropic.py`. Both return
`ChatReply{text, usage_in, usage_out, model, stop_reason}` and write one CSV row per call.

**M1.** Order of work inside ingest: `pdf_text.clean_pages` → `tables.tables_on_page` →
`chunker.chunk_page` → `embed` (batch 32) → `store.upsert` → registry rows → BM25 rebuild.
Use a generator per document so memory stays flat. Test on a clean SOP, a scanned PDF and a
PDF with a table before running on the folder.

**M2.** Retrieval quality is where most time goes. Write `tests/retrieval_cases.jsonl`
(question, expected source, expected page) from real PDFs; iterate chunking and hybrid
settings until ≥ 85%. Keep a run log in `docs/tuning_log.md`.

**M3.** The `<sources>` block and citation verifier are the heart of the product. A number in
the answer that is not in a source is a failure even if it is correct. Use the exact not-found
phrase from `prompts/not_found.txt`.

**M4.** Start with SQL Server: create schema `rag`, 5–8 views, a read-only login (see
`docs/DATA_ACCESS.md`). Write `config/catalog/sqlserver.yaml` in business language. Fixed
tools before generated SQL. MySQL (host `MySQLServer2019`; database and tables still to be confirmed) follows the same pattern with `dialect: mysql`; if MySQL
details are not yet available, implement the connector and leave the catalog empty.

**M5.** Router is a single small-model call returning one label. Memory: keep the last 3
Q/A pairs as plain text; never carry earlier sources into a new prompt; retrieve fresh each turn.

**M6.** Add `scripts/inspect.py` (stats, list failures, show a chunk), `scripts/reindex.py`
(new collection when the embedding model changes). Write the README runbook: add PDFs, add a
view, rotate keys, re-index, restore.

**M7 — ingestion worker.** Goal: bulk arrivals (e.g. 60,000 PDFs) never interrupt chat.
Keep `scripts/ingest.py` as the one-shot command; the worker reuses the same `chunks_for`,
chunker, table and OCR code — no second pipeline.

- *Stages.* Scanner → job queue → parse/OCR pool → embed stage → single writer.
  - **Scanner** walks `data/pdfs/` every N minutes (configurable). Cheap check first
    (path + size + mtime against the registry); SHA-256 only for candidates. A file whose
    size is still changing between two scans is skipped until stable (copy in progress).
  - **Queue** is a table `ingest_jobs` in the registry (source, size, mtime, sha256, action
    add|update|delete, status queued|running|done|failed, attempts, error, updated_at).
    A job is claimed atomically; `running` jobs older than a timeout go back to `queued`
    on restart. After 3 failed attempts → `failed`, listed by `inspect.py --failed`.
  - **Parse/OCR pool**: `ProcessPoolExecutor`, workers = cores − 1 (setting). OCR is the
    bottleneck; one process per page batch, Tesseract `eng+ben` as today.
  - **Embed stage**: one process holds bge-m3. GPU when available; otherwise int8 ONNX
    bge-m3 on CPU. Look up each chunk's SHA-256(text) in an `embed_cache` table first; only
    misses are embedded. The cache is keyed by (text hash, `embed_model`, `embed_backend`).
  - **Writer**: the only process that writes to the vector store, registry and keyword index.
    Per document: delete old chunks → upsert new → registry rows → mark job done, so a crash
    leaves either the old version or the new one, never a mix.
- *Embedding backend parity (non-negotiable 6).* One backend per index. Store
  `embed_backend` (e.g. `st-fp32`, `onnx-int8`, `cuda-fp16`) next to `embed_model`; refuse to
  start on mismatch. Switching backend on an existing index requires `scripts/reindex.py`,
  unless a parity test on 1,000 chunks shows cosine ≥ 0.99 against fp32 and `hit_rate.py`
  drops ≤ 1 point — record the result in `docs/tuning_log.md`.
- *Keyword index.* The pickled `rank_bm25` rebuild does not scale and does not survive a
  separate process: `get_keyword_index()` is `lru_cache`d, so the running app never sees a
  rebuild done by the worker. Interim (Chroma pilot): the worker rebuilds once per finished
  batch, bumps `index_version` in `ingest_state.json`, and the retriever reloads when that
  value changes. Production: incremental keyword search — Qdrant sparse vectors in the same
  collection, or SQL Server full-text — behind a `KeywordIndex` interface. Test that Bangla
  terms still match before switching.
- *Vector store.* Chroma and Qdrant reads are live, so no restart is needed for vectors.
  Qdrant bulk load: set `optimizers_config.indexing_threshold` to 0 before a large batch,
  upload in batches, restore the default afterwards; payload indexes on `category`,
  `source`, `superseded`.
- *Superseded revisions* are recomputed once per batch, not per file (it is already a pure
  function over all titles).
- *Deletes are cautious.* A source is deleted only after it is missing in two consecutive
  scans; if more than 5 % of known sources disappear in one scan (share offline, wrong
  mount), the worker stops and logs an alert instead of deleting.
- *Capacity planning.* Run the worker on 200 representative PDFs (text, scanned, tables)
  and record the rates; estimate 60,000 PDFs from total pages × pages/min before a real bulk
  load. OCR pages dominate — count them separately.
- *Run as a service* (Windows service / NSSM or a container) with its own log
  `logs/ingest_worker.log` and a heartbeat row the Streamlit app can show ("index updated
  12 min ago, 340 documents queued").

**M8 — latency.** Measure first: add per-stage timings (rewrite, route, embed, vector,
keyword, rerank, SQL, LLM, verify) to `logs/chat.csv`, and `scripts/latency.py` over a fixed
50-question set. Then, in order of expected payoff:

- Run document retrieval and DB querying concurrently when the route is `both`.
- Indexes on the base-table columns the `rag.*` views filter and join on: we write
  `docs/schema/rag_indexes_<db>.sql`; a DBA reviews and runs it (non-negotiable 7 — this
  repo never executes DDL).
- Point the read-only login at a readable secondary / replica where one exists (connection
  string change only).
- Heavy aggregates (monthly efficiency, order-level totals) come from SQL Agent summary tables
  refreshed on a schedule, exposed as `rag.*` views with an `as_of` column that the `[D#]`
  reference shows.
- Short-TTL cache (setting, default 120 s) of result sets keyed by (normalised SQL, params,
  user scope). A cache hit keeps the original as-of time.
- The rewrite and route calls use the small model and can be skipped when there is no history
  (rewrite) or a fixed tool matches (route).

**Decided — not on the plan.** *Fine-tuning:* no. It does not make answers faster and does not
make them more factual; facts come from retrieval and live SQL. *MCP:* later and optional — a
standard way to expose `search_documents` and `query_data` to other clients (e.g. Claude
Desktop, other internal agents) with the same guard, scope and citation rules. It is not a
performance fix.

## Definition of done for the pilot

- 10 users, 4 weeks, ≥ 85% correct with valid references, 0 answers without a reference,
  0 invented numbers, "System doesn't have the data." on every unanswerable question.
- IT can add a PDF folder or a catalog view without code changes.
