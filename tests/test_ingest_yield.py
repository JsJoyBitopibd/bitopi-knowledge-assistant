"""I4: the ingest scripts yield the CPU to the chat app: a search marks the app busy, and the worker's
embedding waits (in small slices, never more than a cap) while it is."""
import os
import time

import pytest

from _ingest_fakes import make_index
from ragbot.ingest import pipeline, priority


@pytest.fixture
def busy_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(priority, "_yield_enabled", True)
    return tmp_path


def test_a_search_marks_the_app_busy_for_a_while(busy_dir):
    assert not priority.app_is_busy(busy_dir)                 # no file yet
    priority.mark_app_busy(busy_dir)
    assert priority.app_is_busy(busy_dir, window=5)
    old = time.time() - 10
    os.utime(busy_dir / priority.BUSY_FILE, (old, old))
    assert not priority.app_is_busy(busy_dir, window=5)       # the search is long over


def test_the_worker_waits_while_the_app_is_busy_but_not_forever(busy_dir):
    priority.mark_app_busy(busy_dir)
    waited = priority.yield_to_app(busy_dir, window=0.4, max_wait=5, step=0.05)
    assert 0.3 < waited < 1.5                                 # until the mark is `window` old
    priority.mark_app_busy(busy_dir)
    waited = priority.yield_to_app(busy_dir, window=60, max_wait=0.3, step=0.05)
    assert waited < 1.0                                       # a steady stream of questions cannot stop it


def test_nothing_waits_unless_an_ingest_script_turned_it_on(tmp_path, monkeypatch):
    monkeypatch.setattr(priority, "_yield_enabled", False)
    priority.mark_app_busy(tmp_path)
    assert priority.yield_to_app(tmp_path, window=60) == 0.0


def test_a_read_only_index_never_breaks_a_search(tmp_path):
    priority.mark_app_busy(tmp_path / "missing" / "dir")      # parent does not exist: ignored


def test_embedding_checks_the_app_between_small_slices(tmp_path, monkeypatch):
    root, reg, store, run, files = make_index(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(pipeline, "yield_to_app", lambda: calls.append(1) or 0.0)
    monkeypatch.setattr(pipeline.settings(), "_d", {**pipeline.settings()._d,
                                                    "ingest": {**pipeline.settings()["ingest"], "yield_slice": 1}})
    files("alpha")                                            # 2 pages -> 2 chunks
    r = run()
    assert r["added"] == 1 and r["embedded"] == 2 and len(calls) == 2   # one check per slice of one chunk
    calls.clear()
    files("alpha")                                            # same content again: nothing to embed, no checks
    run(redo={"alpha.pdf"})
    assert calls == []


def test_run_in_background_sets_the_threads_and_the_pause(monkeypatch):
    monkeypatch.delenv("EMBED_THREADS", raising=False)
    monkeypatch.setattr(priority, "lower_priority", lambda level=None: "below_normal")
    monkeypatch.setattr(priority, "_yield_enabled", False)
    desc = priority.run_in_background()
    assert os.environ["EMBED_THREADS"] == "4" and priority._yield_enabled
    assert "below_normal" in desc and "4 embedding threads" in desc
    monkeypatch.setenv("EMBED_THREADS", "2")                  # an explicit value wins
    priority.run_in_background()
    assert os.environ["EMBED_THREADS"] == "2"
