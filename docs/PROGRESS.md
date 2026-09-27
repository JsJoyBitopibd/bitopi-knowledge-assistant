# Progress: what is done, what is next

**Read this first in a new session.** It is the single place that says where the work stands.
Design: `docs/ROADMAP.md` (Phases A–E). Release history: `CHANGELOG.md`. Milestones M0–M8:
`docs/PLAN.md`.

## Snapshot

| | |
|---|---|
| Released | **v1.1.0** (Phase A), tag `v1.1.0`, on `main` |
| In progress | **Phase B** (streaming, parallelism, caching) → v1.2.0, branch `feature/phase-b-streaming` |
| Next task | B1 — streaming in the LLM layer |
| Last eval | 2026-09-23, `eval/results/20260923T1743.json`: correctness 85%, faithful 100%, hit 93%, citations 100% |
| Tests | 99 passing (`.venv\Scripts\python -m pytest -q`) |
| Last updated | 2026-09-27 |

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

## Phase B — Streaming, parallelism, caching (v1.2.0) 🔄

Targets: first token ≤ 3 s on a document question; `both` questions ~4 s faster; repeat questions instant.

| ID | Task | Status | Check before ✅ | Evidence |
|---|---|---|---|---|
| B1 | `ChatModel.stream()` + OpenAI-compatible streaming; one `calls.csv` row per stream | ⬜ | unit test with fake client; live Gemini stream | |
| B2 | `answer_stream()` events (Stage / Token / Replace / Final); `answer()` drains it; verify before Final | ⬜ | unit tests: event order, Replace on failed verify, LLM error mid-stream | |
| B3 | UI renders stages + tokens live; no extra rerun | ⬜ | manual in browser | |
| B4 | Parallel: BM25 ∥ embedding; documents ∥ database for `both` | ⬜ | unit test; timing | |
| B5 | Caches: query embedding, documents-answer cache, SQL result cache (+ Refresh data) | ⬜ | unit tests: TTL, invalidation, `as_of` kept, refresh bypass | |
| B6 | SQL Server connection pool, `NOCOUNT`, `fetchmany`, heavy-tool timeout tier | ⬜ | unit test with fake connections; live DB ping | |
| B✓ | Phase gate: full eval, no metric drops > 5 points | ⬜ | `scripts/eval.py` (62 cases) | |

## Phase C — Large database: schema RAG ⬜

C1 discovery · C2 catalog model · C3 schema index · C4 guard at scale · C5 plug into SQL
generation · C6 pre-computed aggregates · C7 zero-LLM data answers · C8 data UX. Not started.
Expected to fix eval misses 56, 60, 61.

## Phase D — 60K pages ⬜

D1 SQLite FTS5 keyword index · D2 Chroma tuning · D3 ingestion throughput · D4 ONNX reranker. Not started.

## Phase E — Freshness

| ID | Task | Status | Evidence |
|---|---|---|---|
| E1 | Ingestion worker (`scripts/ingest_worker.py`) | ✅ | shipped in v1.1.0 |
| E2 | Hot reload of new PDFs (`index_version.py`) | ✅ | shipped in v1.1.0 |
| E3 | Embedding cache + chunk-level diff | ⬜ | |
| E4 | Document the "new version of a document" behavior in README | ⬜ | |

## Open items outside the code

- **Gemini billing** must be enabled before multi-user use (free tier; company data needs a paid key).
- **DBA request**: `docs/schema/rag_views_*.sql` views, `rag_reader` login to replace `sa`, index on
  `dbo.ExportOrderBack(ExportOrderID)`.
- **Auth and per-user data scoping** (PRD FR-4): not planned in a phase yet. Any cache must key on
  the user's scope once this lands.

## Session log

| Date | What happened |
|---|---|
| 2026-09-22 | v1.0.0: M0–M6 pilot complete |
| 2026-09-23 | v1.1.0: Phase A done, eval correctness 85% |
| 2026-09-27 | Phase A merged to `main`; PRD v1.2 spec committed; tags v1.0.0/v1.1.0; tracking docs added; Phase B started |
