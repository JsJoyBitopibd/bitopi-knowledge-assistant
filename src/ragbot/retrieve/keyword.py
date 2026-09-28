"""Keyword (BM25) search over all chunks: exact codes, form numbers, rare terms, Bangla words.

Phase D1: the index is the `chunk_fts` SQLite FTS5 table in data/index/registry.db, kept up to date
per document by store.Registry (replace_chunks / remove_document / set_superseded) — no full rebuild
after an ingest, no in-memory copy. The old design (rank_bm25 over every chunk, pickled to bm25.pkl)
scored all chunks in Python on every question and would have been a 1–2 GB pickle at 60K pages.

`KeywordIndex` (in-memory rank_bm25) stays for small corpora built on the fly — the schema index
(data/schema_index.py) uses it for table documents.
"""
from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from rank_bm25 import BM25Okapi

from ..config import settings

# latin words/digits, Bangla words; 'PCD-02' -> ['pcd', '02'], 'FileRef 4471' -> ['fileref', '4471']
_TOKEN = re.compile(r"[a-z0-9]+|[ঀ-৿]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


# English function words left out of FTS queries: they match nearly every chunk, so BM25 has to score
# most of the table while they add ~nothing to the ranking. Measured at 60K chunks (2026-09-27):
# query median 57 ms -> 17 ms with an identical top-20. Bangla terms are never dropped.
_STOP = frozenset("""a an the is are was were be been of in on at to for from by with and or not no it its this
that these those what which who whom whose when where why how do does did can could should would will shall
may might must have has had our we you your their they them there here as if than then so such any all each
every per about into over under up down out""".split())


class KeywordIndex:
    """In-memory BM25 over a small list of texts."""

    def __init__(self, ids: list[str], texts: list[str]):
        self.ids = ids
        self.bm25 = BM25Okapi([tokenize(t) for t in texts]) if texts else None

    def search(self, question: str, k: int = 20, where: Optional[dict[str, Any]] = None) -> list[tuple[str, float]]:
        if not self.bm25:
            return []
        scores = self.bm25.get_scores(tokenize(question))
        order = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
        return [(self.ids[i], float(scores[i])) for i in order if scores[i] > 0]


# chunk_fts columns a retrieval filter may restrict to a list of values (store.FTS_DDL)
_LIST_COLUMNS = ("category", "source", "factory", "department", "confidentiality", "buyer_code")


class FtsKeywordIndex:
    """BM25 over the registry's chunk_fts table. A fresh read-only connection per search: searches run
    in a worker thread (retriever.py), and sqlite3 connections must not cross threads."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def search(self, question: str, k: int = 20, where: Optional[dict[str, Any]] = None) -> list[tuple[str, float]]:
        all_terms = list(dict.fromkeys(tokenize(question)))
        terms = [t for t in all_terms if t not in _STOP] or all_terms   # a question of only stopwords keeps them
        if not terms or not self.db_path.exists():
            return []
        # quoted terms joined by OR: any token may match, BM25 ranks by how many and how rare
        sql = "SELECT id, bm25(chunk_fts) FROM chunk_fts WHERE chunk_fts MATCH ?"
        args: list[Any] = [" OR ".join(f'"{t}"' for t in terms)]
        # The retrieval filters are applied inside the search, so k results survive them (filtering
        # after the merge used to shrink the candidate list silently). An unknown key is an error, not
        # skipped: a filter this index cannot apply would otherwise let out-of-scope chunks through.
        for col, val in (where or {}).items():
            if col == "superseded":
                sql += " AND superseded = ?"; args.append(int(bool(val)))
            elif col in _LIST_COLUMNS:
                vals = list(val) if isinstance(val, (list, set, tuple)) else [val]
                if not vals:
                    return []                                   # nothing allowed: no candidates
                sql += f" AND {col} IN ({','.join('?' * len(vals))})"; args += vals
            else:
                raise ValueError(f"the keyword index cannot filter on {col!r}")
        sql += " ORDER BY bm25(chunk_fts) LIMIT ?"
        args.append(int(k))
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []          # a registry from before D1 that has not been opened for writing yet
        finally:
            con.close()
        return [(cid, -float(score)) for cid, score in rows]   # FTS5 bm25(): lower is better


@lru_cache(maxsize=1)
def get_keyword_index() -> FtsKeywordIndex:
    """The app's keyword index. On first use after upgrading from the pickled BM25, fill chunk_fts
    from the vector store once (a registry without it would otherwise return no keyword hits)."""
    from ..store import Registry, get_store
    path = settings().path("index_dir") / "registry.db"
    if path.exists():
        seeded = Registry(path).seed_fts(get_store())
        if seeded:
            import logging
            logging.getLogger("ingest").info("keyword index (chunk_fts) seeded with %d chunks", seeded)
    return FtsKeywordIndex(path)
