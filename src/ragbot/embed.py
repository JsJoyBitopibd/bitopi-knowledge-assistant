"""Local embeddings (bge-m3) and reranker (bge-reranker-v2-m3). Never leave the network.

The embedding model name and backend (backend_label()) are written into the index metadata; store.py
refuses to open an index built with a different model or backend (non-negotiable #6, docs/PLAN.md M7).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from .cpu import disable_power_throttling

import numpy as np

from . import config  # noqa: F401  — loads .env before the settings below are read; importing this module
                      # directly (a script, a test) used to ignore .env, e.g. RERANK_BACKEND

EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-m3")
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
EMBED_BACKEND = os.getenv("EMBED_BACKEND", "sentence_transformers")
# Cross-encoder cost grows with the square of the sequence length, so 512 instead of the model's
# 1024 roughly quarters the CPU time. Chunks are ~600 tokens, so a 512-token window covers the
# question plus most of the chunk; raise it only if scripts/hit_rate.py regresses.
RERANK_MAX_LENGTH = int(os.getenv("RERANK_MAX_LENGTH", "384"))   # I2: 384 cut rerank time, hit rate unchanged

ST_FP32 = "st-fp32"     # the backend of every index, schema vector file and cache row from before embed_backend


def _backend_kind(backend: str | None = None) -> str:
    """Which embedder EMBED_BACKEND (or `backend`) selects: "onnx", "ollama" or "st" (sentence-transformers,
    also for any other value — the Embedder has always fallen back to it). Compared exactly, as the Embedder
    always has (`== "ollama"`): "Ollama" or "ONNX" stays sentence-transformers, so the label recorded with an
    index is what really embedded it, and an existing .env cannot change backend by upgrading."""
    b = EMBED_BACKEND if backend is None else backend
    return b if b in ("onnx", "ollama") else "st"


def backend_label(backend: str | None = None) -> str:
    """The embed_backend recorded next to embed_model (docs/PLAN.md M7): "st-fp32" (sentence-transformers,
    the default), "onnx-int8" (EMBED_BACKEND=onnx) or "ollama". Vectors from two backends differ slightly,
    so the vector store, the schema vector files and the embedding cache each belong to one backend."""
    return {"onnx": "onnx-int8", "ollama": "ollama"}.get(_backend_kind(backend), ST_FP32)


def cache_model(model: str, backend: str) -> str:
    """The embedding cache's `model` key for (model, backend). st-fp32 keeps the plain model name, so every
    row cached before embed_backend existed (all st-fp32) still hits — no migration; any other backend gets
    rows of its own ("BAAI/bge-m3|onnx-int8")."""
    return model if backend == ST_FP32 else f"{model}|{backend}"


class Embedder:
    """embed(texts) -> list of unit-length float vectors (1,024-d for bge-m3). sentence-transformers or
    ollama; `backend` overrides EMBED_BACKEND (the parity check needs fp32 whatever .env says)."""

    def __init__(self, backend: str | None = None) -> None:
        self.model_name = EMBED_MODEL
        kind = _backend_kind(backend)
        if kind == "onnx":
            raise RuntimeError("EMBED_BACKEND=onnx is OnnxEmbedder (get_embedder() picks it), not Embedder")
        if kind == "ollama":
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


def onnx_embedder_dir(precision: str = "int8") -> Path:
    """Where scripts/export_embedder_onnx.py writes the ONNX embedder: data/models/<name>-int8 (used by
    EMBED_BACKEND=onnx) and <name>-fp32 (the unquantized export, kept only for its parity check)."""
    from .config import ROOT
    return ROOT / "data" / "models" / f"{EMBED_MODEL.split('/')[-1]}-{precision}"


ONNX_EMBEDDER_CONFIG = "embedder.json"    # written by the export: model, max_length, pooling


def cls_pool(hidden: np.ndarray) -> np.ndarray:
    """bge-m3's sentence vector, as sentence-transformers computes it: the first ([CLS]) token's last hidden
    state, L2-normalised. hidden: (texts, tokens, dims) -> (texts, dims) float32."""
    v = np.asarray(hidden[:, 0, :], dtype=np.float32)
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)


def _embed_threads() -> int:
    """EMBED_THREADS, else about one per physical core (the same default as torch, _use_all_cpu_threads)."""
    if os.getenv("EMBED_THREADS"):
        return max(1, int(os.environ["EMBED_THREADS"]))
    return max(1, (os.cpu_count() or 2) // 2)


class OnnxEmbedder:
    """bge-m3 as ONNX (scripts/export_embedder_onnx.py; int8 by default), run by onnxruntime: tokenized and
    truncated like sentence-transformers (max length recorded by the export from its max_seq_length), CLS
    pooling, L2-normalised. Selected by EMBED_BACKEND=onnx only, and recorded as onnx-int8: store.py refuses
    an st-fp32 index under it, so its vectors are never mixed with fp32 ones (docs/PLAN.md M7)."""

    def __init__(self, model_dir: Path | str | None = None) -> None:
        import json
        import onnxruntime as ort
        from transformers import AutoTokenizer
        d = Path(model_dir) if model_dir else onnx_embedder_dir("int8")
        if not (d / "model.onnx").exists() or not (d / ONNX_EMBEDDER_CONFIG).exists():
            raise RuntimeError(f"{d / 'model.onnx'} not found — run python scripts/export_embedder_onnx.py")
        cfg = json.loads((d / ONNX_EMBEDDER_CONFIG).read_text(encoding="utf-8"))
        if cfg.get("model") != EMBED_MODEL:      # non-negotiable #6: never embed with another model silently
            raise RuntimeError(f"{d} holds {cfg.get('model')!r} but EMBED_MODEL is {EMBED_MODEL!r}")
        self.model_name = EMBED_MODEL
        self.max_length = int(cfg["max_length"])
        self._tok = AutoTokenizer.from_pretrained(str(d))
        so = ort.SessionOptions()
        so.intra_op_num_threads = _embed_threads()
        so.inter_op_num_threads = 1
        so.add_session_config_entry("session.intra_op.allow_spinning", "0")   # see OnnxReranker
        self._sess = ort.InferenceSession(str(d / "model.onnx"), sess_options=so, providers=["CPUExecutionProvider"])

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        out = []
        bs = _batch_size("EMBED_BATCH_SIZE")
        for i in range(0, len(texts), bs):
            enc = self._tok(texts[i:i + bs], padding=True, truncation=True, max_length=self.max_length,
                            return_tensors="np")
            hidden = self._sess.run(["last_hidden_state"], {"input_ids": enc["input_ids"].astype(np.int64),
                                                            "attention_mask": enc["attention_mask"].astype(np.int64)})[0]
            out.append(cls_pool(hidden))
        return np.concatenate(out).tolist()

    def token_counts(self, texts: list[str]) -> list[int]:
        """Tokens per text before truncation, special tokens included (the parity check's length split)."""
        return [len(ids) for ids in self._tok(texts, truncation=False)["input_ids"]]


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
def get_embedder() -> Embedder | OnnxEmbedder:
    """EMBED_BACKEND: `sentence_transformers` (default), `ollama`, or `onnx` (the int8 export; an index built
    with another backend refuses to open under it, store.check_index_metadata)."""
    disable_power_throttling()           # Windows ran the models 6-9x slower in background processes
    if _backend_kind() == "onnx":
        return OnnxEmbedder()
    return Embedder()


@lru_cache(maxsize=1)
def get_reranker() -> Reranker | OnnxReranker | None:
    """RERANK_BACKEND: `torch` (default, sentence-transformers CrossEncoder) or `onnx` (int8 export)."""
    if os.getenv("RERANK_ENABLED", "true").lower() not in {"1", "true", "yes"}:
        return None
    disable_power_throttling()
    if os.getenv("RERANK_BACKEND", "torch").lower() == "onnx":
        return OnnxReranker()
    return Reranker()
