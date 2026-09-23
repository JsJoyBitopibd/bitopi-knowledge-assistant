"""Full rebuild of the index (required when the embedding model changes).
Usage: set EMBED_MODEL in .env first, then: python scripts/reindex.py --yes"""
import shutil, sys, _path  # noqa: F401
from ragbot.config import settings
if "--yes" not in sys.argv:
    print("This deletes data/index and re-embeds every PDF. Re-run with --yes."); sys.exit(1)
idx = settings().path("index_dir")
for p in ("chroma", "registry.db", "bm25.pkl"):
    target = idx / p
    if target.is_dir(): shutil.rmtree(target)
    elif target.exists(): target.unlink()
from ragbot.ingest.pipeline import ingest_folder
print(ingest_folder())
