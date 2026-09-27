# Bitopi Knowledge Assistant — project brief for Claude Code

You are building a Retrieval-Augmented Generation (RAG) chatbot for Bitopi Group (garment
manufacturing, Bangladesh). It answers staff questions from two sources and **cites every
answer**:

1. **PDF documents** dropped into `data/pdfs/` (SOPs, policies, buyer comments, tech packs,
   inspection reports — text or scanned). Reference shown to the user: **PDF file name, topic
   (section heading), page number, and the quoted passage.**
2. **Production databases**, read-only: SQL Server (Production.PPM, BitopiSplint, HR …) and a
   MySQL system. Reference shown: **database, table/view name, row key(s), the SQL executed,
   and the as-of time.**

If neither source contains the answer, the assistant replies with exactly:
**"System doesn't have the data."** followed by one sentence on where it might be found.
It never guesses.

Read `docs/PLAN.md` for the build order and acceptance tests, `docs/REFERENCE_FORMAT.md` for
the exact citation format, and `docs/DATA_ACCESS.md` for database rules. The PRD that governs
this project is `docs/PRD_v1.2.pdf` (read it once; this file is the operational summary).

## Start of every session: where are we?

1. Read **`docs/PROGRESS.md`** — current version, branch, the next task, and what is verified.
2. The design for the task is in `docs/ROADMAP.md` (speed/UX/scale Phases A–E).
3. Keep the record current, as version control for the project's state: when a task is **verified**
   (tests pass and the task's check in `docs/PROGRESS.md` has been run), update `docs/PROGRESS.md`
   (status ✅, evidence, next task) and add a line under `[Unreleased]` in `CHANGELOG.md` **in the
   same commit as the code**. Never mark a task ✅ on an unverified claim. A finished phase becomes
   a version: move its changelog entries under `## [X.Y.Z]`, merge to `main`, tag `vX.Y.Z`.

---

## Non-negotiables (a "working" feature that breaks one of these is a bug)

1. **Every factual sentence carries a reference marker** `[P1]`, `[D1]` … that resolves to a
   source actually retrieved for that question. Markers that do not resolve are stripped and
   the answer regenerated once; second failure → "System doesn't have the data."
2. **Numbers, dates and codes in the answer must appear in a retrieved chunk or a returned
   row.** The model never computes; SQL or Python computes.
3. **Databases are read-only.** One dedicated login per engine with SELECT on a `rag` schema of
   views only. Generated SQL passes the guard (`src/ragbot/data/guard.py`) or is not executed.
4. **Data minimisation to the LLM provider.** Per request: system prompt + question + ≤ 8
   chunks + ≤ 50 rows. Never whole documents, whole tables, or the index. Embeddings and
   reranking run locally (CPU). Every provider call is logged (chunk ids, row counts, tokens).
5. **Provider is swappable.** All LLM calls go through `src/ragbot/llm/base.py:ChatModel`.
   No vendor SDK import outside `src/ragbot/llm/`.
6. **Embedding model is fixed** (`BAAI/bge-m3`, 1,024-d) and its name is stored with the index;
   refuse to start if config and index disagree.
7. **Nothing is written to any production database. Ever.**

---

## Decisions already made (do not re-open without asking)

| Area | Decision |
|---|---|
| Language | Python 3.11+ for everything in this repo (a later .NET port is out of scope here) |
| Chat LLM | Commercial API via adapter. **Pilot: Google Gemini** through its OpenAI-compatible endpoint (`openai_compat`, paid/billing-enabled key only — never a free-tier key with company data). Production provider is picked by golden-set eval among Gemini on Vertex AI, Azure OpenAI / Azure AI Foundry (incl. Claude via Foundry) and Anthropic direct; the runner-up is the fallback. Chosen by `LLM_PROVIDER` in `.env`. |
| Small model | Same provider's cheap tier for rewrite/route (`LLM_SMALL_MODEL`) |
| Embeddings | `BAAI/bge-m3` via `sentence-transformers`, local CPU (fallback: Ollama `bge-m3` if `EMBED_BACKEND=ollama`) |
| Reranker | `BAAI/bge-reranker-v2-m3` via `sentence-transformers` CrossEncoder, local CPU, switchable (`RERANK_ENABLED`) |
| Vector store | Phase 1: Chroma (`data/index/chroma`), cosine. Behind `VectorStore` interface so Phase 2 can move to Qdrant without touching callers. |
| Keyword search | BM25 (`rank_bm25`) over the same chunks, rebuilt after each ingest; merged with vectors by RRF (k=60). From M7: incremental keyword index (Qdrant sparse vectors or SQL Server full-text) behind the `KeywordIndex` interface — no full rebuild |
| Ingestion runtime | Pilot: one-shot `scripts/ingest.py`. From M7: a separate always-on worker (`scripts/ingest_worker.py`) with a job queue in the registry; the chat app never restarts to pick up documents. Incremental only: size+mtime pre-check, then SHA-256; embedding cache keyed by chunk-text hash |
| Embedding hardware | CPU fp32 for the pilot. For bulk loads: GPU, or int8 ONNX bge-m3 on CPU — one backend per index, recorded as `embed_backend`; changing it needs the parity test in PLAN M7 or a full reindex |
| PDF parsing | PyMuPDF (`pymupdf`); tables via `page.find_tables()` → Markdown; OCR via Tesseract (`eng+ben`) when a page has no text layer |
| Registry | SQLite `data/index/registry.db` (documents, pages, chunks, ingest state). Phase 3 may move to SQL Server. |
| UI | Streamlit `src/ragbot/app.py` for the pilot |
| Config | `config/settings.yaml` (copy from `settings.example.yaml`) + `.env` for secrets |
| Users & scope | Internal staff and external buyers. What a user may see (categories, buyers, factories) is enforced in code on retrieval filters and SQL parameters — never by the prompt |
| Fine-tuning | None. It helps neither speed nor factual accuracy |
| MCP | Later, optional: expose `search_documents` / `query_data` to other clients under the same guard, scope and citation rules. Not a performance fix |
| Tests | `tests/golden.jsonl` + `scripts/eval.py`; a change that drops any score > 5 points is not merged |

---

## Repository layout

```
CLAUDE.md                     ← this file
README.md                     ← how to run (for humans)
CHANGELOG.md                  ← release history (one section per version tag)
docs/PROGRESS.md              ← status: done / in progress / next — read first each session
docs/ROADMAP.md               ← speed/UX/scale plan, Phases A–E
docs/PLAN.md                  ← milestones M0–M8 with acceptance tests   (build in this order)
docs/REFERENCE_FORMAT.md      ← exact citation format for PDF and DB answers
docs/DATA_ACCESS.md           ← read-only DB setup, catalog format, guard rules
docs/PRD_v1.2.pdf             ← product requirements (background; v1.1 kept for history)
data/pdfs/                    ← DROP PDFs HERE. Subfolder name = category tag (e.g. SOP/, TAL/, Buyer/)
data/index/                   ← generated: chroma/, registry.db, bm25.pkl, ingest_state.json  (git-ignored)
config/settings.example.yaml  ← copy to settings.yaml
config/catalog/               ← semantic layer: one YAML per database describing the rag.* views
config/fixed_tools.example.yaml ← hand-written parameterised SQL for common questions
prompts/                      ← versioned system prompts (plain text). Never inline prompts in code.
src/ragbot/                   ← the package
  config.py                   ← load settings + .env
  llm/                        ← ChatModel interface + providers (openai_compat.py, anthropic.py)
  embed.py                    ← Embedder (bge-m3), Reranker
  store.py                    ← VectorStore interface + ChromaStore; Registry (SQLite)
  ingest/                     ← pdf_text.py, tables.py, chunker.py, pipeline.py
  retrieve/                   ← keyword.py (BM25), hybrid.py (RRF), retriever.py
  data/                       ← catalog.py, guard.py, connectors.py (SQL Server, MySQL), tools.py
  agent/                      ← rewrite.py, router.py, answer.py, citations.py, orchestrator.py
  app.py                      ← Streamlit chat
scripts/ingest.py             ← python scripts/ingest.py   (index data/pdfs, one shot)
scripts/ingest_worker.py      ← M7: always-on incremental ingestion worker (not yet written)
scripts/ask.py                ← python scripts/ask.py "question"
scripts/eval.py               ← python scripts/eval.py     (golden set)
tests/golden.jsonl            ← evaluation cases (grow this; never delete cases)
logs/                         ← calls.csv, chat.csv, ingest.log (git-ignored)
```

Files marked **STUB** in their docstring are skeletons with the intended interface; implement
them following the milestone order. Files without that marker are meant to work as written but
have not been run against real data or databases here; test them first.

---

## How the answer path works (implement exactly this)

1. **Rewrite** (small model): if there is history, turn the latest message into a standalone
   question. Log original and rewritten.
2. **Route** (small model): `documents` | `data` | `both` | `chitchat` | `refuse`.
   `refuse` covers write requests and off-topic questions; reply with the fixed text in
   `prompts/refusal.txt`.
3. **Retrieve** (documents): embed question locally → vector top-20 + BM25 top-20 → RRF →
   fetch chunks + metadata → apply filters (category, superseded=false) → rerank to top-6.
4. **Query** (data): try fixed tools first (pattern match on question); else generate SQL from
   the catalog for the right dialect → guard → execute read-only with 10 s timeout, TOP/LIMIT 200
   → shape rows.
5. **Assemble prompt**: `prompts/system_answer.txt` + `<sources>` block (numbered `[P#]` for
   chunks with file/topic/page, `[D#]` for result sets with db/view/row keys) + question +
   one-line restatement of the citation rule.
6. **Generate** via `ChatModel`, temperature 0. Log the call.
7. **Verify** (`agent/citations.py`): every `[P#]`/`[D#]` resolves; every number/date/code in
   the answer appears in a source; else regenerate once with a stricter instruction; else
   return "System doesn't have the data."
8. **Return** `Answer{text, references[], not_found: bool, usage}` and render references as in
   `docs/REFERENCE_FORMAT.md`.

---

## Ingestion (scripts/ingest.py) — what it must do

- Walk `data/pdfs/**/*.pdf`. Category tag = first subfolder name (or `General`).
- SHA-256 per file; skip unchanged; re-index changed; delete chunks of removed files.
- Per page: strip repeated headers/footers; OCR if no text layer; extract tables as Markdown
  chunks with caption; chunk remaining text at numbered headings → paragraphs, 400–800 tokens,
  ~60-token overlap; prefix each chunk with `"<doc title> › <section heading>"`.
- Metadata per chunk: `source`, `title`, `page`, `section`, `kind` (text|table), `category`,
  `doc_hash`, `superseded` (false), `embed_model`, `ingested_at`.
- Chunk id = `<file stem>#p<page>#c<n>` (tables: `#t<n>`). Upsert by id.
- Embed in batches of 32; rebuild BM25 index; write ingest_state.json; print a summary line and
  list failures (zero chunks, corrupt, encrypted) in `logs/ingest.log`.
- Must handle 10,000 PDFs in one run without running out of memory (stream, don't hold all
  chunks in RAM).
- **Worker mode (M7).** Same code path, driven by a job queue instead of one walk: scanner →
  parse/OCR process pool → embed stage (with embedding cache) → single writer (per-document
  delete-then-upsert). Resumable after a crash; never deletes on a single missed scan. The
  retriever reloads the keyword index when `index_version` in `ingest_state.json` changes.
  Details and acceptance tests: `docs/PLAN.md` → M7.

---

## Conventions

- Type hints everywhere; `pydantic` models for `Chunk`, `Reference`, `Answer`, `QueryResult`.
- No prompt text in `.py` files — load from `prompts/` by name.
- Every LLM call goes through `llm/base.py:ChatModel.chat()` which logs to `logs/calls.csv`.
- Secrets only from `.env` (`python-dotenv`); never commit `.env`, `config/settings.yaml`,
  `data/index/`, `logs/`.
- Dates in ISO format in metadata and rows; display as `12 Oct 2026`.
- English and Bangla text must both survive (UTF-8 everywhere; BM25 tokenizer keeps Bangla).
- Prefer small, testable functions; add a `tests/test_*.py` for guard, chunker, citations, RRF.

## Commands

```bash
python -m venv .venv && .venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy config\settings.example.yaml config\settings.yaml
copy .env.example .env                                   # then fill keys and DB strings
python scripts/ingest.py                                 # index data/pdfs
python scripts/ask.py "Who approves a PCD change after fabric in-house?"
streamlit run src/ragbot/app.py
python scripts/eval.py
pytest -q
```

## What NOT to do

- Do not add LangChain/LlamaIndex/other frameworks; the pipeline is small and explicit.
- Do not send document text to a provider embedding API.
- Do not let the model see table names outside `config/catalog/*.yaml`.
- Do not "fix" a low score by loosening the guard, the citation check, or the not-found rule.
- Do not silently change the embedding model.
