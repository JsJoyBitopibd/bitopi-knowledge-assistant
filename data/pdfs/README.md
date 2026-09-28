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

## Who may see a document

Every document carries four access attributes; a user sees it only when all four are inside their
scope (config/scopes.yaml):

- factory: the first folder's name when it is a factory code (TAL/, RHL/, BGL/, KTL/, CKDL/),
  otherwise ALL, which every user sees. So SOP/ documents are visible to all staff and TAL/
  documents only to users with TAL.
- department: Common unless set; Common documents are visible to every department.
- confidentiality: internal unless set; public, internal or restricted.
- buyer_code: empty unless set (buyer accounts come later).

To change them for a folder, put a meta.yaml in it. It applies to every file below, and a deeper
folder's meta.yaml wins:

  # data/pdfs/HR/meta.yaml
  department: HR
  confidentiality: restricted

  # data/pdfs/Buyer/MARCO/meta.yaml
  factory: TAL
  buyer_code: MARCO

Editing a meta.yaml or moving a file retags the documents on the next ingest pass; nothing is
re-embedded. An invalid meta.yaml makes its new files fail (listed by
`python scripts/inspect.py --failed`) and leaves already indexed files as they were.
