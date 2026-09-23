"""Retrieval-only check: is the expected page in the top-k? Uses tests/retrieval_cases.jsonl
({"q":..., "source":..., "page":...} with PHYSICAL page numbers; optional "accept": [[source, page], ...]
lists alternative pages that also count as a hit). Usage: python scripts/hit_rate.py [--k 6] [--cases path]"""
import argparse, json, _path  # noqa: F401
from pathlib import Path
from ragbot.retrieve.retriever import retrieve

ROOT = Path(__file__).resolve().parents[1]


def wanted(c: dict) -> set[tuple[str, int]]:
    out = {(c["source"], int(c["page"]))} if c.get("source") else set()
    for s, p in c.get("accept", []):
        out.add((s, int(p)))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=None); ap.add_argument("--cases", default="tests/retrieval_cases.jsonl")
    a = ap.parse_args()
    cases = [json.loads(l) for l in (ROOT / a.cases).read_text(encoding="utf-8").splitlines() if l.strip()]
    hits, misses = 0, []
    for c in cases:
        got = [(x.source, x.page) for x in retrieve(c["q"], top_k=a.k)]
        if wanted(c) & set(got):
            hits += 1
        else:
            misses.append((c["q"], sorted(wanted(c)), got[:4]))
    print(f"hit rate: {hits}/{len(cases)} = {hits / max(1, len(cases)):.0%}")
    for q, want, got in misses:
        print(f"  MISS  want {want}  got {got}  <- {q}")


if __name__ == "__main__":
    main()
