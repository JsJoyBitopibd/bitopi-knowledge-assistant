"""Document retrieval: scoped hybrid search -> fetch -> filter -> rerank -> top-k chunks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import Any, Optional

from .. import trace
from ..auth.filters import SCOPE_FIELDS, scope_where
from ..auth.models import Scope
from ..config import settings
from ..embed import get_embedder, get_reranker
from ..models import Chunk
from ..store import get_store
from .hybrid import rrf
from .keyword import get_keyword_index


_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="retrieve")


@lru_cache(maxsize=512)
def _embed_query(question: str) -> tuple[float, ...]:
    """Query vectors are deterministic for a given model, so a repeated or retried question (the
    documents retry after a data miss, eval reruns, the same question from two users) embeds once."""
    return tuple(get_embedder().embed([question])[0])


def chroma_filter(where: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The whole filter for the vector query, lists included (`{"category": {"$in": [...]}}`), so the
    top-k it returns all pass. Lists used to be applied only after the search, which silently shrank
    the candidate set: with a category filter, most of the 20 nearest chunks could be dropped (D2)."""
    clauses = []
    for k, v in where.items():
        if isinstance(v, (list, set, tuple)):
            vals = list(v)
            clauses.append({k: {"$in": vals}} if len(vals) != 1 else {k: vals[0]})
        else:
            clauses.append({k: v})
    if not clauses:
        return None
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def _passes(chunk: Chunk, where: dict[str, Any]) -> bool:
    for key, val in where.items():
        have = getattr(chunk, key, None)
        allowed = val if isinstance(val, (list, set, tuple)) else [val]
        if have not in allowed:
            return False
    return True


def retrieve(question: str, where: Optional[dict[str, Any]] = None, top_k: Optional[int] = None, *,
             scope: Scope) -> list[Chunk]:
    """where: {"category": ["SOP", "TAL"], "superseded": False} — applied to BOTH search paths.
    scope: the user's (PRD FR-4.3), merged last so `where` can only narrow it; there is no default, so
    a call that forgets it fails instead of searching everything. Returns up to top_k chunks, best
    first, each with .score set."""
    s = settings()
    clash = sorted(set(where or {}) & set(SCOPE_FIELDS))
    if clash:
        raise ValueError(f"access fields are set by the scope, not by the caller: {clash}")
    where = {**s.get("retrieval.default_filters", {}), **(where or {}), **scope_where(scope)}
    with trace.span("retrieve"):
        store, kw = get_store(), get_keyword_index()

        # Keyword search is independent of the embedding, so run it alongside embed + vector search. It
        # applies the filters itself (category, superseded), so its top-k are all usable.
        sparse_f = trace.submit(_POOL, _timed, "retrieve.keyword", kw.search, question,
                                s["retrieval.keyword_top_k"], where)
        with trace.span("retrieve.embed"):
            qvec = list(_embed_query(question))
        with trace.span("retrieve.vector"):
            dense = [c.id for c in store.query(qvec, s["retrieval.vector_top_k"], chroma_filter(where))]
        sparse = [cid for cid, _ in sparse_f.result()]

        merged = rrf([dense, sparse], s["retrieval.rrf_k"])[: s["retrieval.rerank_candidates"]]
        with trace.span("retrieve.fetch"):
            chunks = [c for c in store.get(merged) if _passes(c, where)]
        k = top_k or s["retrieval.final_top_k"]

        rr = get_reranker()
        if rr and chunks:
            with trace.span("retrieve.rerank"):
                scores = rr.score(question, [c.text for c in chunks])
            for c, sc in zip(chunks, scores):
                c.score = sc
            chunks.sort(key=lambda c: -c.score)
        return chunks[:k]


def _timed(key: str, fn, *args):
    with trace.span(key):
        return fn(*args)
