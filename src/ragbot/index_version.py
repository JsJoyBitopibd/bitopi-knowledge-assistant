"""Detect when a background ingest has updated the index, and drop the in-memory caches that would
otherwise hide the change until the app restarts.

The chat process holds the BM25 index and the Chroma client in module-level lru_caches. An ingest run
in a separate process (scripts/ingest.py or scripts/ingest_worker.py) writes new files on disk but the
running app never re-reads them. `ingest_folder()` rewrites data/index/ingest_state.json at the end of
every run, so its mtime is a cheap version stamp: when it changes, clear the caches so the next question
opens a fresh Chroma client and reloads the keyword index.
"""
from __future__ import annotations

from .config import settings

_seen: int = -1


def _state_path():
    return settings().path("index_dir") / "ingest_state.json"


def index_version() -> int:
    """mtime_ns of ingest_state.json, or 0 if no ingest has run yet."""
    try:
        return _state_path().stat().st_mtime_ns
    except OSError:
        return 0


def refresh_if_changed() -> bool:
    """Clear the retrieval caches if the index changed since the last check. Returns True on a refresh.
    Cheap enough (one stat) to call before every question."""
    global _seen
    v = index_version()
    if v == _seen:
        return False
    first = _seen == -1
    _seen = v
    if first:
        return False  # first call only records the baseline; nothing to clear yet
    from .retrieve.keyword import get_keyword_index
    from .store import get_store
    get_keyword_index.cache_clear()
    get_store.cache_clear()
    return True
