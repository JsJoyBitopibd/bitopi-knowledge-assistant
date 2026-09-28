"""Index every PDF under data/pdfs (idempotent).

    python scripts/ingest.py [folder] [--confirm-removals] [--redo FILE [FILE ...]]

A file missing from the folder is removed from the index only when two consecutive runs miss it.
--confirm-removals removes every missing file now (for a deliberate bulk removal, which the normal
run refuses when more than 5% of the files vanish at once). A missing or empty folder is always
refused: exit code 2 and an ALERT line. --redo re-indexes the named files even though they did not
change (the repair for what `scripts/inspect.py --check` reports; unchanged chunks are not re-embedded).
Exit code 3: another ingest (the worker, reindex.py) is writing the index.
"""
import argparse, sys, _path  # noqa: F401
from pathlib import Path
from ragbot.ingest.lock import IndexLocked, index_lock
from ragbot.ingest.pipeline import ingest_folder

p = argparse.ArgumentParser(usage=__doc__.splitlines()[2].strip())
p.add_argument("folder", nargs="?")
p.add_argument("--confirm-removals", action="store_true")
p.add_argument("--redo", nargs="+", metavar="FILE", default=[])
a = p.parse_args()
from ragbot.ingest.priority import run_in_background
run_in_background()                          # lower priority, fewer threads, pause while the app searches
try:
    with index_lock():                       # never at the same time as the worker or reindex.py
        r = ingest_folder(Path(a.folder) if a.folder else None, confirm_removals=a.confirm_removals,
                          redo={Path(f).name for f in a.redo})
except IndexLocked as e:
    print(f"{e}; try again when it has finished")
    sys.exit(3)
if r.get("alert"):
    print("ALERT:", r["alert"])
    sys.exit(2)
print(f"added={r['added']} updated={r['updated']} removed={r['removed']} retagged={r['retagged']} failed={r['failed']} | "
      f"documents={r['documents']} pages={r['pages']} chunks={r['chunks']} tables={r['tables']} ocr_pages={r['ocr_pages']}")
if r["pending_removal"]:
    print(f"{r['pending_removal']} file(s) missing from the folder: removed if the next run misses them too "
          "(or run again with --confirm-removals)")
for dup in r.get("duplicates", []):
    print(f"skipped {dup}: a file with the same name is already indexed from another folder; rename one of them")
if r["failed"]:
    print("see logs/ingest.log for failures; `python scripts/inspect.py --failed` lists them"
          + (f" ({r['retry']} will be retried on the next run)" if r.get("retry") else ""))
