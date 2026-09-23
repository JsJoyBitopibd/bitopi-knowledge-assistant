"""Document retrieval: scoped hybrid search -> fetch -> filter -> rerank -> top-k chunks."""
from __future__ import annotations

from typing import Any, Optional

from ..config import settings
from ..embed import get_embedder, get_reranker
from ..models import Chunk
from ..store import get_store
from .hybrid import rrf
from .keyword import get_keyword_index


def _passes(chunk: Chunk, where: dict[str, Any]) -> bool:
    for key, val in where.items():
        have = getattr(chunk, key, None)
        allowed = val if isinstance(val, (list, set, tuple)) else [val]
        if have not in allowed:
            return False
    return True


def retrieve(question: str, where: Optional[dict[str, Any]] = None, top_k: Optional[int] = None) -> list[Chunk]:
    """where: {"category": ["SOP", "TAL"], "superseded": False} — applied to BOTH search paths.
    Returns up to top_k chunks, best first, each with .score set."""
    s = settings()
    where = {**s.get("retrieval.default_filters", {}), **(where or {})}
    store, kw = get_store(), get_keyword_index()

    qvec = get_embedder().embed([question])[0]
    # Chroma needs a single-clause or $and dict; equality-only filter here, lists handled post-hoc.
    chroma_where = {k: v for k, v in where.items() if not isinstance(v, (list, set, tuple))}
    if len(chroma_where) > 1:
        chroma_where = {"$and": [{k: v} for k, v in chroma_where.items()]}
    dense = [c.id for c in store.query(qvec, s["retrieval.vector_top_k"], chroma_where or None)]
    sparse = [cid for cid, _ in kw.search(question, s["retrieval.keyword_top_k"])]

    merged = rrf([dense, sparse], s["retrieval.rrf_k"])[: s["retrieval.rerank_candidates"]]
    chunks = [c for c in store.get(merged) if _passes(c, where)]
    k = top_k or s["retrieval.final_top_k"]

    rr = get_reranker()
    if rr and chunks:
        scores = rr.score(question, [c.text for c in chunks])
        for c, sc in zip(chunks, scores):
            c.score = sc
        chunks.sort(key=lambda c: -c.score)
    return chunks[:k]
