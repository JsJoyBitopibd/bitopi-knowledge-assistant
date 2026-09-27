# Progress: what is done, what is next

**Read this first in a new session.** It is the single place that says where the work stands.
Design: `docs/ROADMAP.md` (Phases A–E). Release history: `CHANGELOG.md`. Milestones M0–M8:
`docs/PLAN.md`.

## Snapshot

| | |
|---|---|
| Released | **v1.2.0** (Phase B), tag `v1.2.0`, on `main` |
| In progress | **Phase C** (large database: schema RAG) → v1.3.0, branch `feature/phase-c-schema-rag` |
| Next task | C6 — pre-computed aggregates for heavy queries (local SQLite, scheduled refresh) |
| Last eval | 2026-09-27, `eval/results/20260927T1214.json`: correctness 85%, faithful 100%, hit 91%, citations 100% |
| Tests | 242 passing (`.venv\Scripts\python -m pytest -q`) |
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

## Phase C — Large database: schema RAG (v1.3.0) 🔄

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
| C6 | Pre-computed aggregates in local SQLite for heavy queries | ⬜ | unit tests; live refresh | |
| C✓ | Phase gate: full eval, no metric drops > 5 points | ⬜ | `scripts/eval.py` (62 cases) | |

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
| 2026-09-27 | v1.2.0: Phase B done (B1–B6 + gate), merged to `main`, tagged. Phase C drafted (C1–C5 in working tree) |
