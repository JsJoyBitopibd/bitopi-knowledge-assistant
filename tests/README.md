- golden.jsonl: end-to-end cases for scripts/eval.py. Fields: id, kind (direct|paraphrase|code|table|data|both|not_found|refuse|follow_up),
  q, expected (short plain sentence), source+page (PDF cases), view (data cases), category (optional filter),
  expected_not_found (true for cases the system must decline). The seven rows here are TEMPLATES that
  reference an invented SOP; replace them with cases from your real PDFs and views before trusting the scores.
- retrieval_cases.jsonl: retrieval-only cases for scripts/hit_rate.py (q, source, page).
- test_*.py: unit tests (pytest -q). Add tests/test_guard.py, test_chunker.py, test_citations.py, test_rrf.py in M1–M4.
