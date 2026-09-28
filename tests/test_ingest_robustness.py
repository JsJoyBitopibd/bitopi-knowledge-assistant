"""Phase H: same-name PDFs (H1), the stores kept in step (H2), retries, crash resume, the index lock and the
state files (H3)."""
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time

import pytest

from _ingest_fakes import make_index, make_pdf
from ragbot.ingest import pipeline
from ragbot.ingest.check import check_index
from ragbot.ingest.lock import IndexLocked, index_lock
from ragbot.store import Registry


@pytest.fixture
def index(tmp_path, monkeypatch):
    return make_index(tmp_path, monkeypatch)


def clean(reg, store):
    return {k: v for k, v in check_index(reg, store).items() if v} == {}


def texts(store, source):
    return sorted(c.text for c in store.rows.values() if c.source == source)


# ---- H1: two PDFs with the same file name
def test_a_same_name_file_in_another_folder_is_skipped_and_reported(index, tmp_path):
    root, reg, store, run, files = index
    files("manual", folder="SOP")
    run()
    before = texts(store, "manual.pdf")
    make_pdf(root / "TAL" / "manual.pdf", "tal copy")          # same name, other folder, other content
    for _ in range(2):                                         # stable: never flips to the other copy
        r = run()
        assert r["duplicates"] == ["TAL/manual.pdf"] and r["failed"] == 1 and r["added"] == r["updated"] == 0
        assert texts(store, "manual.pdf") == before and reg.indexed_path("manual.pdf") == "SOP/manual.pdf"
    scan = json.loads((tmp_path / "index" / "last_scan.json").read_text(encoding="utf-8"))
    assert scan["duplicates"] == ["TAL/manual.pdf"]
    (root / "SOP" / "manual.pdf").unlink()                     # the indexed copy goes: the other one takes over
    r = run()
    assert "duplicates" not in r and r["updated"] == 1 and r["pending_removal"] == 0
    assert reg.indexed_path("manual.pdf") == "TAL/manual.pdf"
    assert {(c.category, c.factory) for c in store.rows.values()} == {("TAL", "TAL")}


def test_two_new_same_name_files_index_the_first_by_path(index):
    root, reg, store, run, files = index
    files("x", folder="B")
    files("x", folder="A")
    r = run()
    assert r["added"] == 1 and r["duplicates"] == ["B/x.pdf"] and reg.indexed_path("x.pdf") == "A/x.pdf"


# ---- H2: a failed update leaves the old version whole; --check finds a store left behind
class Boom:
    def embed(self, texts):
        raise RuntimeError("model hub unreachable")


def test_a_failed_update_keeps_the_old_version_in_every_store(index, monkeypatch):
    root, reg, store, run, files = index
    files("alpha")
    run()
    old_hash, old = reg.known_hash("alpha.pdf"), texts(store, "alpha.pdf")
    make_pdf(root / "SOP" / "alpha.pdf", "revised")
    monkeypatch.setattr(pipeline, "get_embedder", lambda: Boom())
    r = run()
    assert r["failed"] == 1 and r["updated"] == 0
    assert texts(store, "alpha.pdf") == old and reg.known_hash("alpha.pdf") == old_hash and clean(reg, store)
    from _ingest_fakes import FakeEmbedder
    monkeypatch.setattr(pipeline, "get_embedder", lambda: FakeEmbedder())
    r = run()                                                  # retried (the file did not change)
    assert r["updated"] == 1 and texts(store, "alpha.pdf") != old and clean(reg, store)
    assert reg.db.execute("SELECT status, attempts, failed_hash FROM document").fetchone() == ("ok", 0, None)


