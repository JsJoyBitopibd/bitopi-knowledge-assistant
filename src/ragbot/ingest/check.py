"""scripts/inspect.py --check: do the registry, the keyword index (chunk_fts) and the vector store agree?

Every document is written to the three stores one after the other, so a crash or a bug between two
writes leaves one of them behind. The report names what disagrees; a re-ingest of the affected
documents (delete the document row, run scripts/ingest.py) or scripts/reindex.py repairs it.
"""
from __future__ import annotations

from collections import Counter

from ..store import TAG_FIELDS, Registry


def check_index(reg: Registry, store) -> dict[str, list[str]]:
    """{problem: sorted chunk ids or sources}; every list is empty when the three stores agree."""
    cols = ", ".join(TAG_FIELDS)
    chunk_src = {cid: src for cid, src in reg.db.execute("SELECT id, source FROM chunk")}
    fts = {r[0]: (bool(r[1]), *r[2:]) for r in reg.db.execute(f"SELECT id, superseded, {cols} FROM chunk_fts")}
    # only the compared fields are kept: 145K full metadata dicts would take hundreds of MB
    vec = {cid: (bool(m.get("superseded")), *(m.get(k) for k in TAG_FIELDS))
           for cid, m in store.all_ids_and_metadata()}
    docs = {r[0]: (r[1], (bool(r[2]), *r[3:])) for r in reg.db.execute(
        f"SELECT source, doc_hash, superseded, {cols} FROM document")}
    per_doc = Counter(chunk_src.values())
    out = {
        "registry chunks missing from the vector store": sorted(set(chunk_src) - set(vec)),
        "vector store chunks missing from the registry": sorted(set(vec) - set(chunk_src)),
        "registry chunks missing from the keyword index": sorted(set(chunk_src) - set(fts)),
        "keyword index chunks missing from the registry": sorted(set(fts) - set(chunk_src)),
        "indexed documents without chunks": sorted(s for s, (h, _) in docs.items() if h and not per_doc[s]),
        "chunks left from a document with no indexed version": sorted(s for s, (h, _) in docs.items()
                                                                      if not h and per_doc[s]),
        "chunks of a document the registry does not know": sorted({s for s in chunk_src.values() if s not in docs}),
        "documents whose last write did not finish (the next pass rewrites them)": sorted(reg.dirty_sources()),
        "superseded flag differs from the document's": [],
        "category or access tags differ from the document's": [],
    }
    for cid, src in chunk_src.items():
        if src not in docs:
            continue
        want = docs[src][1]                                 # (superseded, category, factory, ...)
        copies = [c for c in (fts.get(cid), vec.get(cid)) if c is not None]
        if any(c[0] != want[0] for c in copies):
            out["superseded flag differs from the document's"].append(cid)
        if any(tuple(c[1:]) != tuple(want[1:]) for c in copies):
            out["category or access tags differ from the document's"].append(cid)
    for k in ("superseded flag differs from the document's", "category or access tags differ from the document's"):
        out[k].sort()
    return out
