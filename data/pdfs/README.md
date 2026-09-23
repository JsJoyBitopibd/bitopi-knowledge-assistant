Put PDFs here. Each first-level subfolder becomes the document's *category* tag, shown in
references and usable as a filter. Suggested:

  data/pdfs/SOP/        company SOPs and policies (apply to all factories)
  data/pdfs/TAL/        documents specific to Tarasima Apparels
  data/pdfs/RHL/        Remi Holdings
  data/pdfs/BGL/        Baridhi Garments
  data/pdfs/Buyer/      buyer comments, tech packs, POs
  data/pdfs/QA/         inspection reports

Files directly in data/pdfs/ get category "General". Nested folders below the first level are
allowed; only the first level is the category. Re-run `python scripts/ingest.py` after adding,
replacing or removing files; unchanged files are skipped. Scanned PDFs are OCR'd (needs Tesseract).
