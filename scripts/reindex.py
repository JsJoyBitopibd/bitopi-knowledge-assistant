"""Full rebuild of the index (required when the embedding model changes).
Usage: set EMBED_MODEL in .env first, then: python scripts/reindex.py --yes"""
import shutil, sys, _path  # noqa: F401
from ragbot.config import settings
if "--yes" not in sys.argv:
    print("This deletes data/index and re-embeds every PDF. Re-run with --yes."); sys.exit(1)
root = settings().path("pdf_root")
if not root.is_dir() or next(root.rglob("*.pdf"), None) is None:
    # Checked before deleting anything: with the share offline, a rebuild would leave an empty index.
    print(f"No PDFs found under {root}: the index was not deleted (is the share mounted?)"); sys.exit(2)
idx = settings().path("index_dir")
from ragbot.ingest.lock import IndexLocked, index_lock
from ragbot.ingest.priority import run_in_background
print("running in the background:", run_in_background())   # the app stays responsive during the rebuild
try:
    with index_lock():                  # not while the worker or ingest.py is writing the index
        for p in ("chroma", "registry.db", "bm25.pkl"):
            target = idx / p
            if target.is_dir(): shutil.rmtree(target)
            elif target.exists(): target.unlink()
        from ragbot.store import get_store
        get_store.cache_clear()   # the cached client still points at the directory just deleted
        from ragbot.ingest.pipeline import ingest_folder
        print(ingest_folder())
except IndexLocked as e:
    print(f"{e}: the index was not deleted; try again when it has finished"); sys.exit(3)