def test_check_finds_what_each_store_is_missing_and_redo_repairs_it(index):
    root, reg, store, run, files = index
    files("alpha", "beta")
    run()
    assert clean(reg, store)
    a = sorted(k for k, c in store.rows.items() if c.source == "alpha.pdf")
    b = sorted(k for k, c in store.rows.items() if c.source == "beta.pdf")
    store.rows["ghost#p1#c1"] = store.rows[a[0]].model_copy(update={"id": "ghost#p1#c1", "source": "ghost.pdf"})
    del store.rows[a[1]]                                       # a registry chunk with no vector
    reg.db.execute("INSERT INTO chunk_fts(id, source, category, superseded, body) "
                   "VALUES('alpha#p9#c9', 'alpha.pdf', 'SOP', 0, 'x')")   # a keyword row with no chunk
    reg.db.execute("UPDATE chunk_fts SET superseded=1 WHERE id=?", (b[0],))
    reg.db.commit()
    store.rows[b[1]].factory = "TAL"                           # not what the document's folder says
    found = check_index(reg, store)
    assert found["vector store chunks missing from the registry"] == ["ghost#p1#c1"]
    assert found["registry chunks missing from the vector store"] == [a[1]]
    assert found["keyword index chunks missing from the registry"] == ["alpha#p9#c9"]
    assert found["superseded flag differs from the document's"] == [b[0]]
    assert found["category or access tags differ from the document's"] == [b[1]]
    r = run(redo={"alpha.pdf", "beta.pdf"})                    # scripts/ingest.py --redo alpha.pdf beta.pdf
    assert r["updated"] == 2 and r["embedded"] == 0            # rewritten from the embedding cache
    left = {k: v for k, v in check_index(reg, store).items() if v}
    assert left == {"vector store chunks missing from the registry": ["ghost#p1#c1"]}   # unknown: reindex.py


# ---- H3: retries, crash resume, state files, lock
def test_a_failing_file_is_tried_three_times_then_left_until_it_changes(index):
    root, reg, store, run, files = index
    broken = root / "SOP" / "broken.pdf"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a pdf at all")
    results = [run() for _ in range(4)]
    assert [r["failed"] for r in results] == [1, 1, 1, 0] and [r["retry"] for r in results] == [1, 1, 0, 0]
    assert results[-1]["failed_total"] == 1
    assert reg.db.execute("SELECT attempts FROM document WHERE source='broken.pdf'").fetchone()[0] == 3
    make_pdf(broken, "fixed")                                  # a new version gets its own three attempts
    r = run()
    assert r["added"] == 1 and r["failed_total"] == 0


def test_a_run_killed_half_way_is_redone_by_the_next_one(index, monkeypatch):
    root, reg, store, run, files = index
    files("alpha")
    run()
    make_pdf(root / "SOP" / "alpha.pdf", "revised")
    files("beta")
    real = store.upsert

    def killed(*a, **k):
        raise KeyboardInterrupt                                # not an Exception: the process dies here
    monkeypatch.setattr(store, "upsert", killed)
    with pytest.raises(KeyboardInterrupt):
        run()
    assert not clean(reg, store)                               # alpha's old vectors went, its new ones never came
    monkeypatch.setattr(store, "upsert", real)
    r = run()
    assert r["updated"] == 1 and r["added"] == 1 and clean(reg, store)
    runs = [s for (s,) in reg.db.execute("SELECT status FROM ingest_run ORDER BY id")]
    assert runs == ["done", "interrupted", "done"]


