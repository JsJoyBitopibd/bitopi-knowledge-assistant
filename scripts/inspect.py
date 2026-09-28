"""Look inside the index. Usage: python scripts/inspect.py --stats | --failed | --chunk <id> | --sample N | --search "q" """
import argparse, random, _path  # noqa: F401
from ragbot.store import Registry, get_store

p = argparse.ArgumentParser()
p.add_argument("--stats", action="store_true"); p.add_argument("--failed", action="store_true")
p.add_argument("--chunk"); p.add_argument("--sample", type=int); p.add_argument("--search")
a = p.parse_args()
reg = Registry()
if a.stats or not any(vars(a).values()):
    print(reg.stats())
if a.failed:
    for row in reg.db.execute("SELECT source, error FROM document WHERE status='failed'"):
        print(row)
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
