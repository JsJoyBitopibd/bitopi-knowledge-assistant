"""Index every PDF under data/pdfs (idempotent).

    python scripts/ingest.py [folder] [--confirm-removals]

A file missing from the folder is removed from the index only when two consecutive runs miss it.
--confirm-removals removes every missing file now (for a deliberate bulk removal, which the normal
run refuses when more than 5% of the files vanish at once). A missing or empty folder is always
refused: exit code 2 and an ALERT line.
"""
import sys, _path  # noqa: F401
from pathlib import Path
from ragbot.ingest.pipeline import ingest_folder

args = [a for a in sys.argv[1:] if not a.startswith("--")]
root = Path(args[0]) if args else None
r = ingest_folder(root, confirm_removals="--confirm-removals" in sys.argv)
if r.get("alert"):
    print("ALERT:", r["alert"])
    sys.exit(2)
print(f"added={r['added']} updated={r['updated']} removed={r['removed']} failed={r['failed']} | "
      f"documents={r['documents']} pages={r['pages']} chunks={r['chunks']} tables={r['tables']} ocr_pages={r['ocr_pages']}")
if r["pending_removal"]:
    print(f"{r['pending_removal']} file(s) missing from the folder: removed if the next run misses them too "
          "(or run again with --confirm-removals)")
if r["failed"]:
    print("see logs/ingest.log for failures; `python scripts/inspect.py --failed` lists them")
