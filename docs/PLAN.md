# Build plan — milestones and acceptance tests

Build in this order. Each milestone ends with a runnable command and a test that either passes
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
tools before generated SQL. MySQL follows the same pattern with `dialect: mysql`; if MySQL
details are not yet available, implement the connector and leave the catalog empty.

**M5.** Router is a single small-model call returning one label. Memory: keep the last 3
Q/A pairs as plain text; never carry earlier sources into a new prompt; retrieve fresh each turn.

**M6.** Add `scripts/inspect.py` (stats, list failures, show a chunk), `scripts/reindex.py`
(new collection when the embedding model changes). Write the README runbook: add PDFs, add a
view, rotate keys, re-index, restore.

## Definition of done for the pilot

- 10 users, 4 weeks, ≥ 85% correct with valid references, 0 answers without a reference,
  0 invented numbers, "System doesn't have the data." on every unanswerable question.
- IT can add a PDF folder or a catalog view without code changes.
