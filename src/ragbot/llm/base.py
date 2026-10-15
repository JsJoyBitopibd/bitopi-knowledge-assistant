"""The ONLY interface the rest of the code uses to talk to a chat model.

Non-negotiable #5: no vendor SDK import outside src/ragbot/llm/. Every call is logged to
logs/calls.csv (data-minimisation audit, FR-8.4): who/what was sent (ids and counts, not text).
"""
from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Iterator, Optional, Union

from pydantic import BaseModel

from .. import trace


class LLMError(RuntimeError):
    """A provider call that failed for a reason the UI should explain, not a raw traceback.

    `kind` is one of quota | rate | timeout | auth | other. `quota` is the day's allowance spent (try again
    after the provider's midnight); `rate` is a per-minute limit (try again in a minute) — both arrive as
    an HTTP 429, and `rate_or_quota()` tells them apart. Adapters map their vendor SDK's exceptions to a
    kind via their own `_classify()` so that no vendor exception type leaks past src/ragbot/llm/
    (non-negotiable #5)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# A provider error is cut to this many characters before it reaches LLMError (and the warnings and logs).
MESSAGE_CUT = 300
# A Gemini 429 names the limits it hit, e.g. quotaId "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
# or "GenerateRequestsPerDayPerProjectPerModel-FreeTier". The window in the id is what decides the kind.
_QUOTA_ID = re.compile(r"\b[A-Za-z][A-Za-z0-9]*Per(?:Minute|Day)[A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")


def quota_ids(text: str) -> list[str]:
    """The provider's quota ids named in an error text, in the order given, each once."""
    found: list[str] = []
    for m in _QUOTA_ID.finditer(text or ""):
        if m.group(0) not in found:
            found.append(m.group(0))
    return found


def rate_or_quota(text: str, calls: int, budget: int) -> str:
    """The kind of a 429: "quota" when the day's allowance is spent, "rate" when a per-minute limit was hit.

    Every 429 used to read as "quota", so a per-minute limit told staff to come back after midnight Pacific
    time. The error's own quota ids decide first: any per-day id is the daily quota, even when a per-minute
    id is listed beside it; per-minute ids alone are a rate limit. A 429 that names no id falls back on the
    count of today's calls (ragbot/quota.py): under the budget it is taken for a rate limit. OpenAI and Azure
    say `insufficient_quota` when the account's money or allowance is spent: that is never a minute's wait."""
    ids = quota_ids(text)
    if any("PerDay" in i for i in ids) or "insufficient_quota" in (text or ""):
        return "quota"
    if ids:
        return "rate"
    return "rate" if calls < budget else "quota"


def error_text(exc: BaseException) -> str:
    """The exception's text plus the response body an SDK keeps beside it (where a 429 lists its quota ids)."""
    body = getattr(exc, "body", None)
    return f"{exc} {body}" if body else str(exc)


def error_message(exc: BaseException) -> str:
    """What LLMError carries: the first MESSAGE_CUT characters, plus the quota ids found anywhere in the
    error — a Gemini 429 is longer than the cut, and the log must still say which limit was hit."""
    ids = quota_ids(error_text(exc))
    return str(exc)[:MESSAGE_CUT] + (f" [quota: {', '.join(ids)}]" if ids else "")


class ChatReply(BaseModel):
    text: str = ""
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

    def _classify(self, exc: Exception) -> str:
        """Map a vendor SDK exception to an LLMError kind. Overridden per adapter; the base default
        treats everything as 'other'."""
        return "other"

    def chat(self, messages: list[dict], system: str = "", max_tokens: int = 700, temperature: float = 0.0,
             *, purpose: str = "answer", sent_chunk_ids: list[str] | None = None, sent_rows: int = 0,
             user: str = "") -> ChatReply:
        t0 = time.perf_counter()
        try:
            reply = self._chat(messages, system, max_tokens, temperature)
        except LLMError:
            raise
        except Exception as e:
            kind = self._classify(e)
            _log_call(self.name, ChatReply(stop_reason=f"error:{kind}"), time.perf_counter() - t0,
                      purpose, sent_chunk_ids or [], sent_rows, user)
            raise LLMError(kind, error_message(e)) from e
        finally:
            trace.add(f"llm.{purpose}", time.perf_counter() - t0)
        _log_call(self.name, reply, time.perf_counter() - t0, purpose, sent_chunk_ids or [], sent_rows, user)
        return reply

    def _stream(self, messages: list[dict], system: str, max_tokens: int,
                temperature: float) -> Iterator[Union[str, ChatReply]]:
        """Yield text deltas, then one final ChatReply (full text + usage). Adapters that cannot stream
        inherit this default: the whole reply arrives as a single delta."""
        reply = self._chat(messages, system, max_tokens, temperature)
        if reply.text:
            yield reply.text
        yield reply

    def stream(self, messages: list[dict], system: str = "", max_tokens: int = 700, temperature: float = 0.0,
               *, purpose: str = "answer", sent_chunk_ids: list[str] | None = None, sent_rows: int = 0,
               user: str = "") -> Iterator[Union[str, ChatReply]]:
        """Like chat(), but yields text deltas as they arrive; the LAST item is the complete ChatReply.
        One calls.csv row is written when the stream ends (or fails), with the same columns as chat()."""
        t0 = time.perf_counter()
        parts: list[str] = []
        try:
            for item in self._stream(messages, system, max_tokens, temperature):
                if isinstance(item, ChatReply):
                    if not item.text:
                        item.text = "".join(parts)
                    trace.add(f"llm.{purpose}", time.perf_counter() - t0)
                    _log_call(self.name, item, time.perf_counter() - t0, purpose, sent_chunk_ids or [],
                              sent_rows, user)
                    yield item
                    return
                if not parts:
                    trace.first(f"llm.{purpose}.first_token", time.perf_counter() - t0)
                parts.append(item)
                yield item
        except LLMError:
            raise
        except Exception as e:
            kind = self._classify(e)
            trace.add(f"llm.{purpose}", time.perf_counter() - t0)
            _log_call(self.name, ChatReply(stop_reason=f"error:{kind}"), time.perf_counter() - t0,
                      purpose, sent_chunk_ids or [], sent_rows, user)
            raise LLMError(kind, error_message(e)) from e
        # The adapter ended without a final ChatReply: synthesize one so callers can rely on it.
        reply = ChatReply(text="".join(parts), stop_reason="stop")
        trace.add(f"llm.{purpose}", time.perf_counter() - t0)
        _log_call(self.name, reply, time.perf_counter() - t0, purpose, sent_chunk_ids or [], sent_rows, user)
        yield reply


CALLS_HEADER = ["ts", "user", "purpose", "provider", "model", "input_tokens", "output_tokens", "seconds",
                "stop_reason", "chunks_sent", "rows_sent", "chunk_ids", "request_id"]


def _log_call(provider: str, r: ChatReply, seconds: float, purpose: str, chunk_ids: list[str], rows: int, user: str) -> None:
    """One row per model call in logs/calls.csv, with the request that made it (ragbot/trace.py)."""
    from ..logs import append_row
    append_row("calls.csv", CALLS_HEADER, [datetime.now().isoformat(timespec="seconds"), user, purpose, provider,
                                           r.model, r.input_tokens, r.output_tokens, f"{seconds:.2f}", r.stop_reason,
                                           len(chunk_ids), rows, ";".join(chunk_ids), trace.request_id()])
