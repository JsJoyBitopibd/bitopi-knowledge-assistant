"""BM25 keyword index over all chunks (exact codes, form numbers, rare terms, Bangla words).
Rebuilt after each ingest and pickled to data/index/bm25.pkl."""
from __future__ import annotations

import pickle
import re
from functools import lru_cache
from pathlib import Path

from rank_bm25 import BM25Okapi

from ..config import settings

# latin words/digits, Bangla words; 'PCD-02' -> ['pcd', '02'], 'FileRef 4471' -> ['fileref', '4471']
_TOKEN = re.compile(r"[a-z0-9]+|[ঀ-৿]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class KeywordIndex:
    def __init__(self, ids: list[str], texts: list[str]):
        self.ids = ids
        self.bm25 = BM25Okapi([tokenize(t) for t in texts]) if texts else None

    def search(self, question: str, k: int = 20) -> list[tuple[str, float]]:
        if not self.bm25:
            return []
        scores = self.bm25.get_scores(tokenize(question))
        order = sorted(range(len(scores)), key=lambda i: -scores[i])[:k]
        return [(self.ids[i], float(scores[i])) for i in order if scores[i] > 0]


def _path() -> Path:
    return settings().path("index_dir") / "bm25.pkl"


def rebuild_keyword_index(store, index_dir: Path | None = None) -> KeywordIndex:
    """index_dir: where bm25.pkl goes (default: the app's index; a test ingest passes its own)."""
    ids, texts = [], []
    for cid, text in store.all_ids_and_texts():
        ids.append(cid); texts.append(text)
    idx = KeywordIndex(ids, texts)
    path = (index_dir / "bm25.pkl") if index_dir else _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(idx, f)
    get_keyword_index.cache_clear()
    return idx


@lru_cache(maxsize=1)
def get_keyword_index() -> KeywordIndex:
    p = _path()
    if not p.exists():
        return KeywordIndex([], [])
    with open(p, "rb") as f:
        return pickle.load(f)
