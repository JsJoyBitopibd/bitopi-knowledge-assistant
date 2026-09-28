"""H0 (PRD FR-2.14): an empty or offline PDF folder must never empty the index. A missing file is
removed only after two consecutive scans miss it; a mass disappearance removes nothing and alerts."""
import json
import sqlite3

import pymupdf
import pytest

from ragbot.ingest import pipeline
from ragbot.store import Registry


class FakeStore:
    def __init__(self):
        self.rows = {}                                       # chunk id -> Chunk

    def upsert(self, chunks, vectors):
        self.rows.update({c.id: c for c in chunks})

    def delete_by_source(self, source):
        self.rows = {k: c for k, c in self.rows.items() if c.source != source}

    def set_superseded(self, source, superseded):
        pass

    def all_ids_and_texts(self):
        return [(k, c.text) for k, c in self.rows.items()]

    def sources(self):
        return {c.source for c in self.rows.values()}


class FakeEmbedder:
    def embed(self, texts):
        return [[float(len(t)), 1.0] for t in texts]


def make_pdf(path, word):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for n in range(2):
        page = doc.new_page()
        body = f"{n + 1}. Section {word} {n}\n" + (f"The {word} procedure step {n} is approved within two days. " * 14)
        page.insert_textbox(pymupdf.Rect(50, 60, 550, 780), body, fontsize=10)
    doc.save(path)
    doc.close()


@pytest.fixture
def index(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "get_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(pipeline, "log_dir", lambda: tmp_path)
    root, idx = tmp_path / "pdfs", tmp_path / "index"
    idx.mkdir()
    reg, store = Registry(idx / "registry.db"), FakeStore()

    def run(**kw):
        return pipeline.ingest_folder(root, store=store, reg=reg, index_dir=idx, **kw)

    def files(*names):
        for n in names:
            make_pdf(root / "SOP" / f"{n}.pdf", n)

    return root, reg, store, run, files


def missed(reg, source):
    return reg.db.execute("SELECT missed_scans FROM document WHERE source=?", (source,)).fetchone()[0]


def test_missing_folder_changes_nothing(index, tmp_path):
    root, reg, store, run, files = index
    files("alpha", "beta")
    assert run()["added"] == 2
    root.rename(tmp_path / "offline")                       # the share drops out
    r = run()
    assert "missing" in r["alert"] and r["removed"] == 0
    assert reg.all_sources() == {"alpha.pdf", "beta.pdf"} and store.sources() == {"alpha.pdf", "beta.pdf"}
    assert r["documents"] == 2                              # the summary still reports the intact index
    state = json.loads((tmp_path / "index" / "ingest_state.json").read_text(encoding="utf-8"))
    assert state["alert"] == r["alert"]


def test_empty_folder_changes_nothing_and_counts_nothing(index):
    root, reg, store, run, files = index
    files("alpha", "beta")
    run()
    for p in root.rglob("*.pdf"):
        p.unlink()
    for _ in range(3):                                     # however many passes: never deleted, never counted
        r = run()
        assert "no PDFs found" in r["alert"] and r["removed"] == 0
    assert store.sources() == {"alpha.pdf", "beta.pdf"} and missed(reg, "alpha.pdf") == 0


def test_removed_file_leaves_after_two_scans(index):
    root, reg, store, run, files = index
    files("alpha", "beta", "gamma")
    run()
    (root / "SOP" / "gamma.pdf").unlink()
    r1 = run()
    assert (r1["removed"], r1["pending_removal"]) == (0, 1) and "alert" not in r1
    assert "gamma.pdf" in store.sources() and missed(reg, "gamma.pdf") == 1
    r2 = run()
    assert (r2["removed"], r2["pending_removal"]) == (1, 0)
    assert "gamma.pdf" not in reg.all_sources() and store.sources() == {"alpha.pdf", "beta.pdf"}


def test_file_back_after_one_missed_scan_resets_the_count(index, tmp_path):
    root, reg, store, run, files = index
    files("alpha", "beta")
    run()
    aside = tmp_path / "beta.pdf"
    (root / "SOP" / "beta.pdf").rename(aside)
    assert run()["pending_removal"] == 1
    aside.rename(root / "SOP" / "beta.pdf")                 # back (e.g. a copy that was being replaced)
    r = run()
    assert r["pending_removal"] == 0 and missed(reg, "beta.pdf") == 0 and r["added"] == r["updated"] == 0
    (root / "SOP" / "beta.pdf").rename(aside)
    assert run()["removed"] == 0                            # the count started again from zero


def test_mass_disappearance_removes_nothing_until_confirmed(index):
    root, reg, store, run, files = index
    files("a1", "a2", "a3", "a4", "a5")
    run()
    for n in ("a1", "a2", "a3", "a4"):                     # 4 of 5 gone > max(3 files, 5 %)
        (root / "SOP" / f"{n}.pdf").unlink()
    for _ in range(2):
        r = run()
        assert "4 of 5 indexed files are missing" in r["alert"] and r["removed"] == 0
    assert len(store.sources()) == 5 and missed(reg, "a1.pdf") == 0
    r = run(confirm_removals=True)                          # deliberate bulk removal
    assert r["removed"] == 4 and "alert" not in r and store.sources() == {"a5.pdf"}


def test_confirm_removals_never_empties_the_index_from_an_empty_folder(index):
    root, reg, store, run, files = index
    files("alpha")
    run()
    (root / "SOP" / "alpha.pdf").unlink()
    r = run(confirm_removals=True)
    assert "no PDFs found" in r["alert"] and store.sources() == {"alpha.pdf"}


def test_unreadable_file_does_not_abort_the_run(index, monkeypatch):
    root, reg, store, run, files = index
    files("alpha", "beta")
    run()
    files("gamma", "delta")
    real = pipeline.sha256

    def flaky(path):
        if path.name in ("alpha.pdf", "gamma.pdf"):         # one known, one new file locked by another program
            raise PermissionError("locked")
        return real(path)

    monkeypatch.setattr(pipeline, "sha256", flaky)
    r = run()
    assert r["failed"] == 2 and r["added"] == 1             # delta still indexed
    assert "alpha.pdf" in store.sources()                   # the known file keeps its indexed version
    status = dict(reg.db.execute("SELECT source, status FROM document"))
    assert status["alpha.pdf"] == "ok" and status["gamma.pdf"] == "failed" and status["delta.pdf"] == "ok"


def test_failed_counts_this_run_not_the_registry_total(index):
    root, reg, store, run, files = index
    files("alpha")
    (root / "SOP" / "broken.pdf").write_bytes(b"not a pdf at all")
    r1 = run()
    assert r1["failed"] == 1 and r1["failed_total"] == 1
    r2 = run()                                              # unchanged broken file is skipped (same hash)
    assert r2["failed"] == 0 and r2["failed_total"] == 1


def test_registry_from_before_h0_gets_the_counter(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE document(source TEXT PRIMARY KEY, title TEXT, category TEXT, doc_hash TEXT, "
                "pages INTEGER, chunks INTEGER, tables_ INTEGER, ocr_pages INTEGER, superseded INTEGER DEFAULT 0, "
                "status TEXT, error TEXT, ingested_at TEXT)")
    con.execute("INSERT INTO document(source, status) VALUES('a.pdf', 'ok')")
    con.commit(); con.close()
    reg = Registry(db)
    assert missed(reg, "a.pdf") == 0
    assert reg.record_scan(set()) == {"a.pdf": 1} and reg.record_scan({"a.pdf"}) == {}
