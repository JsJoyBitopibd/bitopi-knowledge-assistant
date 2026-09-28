"""F1: access attributes of documents (factory, department, confidentiality, buyer_code), from folder
names and meta.yaml, stored with every chunk in the registry, the keyword index and the vector store."""
import sqlite3

import pytest

from _ingest_fakes import make_index
from ragbot.ingest.meta import attributes_for
from ragbot.store import Registry


def _pdf(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 placeholder")                 # attributes_for never opens the file
    return path


def test_factory_comes_from_the_first_folder_when_it_is_a_factory_code(tmp_path):
    assert attributes_for(_pdf(tmp_path / "TAL" / "Cutting SOP.pdf"), tmp_path)["factory"] == "TAL"
    assert attributes_for(_pdf(tmp_path / "tal" / "x" / "y.pdf"), tmp_path)["factory"] == "TAL"
    sop = attributes_for(_pdf(tmp_path / "SOP" / "IT Policy.pdf"), tmp_path)
    assert sop == {"factory": "ALL", "department": "Common", "confidentiality": "internal", "buyer_code": ""}
    assert attributes_for(_pdf(tmp_path / "loose.pdf"), tmp_path)["factory"] == "ALL"


def test_meta_yaml_applies_below_its_folder_and_deeper_overrides(tmp_path):
    (tmp_path / "meta.yaml").write_text("department: IT\n", encoding="utf-8")
    hr = tmp_path / "HR"
    _pdf(hr / "Payroll" / "p.pdf")
    (hr / "meta.yaml").write_text("department: HR\nconfidentiality: restricted\n", encoding="utf-8")
    (tmp_path / "Buyer" / "MARCO").mkdir(parents=True)
    (tmp_path / "Buyer" / "MARCO" / "meta.yaml").write_text("factory: tal\nbuyer_code: MARCO\n", encoding="utf-8")
    assert attributes_for(hr / "Payroll" / "p.pdf", tmp_path) == {
        "factory": "ALL", "department": "HR", "confidentiality": "restricted", "buyer_code": ""}
    assert attributes_for(_pdf(tmp_path / "SOP" / "s.pdf"), tmp_path)["department"] == "IT"
    marco = attributes_for(_pdf(tmp_path / "Buyer" / "MARCO" / "comments.pdf"), tmp_path)
    assert marco["factory"] == "TAL" and marco["buyer_code"] == "MARCO"


@pytest.mark.parametrize("meta, msg", [("confidentiality: secret\n", "confidentiality"),
                                       ("factory: T AL\n", "factory"),
                                       ("owner: someone\n", "unknown key")])
def test_an_invalid_meta_yaml_is_an_error_not_a_guess(tmp_path, meta, msg):
    (tmp_path / "X").mkdir()
    (tmp_path / "X" / "meta.yaml").write_text(meta, encoding="utf-8")
    with pytest.raises(ValueError, match=msg):
        attributes_for(_pdf(tmp_path / "X" / "a.pdf"), tmp_path)


def _fts(reg):
    return {r[0]: r[1:] for r in reg.db.execute(
        "SELECT source, factory, department, confidentiality, buyer_code FROM chunk_fts GROUP BY source")}


def test_attributes_reach_every_store(tmp_path, monkeypatch):
    root, reg, store, run, files = make_index(tmp_path, monkeypatch)
    files("cutting", folder="TAL")
    files("itpolicy", folder="SOP")
    run()
    assert {c.source: c.factory for c in store.rows.values()} == {"cutting.pdf": "TAL", "itpolicy.pdf": "ALL"}
    assert reg.document_attributes("cutting.pdf")["factory"] == "TAL"
    assert _fts(reg)["cutting.pdf"] == ("TAL", "Common", "internal", "")


def test_changed_meta_yaml_retags_without_re_embedding(tmp_path, monkeypatch):
    root, reg, store, run, files = make_index(tmp_path, monkeypatch)
    files("payroll", "leave", folder="HR")
    run()
    (root / "HR" / "meta.yaml").write_text("department: HR\nconfidentiality: restricted\n", encoding="utf-8")
    r = run()
    assert r["retagged"] == 2 and r["embedded"] == 0 and r["updated"] == 0
    assert {c.confidentiality for c in store.rows.values()} == {"restricted"}
    assert _fts(reg)["payroll.pdf"][1:3] == ("HR", "restricted")
    assert run()["retagged"] == 0                                # idempotent


def test_a_moved_file_is_retagged_in_place(tmp_path, monkeypatch):
    root, reg, store, run, files = make_index(tmp_path, monkeypatch)
    files("cutting", folder="SOP")
    run()
    (root / "TAL").mkdir()
    (root / "SOP" / "cutting.pdf").rename(root / "TAL" / "cutting.pdf")
    r = run()
    assert r["retagged"] == 1 and r["added"] == r["removed"] == 0
    assert {c.factory for c in store.rows.values()} == {"TAL"}


def test_invalid_meta_yaml_fails_new_files_and_keeps_indexed_ones(tmp_path, monkeypatch):
    root, reg, store, run, files = make_index(tmp_path, monkeypatch)
    files("old", folder="QA")
    run()
    files("new", folder="QA")
    (root / "QA" / "meta.yaml").write_text("confidentiality: top-secret\n", encoding="utf-8")
    r = run()
    assert r["failed"] == 2 and r["added"] == 0
    status = dict(reg.db.execute("SELECT source, status FROM document"))
    assert status == {"old.pdf": "ok", "new.pdf": "failed"} and "old.pdf" in store.sources()


def test_registry_from_before_f1_is_upgraded_and_retagged(tmp_path, monkeypatch):
    db = tmp_path / "index" / "registry.db"
    db.parent.mkdir()
    con = sqlite3.connect(db)
    con.executescript("""
      CREATE TABLE document(source TEXT PRIMARY KEY, title TEXT, category TEXT, doc_hash TEXT, pages INTEGER,
        chunks INTEGER, tables_ INTEGER, ocr_pages INTEGER, superseded INTEGER DEFAULT 0, status TEXT, error TEXT,
        ingested_at TEXT);
      CREATE TABLE chunk(id TEXT PRIMARY KEY, source TEXT, page INTEGER, section TEXT, kind TEXT, chars INTEGER);
      CREATE VIRTUAL TABLE chunk_fts USING fts5(id UNINDEXED, source UNINDEXED, category UNINDEXED,
        superseded UNINDEXED, body, tokenize='ascii');
      INSERT INTO document(source, category, status) VALUES('a.pdf', 'SOP', 'ok');
      INSERT INTO chunk(id, source, page) VALUES('a#p1#c1', 'a.pdf', 1);
      INSERT INTO chunk_fts(id, source, category, superseded, body) VALUES('a#p1#c1', 'a.pdf', 'SOP', 0, 'x');""")
    con.commit(); con.close()
    reg = Registry(db)
    cols = [r[1] for r in reg.db.execute("PRAGMA table_info(chunk_fts)")]
    assert "factory" in cols and reg.db.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0] == 0

    class Store:
        def all_ids_and_texts(self):
            return [("a#p1#c1", "SOP › 1\nold text")]

    assert reg.seed_fts(Store()) == 1
    assert reg.db.execute("SELECT factory FROM chunk_fts").fetchone()[0] is None     # untagged: scoped searches skip it
    assert reg.document_attributes("a.pdf") == {"factory": None, "department": None, "confidentiality": None,
                                                "buyer_code": None}
