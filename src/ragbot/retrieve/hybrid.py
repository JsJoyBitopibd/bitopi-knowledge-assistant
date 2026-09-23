"""Reciprocal Rank Fusion of vector and keyword result lists."""
from __future__ import annotations


def rrf(ranked_lists: list[list[str]], k: int = 60) -> list[str]:
    score: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, cid in enumerate(ranked, start=1):
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(score, key=score.get, reverse=True)  # type: ignore[arg-type]