def test_a_rewrite_of_an_unchanged_file_killed_half_way_is_redone(index, monkeypatch):
    root, reg, store, run, files = index
    files("alpha")
    run()
    real = store.upsert
    monkeypatch.setattr(store, "upsert", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run(redo={"alpha.pdf"})                                # the file did not change, its hash is current
    found = {k: v for k, v in check_index(reg, store).items() if v}
    assert found["documents whose last write did not finish (the next pass rewrites them)"] == ["alpha.pdf"]
    monkeypatch.setattr(store, "upsert", real)
    r = run()                                                  # a normal pass, no --redo
    assert r["updated"] == 1 and clean(reg, store)


def test_a_pass_that_changes_nothing_leaves_the_index_version_alone(index, tmp_path, monkeypatch):
    root, reg, store, run, files = index
    files("alpha")
    run()
    state, scan = tmp_path / "index" / "ingest_state.json", tmp_path / "index" / "last_scan.json"
    stamp, content = state.stat().st_mtime_ns, state.read_text(encoding="utf-8")
    monkeypatch.setattr(pipeline, "get_embedder", lambda: pytest.fail("a pass with nothing new loaded the model"))
    monkeypatch.setattr(pipeline, "sha256", lambda p: pytest.fail("an unchanged file was read again"))
    first_scan = json.loads(scan.read_text(encoding="utf-8"))["scanned_at"]
    time.sleep(1.1)
    r = run()
    assert r["added"] == r["updated"] == r["failed"] == 0
    assert state.stat().st_mtime_ns == stamp and state.read_text(encoding="utf-8") == content
    assert json.loads(scan.read_text(encoding="utf-8"))["scanned_at"] > first_scan
    assert not list((tmp_path / "index").glob("*.tmp"))


def test_an_alert_rewrites_the_state_but_keeps_the_index_time(index, tmp_path):
    root, reg, store, run, files = index
    files("alpha")
    run()
    state = tmp_path / "index" / "ingest_state.json"
    updated_at = json.loads(state.read_text(encoding="utf-8"))["index_updated_at"]
    root.rename(tmp_path / "offline")
    time.sleep(1.1)
    run()
    d = json.loads(state.read_text(encoding="utf-8"))
    assert d["alert"] and d["index_updated_at"] == updated_at


def test_registry_from_before_h3_retries_its_failed_files(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE document(source TEXT PRIMARY KEY, title TEXT, category TEXT, doc_hash TEXT, pages INTEGER,
        chunks INTEGER, tables_ INTEGER, ocr_pages INTEGER, superseded INTEGER DEFAULT 0, status TEXT, error TEXT,
        ingested_at TEXT);
      CREATE TABLE ingest_run(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
        added INTEGER, updated INTEGER, removed INTEGER, failed INTEGER);
      INSERT INTO document(source, doc_hash, status) VALUES('ok.pdf', 'h1', 'ok'), ('bad.pdf', 'h2', 'failed');""")
    con.commit(); con.close()
    reg = Registry(db)
    assert reg.known_hash("ok.pdf") == "h1" and reg.known_hash("bad.pdf") is None   # retried as a new file
    assert reg.failed_attempts("bad.pdf", "h2") == 1
    assert "status" in {r[1] for r in reg.db.execute("PRAGMA table_info(ingest_run)")}


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_the_index_lock_admits_one_writer(tmp_path):
    with index_lock(tmp_path):
        with pytest.raises(IndexLocked, match="being written by process"):
            with index_lock(tmp_path):
                pass
    assert not (tmp_path / "ingest.lock").exists()
    with index_lock(tmp_path):                                 # released: free again
        pass


def test_a_lock_left_by_a_dead_process_or_a_silent_host_is_taken_over(tmp_path):
    lock = tmp_path / "ingest.lock"
    lock.write_text(f"{socket.gethostname()} {_dead_pid()} 2026-09-28T10:00:00", encoding="utf-8")
    with index_lock(tmp_path):                                 # same host, process gone
        pass
    lock.write_text("worker-container 7 2026-09-28T10:00:00", encoding="utf-8")
    with pytest.raises(IndexLocked):                           # another host, refreshed recently: respected
        with index_lock(tmp_path, stale_s=60):
            pass
    old = time.time() - 120
    os.utime(lock, (old, old))
    with index_lock(tmp_path, stale_s=60):                     # not refreshed for 2 minutes: abandoned
        pass


def test_the_holder_refreshes_the_lock_and_never_deletes_one_it_lost(tmp_path):
    lock = tmp_path / "ingest.lock"
    with index_lock(tmp_path, heartbeat_s=0.2):
        old = time.time() - 600
        os.utime(lock, (old, old))
        time.sleep(0.6)
        assert time.time() - lock.stat().st_mtime < 5          # the heartbeat touched it
        lock.write_text("other-host 1 2026-09-28T10:00:00", encoding="utf-8")   # taken over meanwhile
    assert lock.read_text(encoding="utf-8").startswith("other-host")          # not removed on exit
