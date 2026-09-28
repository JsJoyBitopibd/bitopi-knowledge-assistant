"""Per-request id and stage timings (Phase I1).

One request is one orchestrator.answer_stream() call. Its id is written to the rows it causes in
logs/chat.csv, calls.csv and sql.csv, so one question can be followed through all three; its timings
(seconds per stage) go to Answer.timings and chat.csv, and scripts/latency.py turns them into p50/p95.

The current request lives in a ContextVar. answer_stream() runs every step of its pipeline inside the
request's own Context (run_steps), so the caller's context is never changed, and work handed to a thread
pool goes through submit(), which carries the Context along. Outside a request (hit_rate.py, tests) every
function here is a no-op.

Timing keys: a stage name ("retrieve", "data", "answer" ...) is the wall-clock time spent in it, summed
when it runs twice; "stage.part" is a part of a stage ("retrieve.rerank", "data.db"); "llm.<purpose>" is
the time in model calls for that purpose, "llm.<purpose>.first_token" the first call's wait for its
first token; "first_token" is when the user saw the first answer token, "total" the whole request.
Stages that run in parallel (documents and database in the "both" route; keyword and vector search)
overlap, so the stages add up to more than "total".
"""
from __future__ import annotations

import contextvars
import threading
import time
import uuid
from concurrent.futures import Executor, Future
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional, TypeVar

T = TypeVar("T")


class Trace:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.t0 = time.perf_counter()
        self.timings: dict[str, float] = {}
        self._lock = threading.Lock()

    def elapsed(self) -> float:
        return time.perf_counter() - self.t0

    def add(self, key: str, seconds: float) -> None:
        with self._lock:
            self.timings[key] = self.timings.get(key, 0.0) + seconds

    def first(self, key: str, seconds: float) -> None:
        """Record `seconds` under `key` unless an earlier value is there (first token, first call)."""
        with self._lock:
            self.timings.setdefault(key, seconds)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            out = {k: round(v, 3) for k, v in self.timings.items()}
        out["total"] = round(self.elapsed(), 3)
        return out


_current: contextvars.ContextVar[Optional[Trace]] = contextvars.ContextVar("ragbot_trace", default=None)


def current() -> Optional[Trace]:
    return _current.get()


def request_id() -> str:
    t = _current.get()
    return t.id if t else ""


def add(key: str, seconds: float) -> None:
    t = _current.get()
    if t is not None:
        t.add(key, seconds)


def first(key: str, seconds: Optional[float] = None) -> None:
    """Record once: `seconds`, or the time since the request started."""
    t = _current.get()
    if t is not None:
        t.first(key, t.elapsed() if seconds is None else seconds)


@contextmanager
def span(key: str) -> Iterator[None]:
    """Add the time spent in the body to `key`."""
    t = _current.get()
    if t is None:
        yield
        return
    t0 = time.perf_counter()
    try:
        yield
    finally:
        t.add(key, time.perf_counter() - t0)


def submit(pool: Executor, fn: Callable[..., T], *args: Any, **kwargs: Any) -> Future:
    """pool.submit(), with the current request (if any) visible in the worker thread."""
    return pool.submit(contextvars.copy_context().run, fn, *args, **kwargs)


def start() -> tuple[Trace, contextvars.Context]:
    """A new request and the Context that holds it, for run_steps()."""
    trace, ctx = Trace(), contextvars.copy_context()
    ctx.run(_current.set, trace)
    return trace, ctx


def run_steps(ctx: contextvars.Context, gen: Iterator[T]) -> Iterator[T]:
    """Iterate `gen`, running each of its steps inside `ctx` (a generator runs in whatever context calls
    next() on it; this keeps the request's trace out of the caller's context)."""
    while True:
        try:
            item = ctx.run(next, gen)
        except StopIteration:
            return
        yield item
