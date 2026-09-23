# Prompt changelog
- v1 (2026-09-22): initial system_answer, strict, rewrite, router, sql_generate, refusal, not_found.
- v2 (2026-09-22): system_answer + strict: one marker per bracket rule, scope widened to IT policies/SOPs/forms; router: IT/SOP/form vocabulary, PPM meetings as data; new chitchat.txt (moved out of orchestrator.py).
- v3 (2026-09-23): router: count-phrased policy questions (licences/laptops/backups/retention/working days) are documents, not data — closes the 83%->85% correctness gap where "how many X licences" was routed to the database.
