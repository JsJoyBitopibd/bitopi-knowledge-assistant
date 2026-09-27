# Retrieval tuning log (M2)

One row per `scripts/hit_rate.py` run against `tests/retrieval_cases.jsonl`.

| Date | Settings | Cases | Hit rate | Notes |
|---|---|---|---|---|
| 2026-09-22 | defaults (`vector_top_k=20`, `keyword_top_k=20`, `rerank_candidates=20`, `final_top_k=6`, reranker on) | 30 | **30/30 = 100%** | First run against the real ingested corpus (Policy Book + SOP Manual, 250 chunks). No misses — default settings met the ≥85% M2 acceptance bar on the first try; no tuning needed. Run was CPU-slow (~1h wall clock) purely from first-time `bge-reranker-v2-m3` download + CPU cross-encoder inference on 30×20 candidate pairs, not a quality issue. |

## Golden-set evaluation (M3 + M4)

`scripts/eval.py` over `tests/golden.jsonl` (62 cases: 44 PDF, 10 data, 3 both, 3 not-found,
1 refuse, 1 chitchat, 1 follow-up). Reranking disabled for these runs (M2 already validated
retrieval at 100% hit rate *with* the reranker on; disabling it only removes a slow CPU re-ranking
pass that would have added hours per run and does not change which chunks are candidates).

| Run | Model | n | Hit | Faithful | Correct | Citations | Not-found | Refuse |
|---|---|---|---|---|---|---|---|---|
| baseline_lite (2026-09-22) | gemini-3.5-flash-lite | 62 | 88% | **100%** | 81% | 98% | 100% | 100% |
| after_fixes (2026-09-22) | gemini-3.5-flash-lite | 62 | **93%** | 99% | 83% | **100%** | **100%** | **100%** |

Targets (docs/PLAN.md M3): correctness ≥85%, faithfulness ≥95%, citation validity 100%, all
not-found cases returning the exact phrase. After the fixes below: citation validity, not-found,
refusal and faithfulness all meet target; hit rate improved 88% → 93%; **correctness is 83%, two
points short of the 85% bar.** Of the five remaining misses, cases 53 and 54 are golden-set
expectation gaps rather than assistant errors (see below), so the true figure is better than 83% —
but the golden set is the contract, so this run does not pass M3's correctness bar as written.

### Defects this run surfaced, and the fixes

1. **Stray marker on a not-found reply** (case 32) — the sole citation-validity failure. `verify()`
   short-circuits on the not-found phrase, so a `[D1]` the model appended survived into the text
   while `references` was (correctly) cleared, leaving a marker that resolved to nothing.
   `docs/REFERENCE_FORMAT.md` requires a not-found reply to carry no markers at all.
   Fixed in `orchestrator.py`: markers are stripped from the text when `not_found` is set.
2. **No documents retry after a fruitless data answer** (cases 20, 31, 32, 41). The router reads
   policy questions phrased as counts ("how many licences does the Group have?") as database
   questions. The generated SQL then returns rows that are real but irrelevant, so the existing
   "no rows → try documents" fallback never fired and the answer became not-found even though the
   PDFs held it. Fixed in `orchestrator.py`: a `data`-routed answer that ends not-found now retries
   once against the documents. Verified directly — both questions now answer correctly:
   "857 laptops and 334 desktops [P1]", "500 Endpoint Security (Kaspersky) licences [P4]".
3. **`how many` over-triggered the data router** (`router.py`) — a bare "how many" matched 12 of the
   44 PDF questions ("Within how many working days must …"). Narrowed to require an order/meeting
   noun nearby; case 3 went from a `data` misroute to a correct `documents` answer.
4. **`eval.py` ignored the `accept` lists** that `tests/golden.jsonl` and `scripts/hit_rate.py` both
   use for facts that legitimately appear on more than one page (case 18 answered correctly from a
   valid alternative page and was still scored a miss). Now honoured in both harnesses.

### Known remaining gaps (not code defects)

- Cases 53/55: the judge marked faithful, correctly-sourced answers wrong because the golden
  `expected` string was narrower than reality — PO 594520-9192 genuinely maps to several export
  orders, so the assistant returned a table of them. The expectations, not the answers, need widening.
- `rag.vw_PCDChangeHistory` can exceed the 10 s guard timeout: `dbo.ExportOrderBack` (4M+ rows) is
  indexed only on `Serial`, so filtering by `ExportOrderID` is a full scan. The index a DBA should
  add is in `docs/schema/rag_views_bitopisplint.sql`.

## Speed/UX/scale phases (2026-09-23 → 27)

All runs: 62 golden cases, gemini-3.5-flash-lite, reranker on. Per-case comparisons use the same cases.

| Run | File | Hit | Faithful | Correct | Citations | Not-found | Refuse |
|---|---|---|---|---|---|---|---|
| v1.1.0 Phase A | `20260923T1743.json` | 93% | 100% | 85% | 100% | 100% | 100% |
| v1.2.0 Phase B | `20260927T1214.json` | 91% | 100% | 85% | 100% | 100% | 100% |
| v1.3.0 Phase C | `20260927T1347.json` | **98%** | 98% | **89%** | 100% | 100% | 100% |
| v1.5.0 Phase D (ONNX reranker) | `20260927T1710.json` | 96% | **100%** | **90%** | 100% | 100% | 100% |

**Phase C gains:** cases 55, 56, 60, 61 went from 0 to fully correct — templated fixed-tool answers
(55: a 200-row list, 56: a count), the `eo_by_po` "IT **po**licy" false match (60), a `both`
question the router sent to `data` (61), and the multi-part answer rule (60). Data + both cases
alone: 9/13 → 13/13.

**Phase C costs (document answers, a tuning follow-up):** cases 6, 22, 33, 37 dropped 1.0 → 0.5. All
four answers are correct but terser than in v1.2.0 — the qualifier after the fact is left out
("for trend analysis and audit", "verified weekly", "before being dispatched to the site"). Likely
from the v5/v6 edits to `system_answer.txt`; to test next: a rule to keep the conditions, purpose and
exceptions a source attaches to a fact. Case 4 (faithful 1.0 → 0.0) is judge noise: the answer is
word-for-word the one Phase A's judge rated faithful.

**Tooling fix:** `eval.py` compared a `--kind` subset run with the previous run's totals and printed
false BLOCKs; it now compares with the most recent earlier run covering the same cases, on those cases.

**Phase D notes:** the hit-rate dip is one case (61) that hit a Gemini rate-limit mid-run; it passes on
re-run (`both` cases 3/3). Cases 2, 22, 33, 37 — "terser" in v1.3.0 — are complete again with the same
prompts, so that was run-to-run variance. Speed: `hit_rate.py` (30 questions, reranker on) >600 s →
387 s (batch size 1) → 174 s (int8 ONNX); document answers in the app ~10 s.
