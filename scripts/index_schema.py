"""Build the schema index vectors (Phase C3): data/index/schema/<db>.npz for every catalog that has a
discovered tier (config/catalog/discovered/<db>.json from scripts/discover_schema.py --json).
Embeds one short document per offered table with bge-m3 — minutes on CPU, once per discovery run.
Without this file schema search still works, keyword-only.

Usage: python scripts/index_schema.py [database ...]
       python scripts/index_schema.py --try "how many suppliers do we have?"   (show the selection)
       python scripts/index_schema.py --check     (recall on tests/schema_cases.jsonl, hybrid vs keyword)
"""
import argparse, json, time, _path  # noqa: F401
from pathlib import Path

from ragbot.data.catalog import load_catalogs
from ragbot.data import schema_index as si


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("databases", nargs="*")
    ap.add_argument("--try", dest="question", help="print the tables selected for this question and exit")
    ap.add_argument("--check", action="store_true", help="recall of the labelled cases in tests/schema_cases.jsonl")
    ap.add_argument("-k", type=int, default=6)
    a = ap.parse_args()
    cats = load_catalogs()
    names = a.databases or [n for n, c in cats.items() if c.tables]
    if a.check:
        check(cats, a.k)
        return
    if a.question:
        from ragbot.retrieve.retriever import _embed_query
        qvec = list(_embed_query(a.question))
        for n in names:
            idx = si.get_index(cats[n])
            sel = si.select_tables(a.question, cats[n], a.k, qvec)
            print(f"{n} ({'hybrid' if idx.vectors is not None else 'keyword-only'}): {sel}")
            for h in si.join_hints(sel, cats[n]):
                print(f"    join hint: {h}")
        return
    for n in names:
        cat = cats[n]
        t0 = time.perf_counter()
        idx = si.build(cat, embed=True)
        print(f"{n}: {len(idx.names)} tables embedded in {time.perf_counter() - t0:.0f} s -> {si.vectors_path(n)}")


def check(cats, k: int) -> None:
    """A case is a hit when any acceptable table is in the top-k search results (FK extras not counted)."""
    from ragbot.retrieve.retriever import _embed_query
    f = Path(__file__).resolve().parents[1] / "tests" / "schema_cases.jsonl"
    cases = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    hits = {"keyword": 0, "hybrid": 0}
    for c in cases:
        cat = cats[c["db"]]
        idx = si.get_index(cat)
        want = {t.lower() for t in c["accept"]}
        row = []
        for mode, qvec in (("keyword", None), ("hybrid", list(_embed_query(c["q"])))):
            got = idx.search(c["q"], k, qvec)
            ok = any(g.lower() in want for g in got)
            hits[mode] += ok
            row.append(f"{mode}={'HIT ' if ok else 'miss'}")
        print(f"  {' '.join(row)}  {c['q']!r}  top: {idx.search(c['q'], 3, list(_embed_query(c['q'])))}")
    for mode, n in hits.items():
        print(f"{mode:8s} recall@{k}: {n}/{len(cases)} = {n / len(cases):.0%}")


if __name__ == "__main__":
    main()
