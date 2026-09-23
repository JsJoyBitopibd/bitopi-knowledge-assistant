"""Index every PDF under data/pdfs (idempotent). Usage: python scripts/ingest.py [folder]"""
import sys, _path  # noqa: F401
from pathlib import Path
from ragbot.ingest.pipeline import ingest_folder

root = Path(sys.argv[1]) if len(sys.argv) > 1 else None
r = ingest_folder(root)
print(f"added={r['added']} updated={r['updated']} removed={r['removed']} failed={r['failed']} | "
      f"documents={r['documents']} pages={r['pages']} chunks={r['chunks']} tables={r['tables']} ocr_pages={r['ocr_pages']}")
if r["failed"]:
    print("see logs/ingest.log for failures; `python scripts/inspect.py --failed` lists them")
