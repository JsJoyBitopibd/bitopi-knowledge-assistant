# Changelog

All notable changes to the Bitopi Knowledge Assistant. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/). Each released version has a git tag (`vX.Y.Z`).

Where things stand right now (done / in progress / next) is in `docs/PROGRESS.md`. The design behind
each phase is in `docs/ROADMAP.md`.

## [Unreleased] — Phase B: streaming, parallelism, caching (target v1.2.0)

Work in progress on branch `feature/phase-b-streaming`. Entries are added here as each task is
verified; see `docs/PROGRESS.md` for per-task status and evidence.

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
