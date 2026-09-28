"""Look inside the index.

    python scripts/inspect.py --stats | --failed | --check | --chunk <id> | --sample N | --search "q"

--failed lists files that could not be indexed (and same-name files that were skipped); --check compares
the registry, the keyword index and the vector store and exits 1 when they disagree (for a scheduler).
"""
import argparse, json, random, sys, _path  # noqa: F401
from ragbot.config import settings
from ragbot.store import Registry, get_store

p = argparse.ArgumentParser()
p.add_argument("--stats", action="store_true"); p.add_argument("--failed", action="store_true")
p.add_argument("--check", action="store_true")
p.add_argument("--chunk"); p.add_argument("--sample", type=int); p.add_argument("--search")
a = p.parse_args()
reg = Registry()
if a.stats or not any(vars(a).values()):
    print(reg.stats())
if a.failed:
    from ragbot.ingest.pipeline import MAX_ATTEMPTS
    rows = reg.db.execute("SELECT source, error, attempts, doc_hash FROM document WHERE status='failed' ORDER BY source")
    for source, error, attempts, indexed in rows:
        kept = "the previous version is still indexed" if indexed else "not indexed"
        tries = (f"attempt {attempts} of {MAX_ATTEMPTS}" + ("; not retried until the file changes"
                                                           if attempts >= MAX_ATTEMPTS else "")
                 if attempts else "retried on every pass")
        print(f"{source}: {error}\n    {kept}; {tries}")
    try:
        scan = json.loads((settings().path("index_dir") / "last_scan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        scan = {}
    for dup in scan.get("duplicates", []):
        print(f"{dup}: skipped, a file with the same name is indexed from another folder; rename one of them")
if a.check:
    from ragbot.ingest.check import check_index
    problems = check_index(reg, get_store())
    s = reg.stats()
    print(f"registry: {s['documents']} documents, {s['chunks']} chunks; vector store: {get_store().count()} chunks")
    bad = {k: v for k, v in problems.items() if v}
    for k, v in bad.items():
        print(f"MISMATCH {k}: {len(v)} (e.g. {', '.join(v[:20])})")
    if bad:
        print("repair: python scripts/ingest.py --redo <file name> ... for the documents named "
              "(nothing is re-embedded); chunks of unknown documents need scripts/reindex.py --yes")
        sys.exit(1)
    print("OK: the registry, the keyword index and the vector store agree")
if a.chunk:
    for c in get_store().get([a.chunk]):
        print(c.metadata()); print(c.text)
if a.sample:
    ids = [r[0] for r in reg.db.execute("SELECT id FROM chunk")]
    for c in get_store().get(random.sample(ids, min(a.sample, len(ids)))):
        print("=" * 80); print(c.id, "|", c.section, "| p", c.page); print(c.text[:600])
if a.search:
    from ragbot.auth.models import Scope
    from ragbot.retrieve.retriever import retrieve
    for c in retrieve(a.search, scope=Scope.unrestricted()):   # an operator's tool: every document
        print(f"{c.score:6.3f}  {c.source}  p{c.page}  {c.section}  [{c.factory}/{c.confidentiality}]")
