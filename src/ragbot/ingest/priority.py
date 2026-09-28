"""Ingestion yields the CPU to the chat app (Phase I4).

The worker and the app run on the same machine (docker-compose, or one Windows server) and both are
CPU-bound: the worker embeds chunks, the app embeds the question and reranks. Measured 2026-09-28
(docs/tuning_log.md): while the worker ingested, the app's document search took 9.6 s p50 instead of
4.2 s. A lower priority alone barely helps (the two still share memory bandwidth, caches and the turbo
budget); fewer worker threads help more; what works is the worker pausing while the app is searching.

run_in_background() is for the ingest scripts: it lowers the process priority (ingest.priority), caps the
embedder's threads (ingest.embed_threads) and turns on the pause (ingest.yield_to_app). The app marks a
search by touching data/index/app_busy (mark_app_busy, called by retrieve()); before each small slice of
embedding the worker waits until that file is BUSY_SECONDS old, but never more than MAX_WAIT_SECONDS in a
row, so a steady stream of questions slows ingestion down without stopping it. The file works across
the docker-compose containers, which share data/index.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

from ..config import settings

log = logging.getLogger("ingest")

_WINDOWS = {"normal": 0x0020, "below_normal": 0x4000, "idle": 0x0040}   # priority classes
_NICE = {"normal": 0, "below_normal": 10, "idle": 19}
BUSY_FILE = "app_busy"
BUSY_SECONDS = 8.0        # a search's CPU-heavy part (embed + rerank) ends within this after it starts
MAX_WAIT_SECONDS = 60.0
_yield_enabled = False


def lower_priority(level: Optional[str] = None) -> str:
    """Set this process's CPU priority to `level` (default: settings ingest.priority, below_normal).
    Returns the level set; never raises (an unknown level or a refused call leaves the priority alone)."""
    level = (level or settings().get("ingest.priority", "below_normal") or "normal").lower()
    if level not in _NICE:
        log.warning("ingest.priority %r is not one of %s; left unchanged", level, ", ".join(_NICE))
        return "normal"
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            # declared types: the current-process pseudo-handle is -1, which ctypes would otherwise pass
            # as a 32-bit int that Windows rejects (the call then fails and nothing changes)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            k32.SetPriorityClass.restype = wintypes.BOOL
            if not k32.SetPriorityClass(k32.GetCurrentProcess(), _WINDOWS[level]):
                raise OSError(ctypes.get_last_error(), "SetPriorityClass failed")
        elif _NICE[level]:
            os.nice(_NICE[level] - os.nice(0))      # os.nice adds; aim at the level from wherever we are
    except (OSError, AttributeError) as e:
        log.warning("could not set the ingest priority to %s: %s", level, e)
        return "normal"
    return level


def run_in_background() -> str:
    """For scripts/ingest_worker.py, ingest.py and reindex.py, before the embedding model loads. Returns a
    one-line description for the log."""
    global _yield_enabled
    s = settings()
    level = lower_priority()
    threads = s.get("ingest.embed_threads", 4)
    if threads and not os.getenv("EMBED_THREADS"):          # an explicit EMBED_THREADS wins
        os.environ["EMBED_THREADS"] = str(int(threads))
    _yield_enabled = bool(s.get("ingest.yield_to_app", True))
    return (f"priority {level}, {os.getenv('EMBED_THREADS') or 'all'} embedding threads, "
            f"{'pauses while the app searches' if _yield_enabled else 'no pause for the app'}")


def _busy_path(index_dir: Optional[Path]) -> Path:
    return (index_dir or settings().path("index_dir")) / BUSY_FILE


def mark_app_busy(index_dir: Optional[Path] = None) -> None:
    """The app is about to search (embed the question, rerank): the worker pauses for BUSY_SECONDS."""
    p = _busy_path(index_dir)
    try:
        os.utime(p)
    except FileNotFoundError:
        try:
            p.touch()
        except OSError:
            pass
    except OSError:
        pass                                   # a read-only index: the app never fails over this


def app_is_busy(index_dir: Optional[Path] = None, window: float = BUSY_SECONDS) -> bool:
    try:
        return time.time() - _busy_path(index_dir).stat().st_mtime < window
    except OSError:
        return False


def yield_to_app(index_dir: Optional[Path] = None, window: float = BUSY_SECONDS,
                 max_wait: float = MAX_WAIT_SECONDS, step: float = 0.2) -> float:
    """Worker side, between slices of work: wait while the app is searching (only after
    run_in_background()). Returns the seconds waited."""
    if not _yield_enabled:
        return 0.0
    t0 = time.monotonic()
    while app_is_busy(index_dir, window) and time.monotonic() - t0 < max_wait:
        time.sleep(step)
    return time.monotonic() - t0
