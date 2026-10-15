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
| v1.6.0 Phase F (sign-in, per-user scope; run as an unrestricted user) | `20260928T1820.json` | **98%** | **100%** | **93%** | 100% | 100% | 100% |

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

**Phase F notes (2026-09-28):** the golden set runs as an unrestricted user, so it checks that scoping
changed nothing for everyone else; leaks are measured separately by `scripts/scope_check.py` (34
cases, 0 leaks; reports in `eval/scope/`). Changed cases vs v1.5.0: 6 (0.5 → 1.0) and 61 (0 → 1.0,
v1.5.0's rate-limit miss). The run itself used the code before the review fixes (stricter CTE rule).
It executed no model-written SQL (only fixed tools and cache hits in `sql.csv`), so that rule cannot
change it; re-run on the final code, the 10 data answers were word-for-word the same, and the 3 `both`
cases and the judge calls hit Gemini 503 "high demand" (`20260928T1858.json`, 9 unscored) — to be re-run
when the provider recovers. `hit_rate.py` on the final code: 29/30. Machine note: this laptop ran CPU-heavy
jobs 4–6× slower than on 27 Sep all day (`hit_rate.py` ~20 min vs 174 s; the nightly copy 146 s vs
33 s, which also gained the Factory join) — the code paths behind those timings did not change.

## Latency by stage (Phase I, `scripts/latency.py`)

Every question cold (no answer, embedding or SQL result cache), models loaded before the clock
starts; seconds, nearest-rank p50 / p95. Stage keys: `ragbot/trace.py`. Files: `eval/latency/`
(git-ignored).

| Run | Group (n) | total p50 / p95 | first token p50 / p95 | Main stages p50 | Notes |
|---|---|---|---|---|---|
| 2026-09-28 20:32 (`20260928T2032.json`) | fixed tools (10) | 0.14 / 0.71 | — (templated, no model call) | data 0.14, data.db 0.12, route 0.00 (regex) | Partial baseline: Gemini returned 503 all evening, so the documents, model-SQL, both and not-found groups wait. The p95 is the first question, which also loads the fixed tools and catalogs (0.3 s) and opens the first database connection (0.26 s). |
| 2026-09-29 09:30 (`20260929T0930.json`) — **I1 baseline, full** | documents (30) | **7.45 / 9.72** | 6.96 / 9.04 | route 1.06 (model call in 16 of 30: 1.40), retrieve 2.93 (rerank 2.81, embed 0.13), answer 3.07 (model first token 3.04 = whole answer) | With the embedding fix, 10 candidates, max length 512. Other groups: fixed tools 0.14 / 0.39; model SQL 8.47 / 13.85 (SQL generation 4.83); both 6.83 / 7.46; not-found 7.86 / 13.12. **Gemini sends the answer in one burst** (first token 3.04 s vs 3.04 s total), so streaming cannot bring the first token near 3 s (I3). |
| 2026-10-04 10:36 (`20261004T1036.json`, RAG-CHAT-BOT server, Phase J) | model-written SQL (5) | **5.98 / 16.53** | 5.93 / 16.43 (2 of 5 called the answer model) | route 0.11 (model call in 1 of 5: 1.27), data.pick ≈ 0.02, llm.sql 3.50 / 3.87, data.db 0.11, answer 1.38 (n=2) | vs the I1 baseline 8.47 / 13.85 on the laptop: 17 catalogs instead of 2, yet one model call for 3 of 5 questions (templated rows, J3); the p95 is s3 ("most PCD changes in the last 30 days"), whose SQL returned no rows → documents fallback → rerank 8.3 s while another container was loading models on the same 4 cores. Through the app, quiet: "How many employees are there in total?" (Hrms5Misami, raw table) **4.2 s**, no answer-model call; a 0-count question (visitors, vouchers — both genuinely 0) 9.6–11.7 s because a model-written 0 is still judged by the model. |
| 2026-10-14 15:28 (`20261014T1528.json`, RAG-CHAT-BOT server, v1.13.0 gate) | model-written SQL (9) | **4.97 / 13.79** | 12.48 (1 of 9 called the answer model) | route 0.01 (no routing model call in any of the 9; p95 2.38 is s1's cold first route), llm.sql 4.02 / 7.17 (n=7), data.db 0.30 / 3.48, two answered by a fixed tool in 0.2–0.4 s | Inside the P3 rule (p50 within 0.5 s of 5.98 s). The set grew from 5 questions to 9 in Phases P and Q; on 9 Oct the 7-question set gave 4.95, 6.63 and 6.69 in three runs, so about 1.7 s of the spread is the provider. Quiet: no staff questions since 13:00, the aggregates refresh finished at 15:13, all services idle. The p95 is s1 (13.8 s), the first question: a cold route step (2.38 s, no model call — `calls.csv` has only its SQL call) and a cold `data.pick` (3.90 s) before its SQL. s4 (12.5 s) is the one documents fallback (rerank 7.0 s) |
| 2026-09-28 20:40 (`20260928T2040.json`, `--retrieval-only`) | documents + not-found search (33) | 4.16 / 5.91 | — (no model call) | **rerank 3.87** (93%), embed 0.46, vector 0.01, keyword 0.01, fetch 0.00 | Retrieval baseline: ONNX int8 reranker, 10 candidates, max length 512, onnxruntime default threads (10 on this 6P+4E i7-13620H). The reranker is the retrieval cost; a documents answer is ~10 s end to end (B2), so this is ~40% of it. |
| 2026-09-28 21:11 (`20260928T2111.json`, `--retrieval-only`) | same 33, **8 candidates, max length 384** | **3.22 / 4.43** | — | rerank 2.91, embed 0.27 | I2 candidate: −0.94 s p50 (−23%). Not applied yet: the G and H gate evals run first with the v1.6.0 settings, then the I2 gate eval with these. |

**I2 reranker experiments (2026-09-28).** onnxruntime threads, micro-benchmark over the real 10 candidates
of 12 golden questions (s per question, 2 interleaved rounds): default (10) 3.90, 8 3.87, 6 (P-cores
only) 4.05, 4 4.20, **16 5.62–6.25** — the default is already best; hyperthreads hurt. Max length
384 instead of 512: 3.51 (−10%, most pairs are shorter than 384 tokens). The reranker costs ~0.39 s
per pair on this CPU, so the lever is the candidate count. `hit_rate.py` (30 questions, gate ≥ 29/30):

| rerank_candidates | RERANK_MAX_LENGTH | Hit rate | Time (30 q) |
|---|---|---|---|
| 10 (v1.6.0) | 512 | 29/30 (miss: laptops/desktops, p39) | 168 s |
| 8 | 512 | 30/30 | 143 s |
| 10 | 384 | 29/30 (same miss) | 144 s |
| 8 | 384 | 29/30 (same miss) | 126 s |

**Threads (2026-09-28).** Two app-side fixes, measured on the pilot box (i7-13620H, 6P+4E cores, 16
threads). (1) onnxruntime's threads busy-wait after each rerank by default, and the next question's
embedding fought them: 260–340 ms per embed instead of 94–143 ms with spinning off (the rerank itself
3.8–4.0 s either way). (2) `embed.py` raised torch to all 16 logical CPUs; 8–10 threads are faster
(query embed 64–78 ms vs 76–87 ms alone; ingestion of 160 chunks **216 s at 4 or 8 threads vs 335 s at
16**). Now: spinning off, torch at least half the logical CPUs (stays at its default 10 here).
`retrieve.embed` p50 0.46 s → **0.09 s** (`20260928T2207.json`: retrieval 4.10 / 5.36 s p50/p95).

**I4: search while the worker ingests (2026-09-28).** A throwaway index ingesting 20 synthetic PDFs
(160 chunks, nothing cached) while `latency.py --retrieval-only` ran the 33 documents questions
against the live index; the ingest outlasted every latency run.

| Worker setting | Search p50 | Search p95 | p95 rise | Ingest (160 chunks) |
|---|---|---|---|---|
| none (same code, `20260928T2207.json`) | 4.10 s | 5.36 s | — | 216 s alone at 4 threads |
| 16 threads, normal priority (before) | 9.57 s | 13.67 s | +131% (vs 5.91 then) | 469 s |
| 4 threads, normal priority | 6.51 s | 8.90 s | +51% | 356 s |
| 4 threads, idle priority | 6.07 s | 8.46 s | +43% | 376 s |
| **4 threads, below normal, pause while the app searches** (`20260928T2211.json`) | **4.36 s** | **5.98 s** | **+12%** | 391 s (130 s paused) |

Priority alone does little (the processes also share memory bandwidth, caches and the turbo budget); the
pause is what keeps the app fast. The model stages of a chat answer run at the provider, so they are
not slowed by the worker; a full `latency.py` during an ingest is still to run when Gemini is back.

**Windows power throttling (2026-09-29).** From ~09:31 every model stage ran 5–6× slower (rerank 14–16 s,
query embed 0.6 s) with the CPU nearly idle otherwise. Cause: Windows 11 power throttling (EcoQoS) moved
the background console processes to the efficiency cores at low clocks. Same process, one call to opt out
(`src/ragbot/cpu.py`): numpy 10 × matmul 1.25 s → 0.19 s; rerank 10 × 512 14.7–16.4 s → 1.80 s, 8 × 384
13.5–14.5 s → 1.68 s per question. The runs `20260929T0956.json` and `T1023.json` were throttled and are not
comparable; the 09:30 baseline may have been partly throttled too.

| Run | Group (n) | total p50 / p95 | first token p50 / p95 | Main stages p50 | Notes |
|---|---|---|---|---|---|
| 2026-09-29 10:39 (`20260929T1039.json`) — **I2 applied** | documents (30) | **5.71 / 10.58** | 5.66 / 10.56 | route 0.76 (model 1.00; one 5.6 s outlier at p95), retrieve 2.04 (rerank 1.88, embed 0.12), answer 3.01 (model first token 2.87) | 8 candidates, max length 384, throttling opt-out. Fixed tools 0.14 / 0.37; model SQL 9.72 / 14.68 (SQL generation 4.12); both 4.90 / 5.34; not-found 5.83 / 13.94 |
| 2026-09-29 10:41 (`20260929T1041.json`, `--retrieval-only`) | 33, **10 candidates, 512**, opt-out | 3.32 / 4.33 | — | rerank 3.21, embed 0.08 | The old settings under the same conditions: I2's change saves 1.3 s of rerank (3.21 → 1.88) |

**I3 (first token).** Gemini (gemini-3.5-flash-lite, OpenAI-compatible endpoint) returns the answer in one
burst: its first token comes after 2.87 s and the whole answer after 2.98 s (p50). A user therefore sees
the first word only after routing + search + the whole generation (5.66 s p50). Streaming cannot bring it
to 3 s with this provider; the plan's fallback applies (show the sources as soon as the search is done).

**I3 fallback (2026-09-29, `20260929T1059.json`).** The "writing" stage now names the sources the answer is
written from, and the app shows them in the progress line at once: **sources shown at 2.05 / 2.98 s p50/p95**
(14 documents questions routed without a model call; add ~1 s where the router calls the model). The first
word of the answer still comes with the provider's single burst (5.66 s p50).

**I4 with the model stages (2026-09-29, `20260929T1045.json`).** Documents questions while the worker ingested
(below normal, 4 threads, pause): 10 answered, **total 5.66 / 7.04 s** vs 5.71 / 10.58 s without a worker;
retrieve 2.11 vs 2.04 s (+3%). The other 20 hit the key's per-minute limit, and a paced rerun
(`--sleep 4`) the free tier's daily limit (500 requests per model, both keys) — so n = 10, with the
33-question search-only run (+12% p95) as the larger sample. Throughput: 47.1 chunks/min for 320 chunks
while giving way (65 s paused).


## Phase M — Stage-2 quality risk: the backtest baseline

The Stage-2 → Stage-3 gate is *"several weeks of stored risk levels compared with what actually
happened"*. The levels began being stored on **2026-10-14**; this section is where the first backtest
goes, and it cannot be written before roughly late November 2026.

**What the backtest must report**, from `signals.settled("quality")`:

- the share of files predicted red whose final quality was over the VistaQ DHU target, and the same for
  amber and green — the thing the levels claim;
- the same split by factory, since TAL carries about two thirds of the files;
- rows with `revised > 0` excluded and counted separately: those are files that settled, resumed and
  moved, so their outcome was not final when it was first recorded;
- the lead time between a prediction and its outcome. A short lead time means the level was a
  description rather than a warning.

**Read every daily prediction, not only the settled row.** `risk.settle()` writes the outcome onto the
file's *newest* open prediction (`open_predictions()`), so each row `settled()` returns is the file's
last daily level, made at most a day before it finished: its lead time is always under a day. The
earlier predictions for the same file (one per day from `watch.py --risk-daily`, M5b) keep
`outcome_at` empty. To ask "what did the level say a week before the file finished?", join every
prediction of a (dimension, factory, subject) to that subject's settled outcome and group by days ahead.

**Before the daily series: 2026-10-14 holds four hand-run passes** (`risk_run` 1–4, 12:53–12:56, while M5 was
built), not one. Use run 4 as that day's level per file. Exclude the 14 rows of runs 1–2 that carry an outcome
(7 per run): those files were predicted and settled in the same pass, before `risk.predictable()` existed,
so their lead time is zero — the rows M5 found and stopped.

**The tool, built 2026-10-15** (`scripts/backtest.py`, `src/ragbot/domain_agents/backtest.py`; read-only over
signals.db, writes `logs/backtest_<dimension>_<date>.md`, exit 0 enough evidence / 3 not enough / 1 no store).
Run it in late November with `docker compose exec app python scripts/backtest.py`; `--until YYYY-MM-DD`
reproduces an earlier baseline exactly. It replaces the hand rules above with general ones:
- **one level per day** = the last run of the day that stored levels without errors (so 14 Oct is run 4 by rule;
  a refused or empty run is no day's level and not a date);
- **no lead time**: a settled row whose outcome was written by the run that stored it is left out first
  (the 14 rows of runs 1–2);
- **not final**, left out whole: a file settled more than once, `revised > 0`, predicted again after its
  outcome, or settled by the run that stored it and later running and settled again. `revised` never rises in
  production — `settle()` only writes rows with no outcome yet — so a paused file shows up as a second settled
  row, not as a revision;
- **over target** = the `dhu` measure stored with the outcome at or above the amber stored with it; too few
  checked at the end is neither a hit nor a miss;
- **days ahead** = outcome date − date of the run that stored the level; the headline is the level closest to
  one window (7 days) ahead and never closer; the lead grid keeps 1–6 (overlaps the outcome window) apart from 7.

**The minimum evidence, set now — before any outcome exists — so it cannot move once results are seen**:
at least **28 dates** in the daily series, **60 usable settled files** with DHU known at the end, and **15
judged files each at red, amber and green** 7 or more days ahead. Constants in the code, with no command-line
override. Below any of them the tool prints INSUFFICIENT EVIDENCE with every shortfall; above them it states
three facts and no judgement — *ordered* (red share > amber > green), *separated* (red's 95% Wilson interval
above green's), *lift* (red share − base rate). Whether those facts earn M✓ is the owner's call, recorded here.

**First run, 2026-10-15 09:5x** (`logs/backtest_quality_20261015.md`): INSUFFICIENT EVIDENCE — 2 dates (run 4
for 14 Oct, run 5 for 15 Oct), 0 usable settled files (the only 14 outcomes were written by the runs that stored
them), 160 files open, 4 of them settled that way on 14 Oct and running again on 15 Oct. The 28-date minimum
can be met on 10 November 2026 at the earliest, if one run is stored every day; the file counts take longer,
because a file has to run a week or more and then finish before it counts.

**The starting picture, 2026-10-14** (`scripts/watch.py --risk-only`, 7-day window): 158 files scored,
151 of them still running and therefore predicted — 52 critical, 55 watch, 32 on target, 12 too few
checked. 2,058,760 pieces checked in the window, 24% of them in files at the critical level.

**The thresholds this baseline tests.** Amber is VistaQ's own (`dbo.Threshold`: DHU 3.85, reject 1%).
Red (DHU 6, reject 2%) is provisional and unvalidated — the backtest is the first evidence about
whether it means anything, and the QC interviews are the second.

## I5 — int8 ONNX bge-m3 against fp32: parity measured, not switched (2026-10-15)

`scripts/export_embedder_onnx.py` (opset 17, `quantize_dynamic` QInt8, 418 s, 569 MB) then `--check`
(`EMBED_THREADS=3`, below-normal priority, while the app served). 1,000 texts: all 250 chunks the index holds,
124 golden and retrieval questions, 626 of 3,837 table documents spread over the databases.

| int8 vs stored fp32 | n | min | p1 | mean |
|---|---:|---:|---:|---:|
| all texts | 1000 | 0.96648 | 0.97282 | 0.98150 |
| ≤ 512 tokens | 978 | 0.96896 | 0.97364 | 0.98163 |
| > 512 tokens | 22 | 0.96648 | 0.96707 | 0.97554 |
| chunks | 250 | 0.96648 | 0.96944 | 0.97872 |
| questions | 124 | 0.97501 | 0.97715 | 0.98455 |
| table documents | 626 | 0.96930 | 0.97542 | 0.98200 |

- **Sanity:** fresh sentence-transformers fp32 vs stored, and the fp32 ONNX export vs sentence-transformers,
  1.00000 on 100 texts: the stored vectors are current and the export is exact, so the whole loss is the
  int8 quantization. No text is stale; all 1,000 are below 0.99.
- **Hit rate**, 30 retrieval cases: 29/30 three ways (fp32/fp32, int8 queries on fp32 documents, int8/int8),
  the same miss each time (the endpoint-fleet question wants page 39 and gets 40).
- **Speed** on the same 100 texts: 180 → 225 tokens/s, **1.3×** (the int8 reranker gained 2.7×).

**Verdict: the PLAN M7 gate fails on cosine** (min 0.966 against ≥ 0.99), though not on hit rate (0 points
dropped). With 1.3× the gain does not pay for a second backend either. The live index stays `st-fp32`;
EMBED_BACKEND is unchanged. Bulk loads that need speed should use a GPU (`embed_backend` will record it).
Per-channel or MatMul-only quantization might lift the cosine; it is not worth trying for 1.3×. The fp32
export (2.3 GB) was deleted after the check, as the script says; the int8 export stays in `data/models/`.
