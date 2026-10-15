"""Retrieval-only check: is the expected page in the top-k? Uses tests/retrieval_cases.jsonl
({"q":..., "source":..., "page":...} with PHYSICAL page numbers; optional "accept": [[source, page], ...]
lists alternative pages that also count as a hit). Usage: python scripts/hit_rate.py [--k 6] [--cases path]
The loop lives in src/ragbot/retrieve/hit_rate.py (the embedder parity check runs it too)."""
import argparse, _path  # noqa: F401
from pathlib import Path
from ragbot.retrieve.hit_rate import hit_rate, load_cases, retriever_search

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=None); ap.add_argument("--cases", default="tests/retrieval_cases.jsonl")
    a = ap.parse_args()
    print("\n".join(hit_rate(load_cases(ROOT / a.cases), retriever_search(a.k)).lines()))


if __name__ == "__main__":
    main()
