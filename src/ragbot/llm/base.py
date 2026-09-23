"""The ONLY interface the rest of the code uses to talk to a chat model.

Non-negotiable #5: no vendor SDK import outside src/ragbot/llm/. Every call is logged to
logs/calls.csv (data-minimisation audit, FR-8.4): who/what was sent (ids and counts, not text).
"""
from __future__ import annotations

import csv
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel

from ..config import log_dir


class ChatReply(BaseModel):
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    stop_reason: str = ""
    raw: Optional[Any] = None


class ChatModel(ABC):
    """messages: [{"role": "user"|"assistant", "content": str}], system: str."""

    name: str = "base"

    @abstractmethod
    def _chat(self, messages: list[dict], system: str, max_tokens: int, temperature: float) -> ChatReply: ...

    def chat(self, messages: list[dict], system: str = "", max_tokens: int = 700, temperature: float = 0.0,
             *, purpose: str = "answer", sent_chunk_ids: list[str] | None = None, sent_rows: int = 0,
             user: str = "") -> ChatReply:
        t0 = time.perf_counter()
        reply = self._chat(messages, system, max_tokens, temperature)
        _log_call(self.name, reply, time.perf_counter() - t0, purpose, sent_chunk_ids or [], sent_rows, user)
        return reply


def _log_call(provider: str, r: ChatReply, seconds: float, purpose: str, chunk_ids: list[str], rows: int, user: str) -> None:
    f = log_dir() / "calls.csv"
    new = not f.exists()
    with open(f, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts", "user", "purpose", "provider", "model", "input_tokens", "output_tokens",
                        "seconds", "stop_reason", "chunks_sent", "rows_sent", "chunk_ids"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), user, purpose, provider, r.model,
                    r.input_tokens, r.output_tokens, f"{seconds:.2f}", r.stop_reason,
                    len(chunk_ids), rows, ";".join(chunk_ids)])
