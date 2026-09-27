# Progress: what is done, what is next

**Read this first in a new session.** It is the single place that says where the work stands.
Design: `docs/ROADMAP.md` (Phases A–E). Release history: `CHANGELOG.md`. Milestones M0–M8:
`docs/PLAN.md`.

## Snapshot

| | |
|---|---|
| Released | **v1.1.0** (Phase A), tag `v1.1.0`, on `main` |
| In progress | **Phase B** (streaming, parallelism, caching) → v1.2.0, branch `feature/phase-b-streaming` |
| Next task | B✓ — Phase B gate: full 62-case eval (rerank on), then release v1.2.0 |
| Last eval | 2026-09-23, `eval/results/20260923T1743.json`: correctness 85%, faithful 100%, hit 93%, citations 100% |
| Tests | 126 passing (`.venv\Scripts\python -m pytest -q`) |
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
| B1 | `ChatModel.stream()` + OpenAI-compatible streaming; one `calls.csv` row per stream | ✅ | unit test with fake client; live Gemini stream | `tests/test_stream.py` (4). Live 2026-09-27: 8 deltas, first at 2.6–3.4 s, one `calls.csv` row. Found + fixed: provider errors crashed instead of raising `LLMError`; `thinking_budget: 0` is rejected (HTTP 400) by flash-lite |
| B2 | `answer_stream()` events (Stage / Token / Replace / Final); `answer()` drains it; verify before Final | ✅ | unit tests: event order, Replace on failed verify, LLM error mid-stream | `tests/test_answer_stream.py` (6). Live 2026-09-27 (rerank off): 2 doc questions verified with correct `[P#]`; first token 3.9–6.1 s. Gemini sends short answers in 1–2 bursts, so first token ≈ full answer; model latency after "writing" is 3.6–3.8 s |
| B3 | UI renders stages + tokens live; no extra rerun | ✅ | manual in browser | Browser 2026-09-27, rerank ON: stage label updates live, chips clear on ask, "Answered in 19.2 s" / "17.9 s" (follow-up "Who approves it?" rewritten to Form IT-03), references + thumbs shown at once, thumbs-up logged to `chat.csv`, no app errors |
| B4 | Parallel: BM25 ∥ embedding; documents ∥ database for `both` | ✅ | unit test; timing | `tests/test_parallel.py` (2): two 0.4 s steps finish in < 0.6 s. `hit_rate.py` (rerank on) 29/30, same as baseline |
| B5 | Caches: query embedding, documents-answer cache, SQL result cache (+ Refresh data) | ✅ | unit tests: TTL, invalidation, `as_of` kept, refresh bypass | `tests/test_cache.py` (8). Browser 2026-09-27: repeat document question 32.2 s → **0.1 s** (same verified answer + `[P1]`); fixed tool 474 ms → cache hit 5 ms with identical `as_of`, logged `cache:<tool>`; ↻ Refresh data re-ran `eo_by_id` live (`sql.csv`) |
| B6 | SQL Server connection pool, `NOCOUNT`, `fetchmany`, heavy-tool timeout tier | ✅ | unit test with fake connections; live DB ping | `tests/test_pool.py` (6). `db_ping.py`: both DBs OK. Live: count tool 266 ms fresh → 4 ms pooled; `eo_by_id` 382 ms → 124 ms (≈120 ms is the query itself). No `SELECT 1` health check (one more round trip per query); stale session retried once instead |
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

## Small follow-ups noticed along the way

- Streamlit's file watcher logs ~50 harmless `ModuleNotFoundError: torchvision` tracebacks at start
  (it scans transformers' image modules). Fix: `server.fileWatcherType = "none"` in
  `.streamlit/config.toml` for production, or install torchvision. Cosmetic.
- Data answers the model wrongly reports as not-found (seen 2026-09-27, both older than Phase B):
  a count of **0** ("How many PPM meetings does TAL have this week?" → rows `[[0]]`), and a list of
  **200 rows** ("Which TAL orders ship in the next 30 days?"). Planned fix: Phase C7 templated
  answers (scalar → "Meetings: 0 [D1]") and C8 result tables.

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
