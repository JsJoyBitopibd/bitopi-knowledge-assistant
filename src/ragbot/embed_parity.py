"""The pure parts of the embedder parity check (scripts/export_embedder_onnx.py --check, docs/PLAN.md M7):
which texts to compare, and cosine statistics between two embeddings of the same texts — overall, by
length (a 512-token boundary) and by kind of text. No model is loaded here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

LONG_TOKENS = 512          # texts longer than this are reported apart
GATE_COSINE = 0.99         # docs/PLAN.md M7: an int8 vector must be this close to its fp32 one
SAME_COSINE = 0.9999       # two fp32 computations of one text (sentence-transformers, fp32 ONNX, stored)
KIND_LABELS = {"chunk": "chunks", "question": "questions", "schema": "table documents"}


@dataclass
class ParityText:
    kind: str                       # "chunk" | "question" | "schema"
    key: str                        # chunk id, "q<n>", "<database>:<table>"
    text: str                       # exactly what was (or would be) embedded
    ref: Optional[np.ndarray]       # the stored fp32 vector; None for a question (nothing is stored)


def rowwise_cosine(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine of each row of `a` with the same row of `b` (both normalised here, so scale does not matter)."""
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"shapes differ: {a.shape} vs {b.shape}")
    na = np.maximum(np.linalg.norm(a, axis=1), 1e-12)
    nb = np.maximum(np.linalg.norm(b, axis=1), 1e-12)
    return np.einsum("ij,ij->i", a, b) / (na * nb)


def cosine_stats(cos: Sequence[float]) -> dict[str, float]:
    """n, min, p1 (1st percentile) and mean; NaN statistics for no texts."""
    c = np.asarray(cos, dtype=np.float64)
    if not c.size:
        return {"n": 0, "min": float("nan"), "p1": float("nan"), "mean": float("nan")}
    return {"n": int(c.size), "min": float(c.min()), "p1": float(np.percentile(c, 1)), "mean": float(c.mean())}


def stats_line(label: str, cos: Sequence[float]) -> str:
    s = cosine_stats(cos)
    return f"{label:<24} n={s['n']:>5}  min {s['min']:.5f}  p1 {s['p1']:.5f}  mean {s['mean']:.5f}"


def parity_lines(cos: Sequence[float], tokens: Sequence[int], kinds: Sequence[str],
                 long_tokens: int = LONG_TOKENS) -> list[str]:
    """The report: all texts, then <= long_tokens vs > long_tokens tokens, then each kind of text."""
    c, t, k = np.asarray(cos, dtype=np.float64), np.asarray(tokens), np.asarray(kinds)
    lines = [stats_line("all texts", c),
             stats_line(f"<= {long_tokens} tokens", c[t <= long_tokens]),
             stats_line(f"> {long_tokens} tokens", c[t > long_tokens])]
    for kind in dict.fromkeys(kinds):
        lines.append(stats_line(KIND_LABELS.get(kind, kind), c[k == kind]))
    return lines


def even_sample(n: int, k: int) -> list[int]:
    """k indexes spread evenly over range(n), in order, deterministic (all of them when k >= n)."""
    if k >= n:
        return list(range(n))
    return [i * n // k for i in range(max(0, k))]


def plan_selection(n_chunks: int, n_questions: int, n_schema: int, limit: int) -> tuple[int, list[int]]:
    """Every chunk is compared (the int8/int8 hit rate needs all of them embedded anyway); then the questions,
    then table documents spread over all databases, until `limit` texts in all. Returns (how many questions,
    which table documents)."""
    room = max(0, limit - n_chunks)
    q = min(n_questions, room)
    return q, even_sample(n_schema, min(n_schema, room - q))
