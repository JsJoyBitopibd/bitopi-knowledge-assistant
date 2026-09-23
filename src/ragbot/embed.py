"""Local embeddings (bge-m3) and reranker (bge-reranker-v2-m3). Never leave the network.

The embedding model name is written into the index metadata; store.py refuses to open an index
built with a different model (non-negotiable #6).
"""
from __future__ import annotations

import os
from functools import lru_cache

import numpy as np

EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "sentence_transformers")


class Embedder:
    """embed(texts) -> list of unit-length float vectors (1,024-d for bge-m3)."""

    def __init__(self) -> None:
        self.model_name = EMBED_MODEL
        if EMBED_BACKEND == "ollama":
            import ollama  # noqa: F401
            self._impl = self._ollama
        else:
            from sentence_transformers import SentenceTransformer
            self._st = SentenceTransformer(EMBED_MODEL)
            self._impl = self._st_embed

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = np.asarray(self._impl(texts), dtype=np.float32)
        vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12)
        return vecs.tolist()

    def _st_embed(self, texts: list[str]):
        return self._st.encode(texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False)

    def _ollama(self, texts: list[str]):
        import ollama
        client = ollama.Client(host=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
        return client.embed(model=EMBED_MODEL.split("/")[-1], input=texts)["embeddings"]


class Reranker:
    """score(question, texts) -> relevance scores; higher is better."""

    def __init__(self) -> None:
        from sentence_transformers import CrossEncoder
        self._ce = CrossEncoder(RERANK_MODEL, max_length=1024)

    def score(self, question: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        return [float(s) for s in self._ce.predict([(question, t) for t in texts])]


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    return Embedder()


@lru_cache(maxsize=1)
def get_reranker() -> Reranker | None:
    if os.getenv("RERANK_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return None
    return Reranker()
