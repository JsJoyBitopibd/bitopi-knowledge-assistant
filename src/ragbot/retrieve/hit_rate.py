"""Retrieval hit rate: is an expected page among the top-k chunks? Run by scripts/hit_rate.py, and by the
embedder parity check (scripts/export_embedder_onnx.py --check), which runs it with other embedders and stores.

A case is {"q": ..., "source": ..., "page": ...} with PHYSICAL page numbers; optional "accept":
[[source, page], ...] lists alternative pages that also count as a hit (tests/retrieval_cases.jsonl).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

Page = tuple[str, int]
Search = Callable[[str], list[Page]]          # question -> (source, page) of the chunks found, best first


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def wanted(case: dict) -> set[Page]:
    out = {(case["source"], int(case["page"]))} if case.get("source") else set()
    for s, p in case.get("accept", []):
        out.add((s, int(p)))
    return out


@dataclass
class HitRate:
    hits: int
    total: int
    misses: list[tuple[str, list[Page], list[Page]]] = field(default_factory=list)   # (question, wanted, top 4 found)

    def summary(self) -> str:
        return f"hit rate: {self.hits}/{self.total} = {self.hits / max(1, self.total):.0%}"

    def lines(self) -> list[str]:
        """What scripts/hit_rate.py prints: the summary, then one line per miss."""
        return [self.summary()] + [f"  MISS  want {want}  got {got}  <- {q}" for q, want, got in self.misses]


def hit_rate(cases: Iterable[dict], search: Search) -> HitRate:
    hits, total, misses = 0, 0, []
    for c in cases:
        total += 1
        got = list(search(c["q"]))
        if wanted(c) & set(got):
            hits += 1
        else:
            misses.append((c["q"], sorted(wanted(c)), got[:4]))
    return HitRate(hits, total, misses)


def retriever_search(k: Optional[int] = None) -> Search:
    """The app's document search for an unrestricted user (top k, default retrieval.final_top_k)."""
    from ..auth.models import Scope
    from .retriever import retrieve
    return lambda q: [(x.source, x.page) for x in retrieve(q, top_k=k, scope=Scope.unrestricted())]
