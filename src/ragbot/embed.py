"""Local embeddings (bge-m3) and reranker (bge-reranker-v2-m3). Never leave the network.

The embedding model name is written into the index metadata; store.py refuses to open an index
built with a different model (non-negotiable #6).
"""
from __future__ import annotations

import os
from functools import lru_cache

import numpy as np

from . import config  # noqa: F401  — loads .env before the settings below are read; importing this module
                      # directly (a script, a test) used to ignore .env, e.g. RERANK_BACKEND

EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "sentence_transformers")
# Cross-encoder cost grows with the square of the sequence length, so 512 instead of the model's
# 1024 roughly quarters the CPU time. Chunks are ~600 tokens, so a 512-token window covers the
# question plus most of the chunk; raise it only if scripts/hit_rate.py regresses.
RERANK_MAX_LENGTH = int(os.getenv("RERANK_MAX_LENGTH", "512"))


class Embedder:
    """embed(texts) -> list of unit-length float vectors (1,024-d for bge-m3)."""

    def __init__(self) -> None:
        self.model_name = EMBED_MODEL
        if EMBED_BACKEND == "ollama":
            import ollama  # noqa: F401
            self._impl = self._ollama
        else:
            from sentence_transformers import SentenceTransformer
            _use_all_cpu_threads()
            self._st = SentenceTransformer(EMBED_MODEL)
            self._impl = self._st_embed

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vecs = np.asarray(self._impl(texts), dtype=np.float32)
        vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12)
        return vecs.tolist()

    def _st_embed(self, texts: list[str]):
        return self._st.encode(texts, batch_size=_batch_size("EMBED_BATCH_SIZE"), normalize_embeddings=True,
                               show_progress_bar=False)

    def _ollama(self, texts: list[str]):
        import ollama
        client = ollama.Client(host=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
        return client.embed(model=EMBED_MODEL.split("/")[-1], input=texts)["embeddings"]


class Reranker:
    """score(question, texts) -> relevance scores; higher is better."""

    def __init__(self) -> None:
        from sentence_transformers import CrossEncoder
        _use_all_cpu_threads()
        self._ce = CrossEncoder(RERANK_MODEL, max_length=RERANK_MAX_LENGTH)

    def score(self, question: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        # Small batches on CPU (see _batch_size). Phase A had used one batch of all pairs; measured
        # 2026-09-27, 10 real pairs x 3 questions x 2 runs: batch 10 = 18.7 s, batch 1 = 10.1 s.
        pairs = [(question, t) for t in texts]
        return [float(s) for s in self._ce.predict(pairs, batch_size=_batch_size("RERANK_BATCH_SIZE"),
                                                  show_progress_bar=False)]


class OnnxReranker:
    """The same reranker as int8 ONNX (scripts/export_reranker_onnx.py), run by onnxruntime. Tokenized
    like CrossEncoder (pairs, longest-first truncation to RERANK_MAX_LENGTH) and scored with the same
    sigmoid, so scores are directly comparable with Reranker's. Selected by RERANK_BACKEND=onnx."""

    def __init__(self, model_dir=None) -> None:
        from pathlib import Path
        import onnxruntime as ort
        from transformers import AutoTokenizer
        from .config import ROOT
        d = Path(model_dir) if model_dir else ROOT / "data" / "models" / f"{RERANK_MODEL.split('/')[-1]}-int8"
        if not (d / "model.onnx").exists():
            raise RuntimeError(f"{d / 'model.onnx'} not found — run python scripts/export_reranker_onnx.py")
        self._tok = AutoTokenizer.from_pretrained(str(d))
        so = ort.SessionOptions()
        # onnxruntime's threads busy-wait after each run by default; the question embedding that follows
        # (the next question) then fought them for the cores: 260-340 ms instead of 94-143 ms, with the
        # rerank itself no faster for it (measured 2026-09-28, docs/tuning_log.md)
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self._sess = ort.InferenceSession(str(d / "model.onnx"), sess_options=so, providers=["CPUExecutionProvider"])

    def score(self, question: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        out: list[float] = []
        bs = _batch_size("RERANK_BATCH_SIZE")
        for i in range(0, len(texts), bs):
            part = texts[i:i + bs]
            enc = self._tok([question] * len(part), part, padding=True, truncation="longest_first",
                            max_length=RERANK_MAX_LENGTH, return_tensors="np")
            logits = self._sess.run(["logits"], {"input_ids": enc["input_ids"].astype(np.int64),
                                                 "attention_mask": enc["attention_mask"].astype(np.int64)})[0]
            out += [float(x) for x in 1.0 / (1.0 + np.exp(-logits[:, 0]))]
        return out


def _batch_size(env_name: str) -> int:
    """Texts per forward pass. On CPU, small batches are much faster: every text in a batch is padded
    to the longest one, and the bigger activations fall out of cache. Measured 2026-09-27 on the pilot
    box (bge-m3, 48 real chunks, 2 runs each): batch 1 = 0.96 chunks/s, 4 = 0.65, 32 = 0.52. On a GPU
    large batches win, so the CPU default is 1 and the GPU default 32; override with `env_name`."""
    if os.getenv(env_name):
        return int(os.environ[env_name])
    try:
        import torch
        return 32 if torch.cuda.is_available() else 1
    except Exception:
        return 1


def _use_all_cpu_threads() -> None:
    """torch can default to a conservative thread count inside containers; raise it to at least half
    the logical CPUs (about one per physical core). Not to all of them: measured 2026-09-28 on the
    pilot box (6P+4E cores, 16 threads), 16 torch threads embedded 54% slower than 8 or 10 (hyperthreads
    and efficiency cores stall the others; docs/tuning_log.md, I2/I4). EMBED_THREADS sets the count
    instead (the ingest scripts use it to leave cores to the app)."""
    try:
        import torch
        if os.getenv("EMBED_THREADS"):
            torch.set_num_threads(int(os.environ["EMBED_THREADS"]))
            return
        n = max(1, (os.cpu_count() or 2) // 2)
        if torch.get_num_threads() < n:
            torch.set_num_threads(n)
    except Exception:  # torch missing (ollama backend) or thread count already fixed by a run
        pass


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    return Embedder()


@lru_cache(maxsize=1)
def get_reranker() -> Reranker | OnnxReranker | None:
    """RERANK_BACKEND: `torch` (default, sentence-transformers CrossEncoder) or `onnx` (int8 export)."""
    if os.getenv("RERANK_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return None
    if os.getenv("RERANK_BACKEND", "torch").lower() == "onnx":
        return OnnxReranker()
    return Reranker()
