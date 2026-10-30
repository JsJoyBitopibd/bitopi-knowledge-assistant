"""Phase U1: conversations kept per user on the server (src/ragbot/conversations.py)."""
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from ragbot import conversations as C
from ragbot.agent.templated import fmt
from ragbot.models import QueryResult, Reference


def _result(db="BitopiSplint", n=3):
    return QueryResult(database=db, engine="sqlserver", views=["rag.vw_ExportOrder"], sql="SELECT TOP (200) …",
                       columns=["ExportOrderID", "OrderQty", "PCD", "Seen"],
                       rows=[[f"TAL-{i}", Decimal("12.1250000000") if i == 0 else Decimal(100 + i), date(2026, 11, 2),
                              datetime(2026, 10, 30, 9, 15)] for i in range(n)],
                       key_columns=["ExportOrderID"], as_of=datetime(2026, 10, 30, 9, 0), tool="t")


def _ref(db="BitopiSplint"):
    return Reference(marker="D1", kind="data", database=db, views=["rag.vw_ExportOrder"], row_keys=["ExportOrderID=TAL-0"],
                     row_count=3, as_of=datetime(2026, 10, 30, 9, 0), sql="SELECT …")


def _turns(db="BitopiSplint"):
    return [{"role": "user", "text": "Which TAL orders have a PCD next week?"},
            {"role": "assistant", "q": "Which TAL orders have a PCD next week?", "request_id": "r1", "route": "data",
             "text": "3 TAL order(s) have a PCD next week [D1]:\n\n| ExportOrderID | OrderQty |\n|---|---|\n| TAL-0 | 12.125 |",
             "refs": [_ref(db)], "results": [_result(db)], "follow_ups": ["What are the next PCDs at TAL?"],
             "refine_suggestions": ["Buyer wise"], "not_found": False, "kind": "", "seconds": 1.2}]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "restricted_databases", lambda: frozenset({"HR"}))
    return tmp_path / "conversations.db"


def test_round_trip_keeps_rows_refs_and_what_the_app_needs(db):
    C.save("c1", "jdoe", "f=*", "TAL orders", _turns(), path=db)
    back = C.load("c1", "jdoe", path=db)
    assert [t["role"] for t in back] == ["user", "assistant"]
    a = back[1]
    assert a["request_id"] == "r1" and a["refine_suggestions"] == ["Buyer wise"] and a["seconds"] == 1.2
    assert a["refs"][0].row_keys == ["ExportOrderID=TAL-0"] and a["results"][0].key_columns == ["ExportOrderID"]
    before, after = _result(), a["results"][0]
    assert [[fmt(v) for v in r] for r in after.rows] == [[fmt(v) for v in r] for r in before.rows]  # same cells shown
    assert after.as_of == before.as_of


def test_another_user_reads_nothing_and_cannot_overwrite(db):
    C.save("c1", "jdoe", "f=*", "TAL orders", _turns(), path=db)
    assert C.load("c1", "ann", path=db) is None and C.recent("ann", path=db) == []
    with pytest.raises(PermissionError):
        C.save("c1", "ann", "f=*", "mine now", _turns(), path=db)
    with pytest.raises(ValueError):
        C.save("c2", "", "f=*", "x", _turns(), path=db)


def test_rows_of_a_restricted_database_are_never_stored(db):
    turns = _turns("HR")
    C.save("c1", "jdoe", "f=*", "HR", turns, path=db)
    a = C.load("c1", "jdoe", path=db)[1]
    assert a["results"] == [] and a["results_withheld"] == "restricted"
    assert "| TAL-0 |" not in a["text"] and "not kept between sessions" in a["text"]
    assert a["refs"][0].row_keys is None
    import sqlite3
    raw = sqlite3.connect(db).execute("SELECT results, refs, text FROM turn WHERE role = 'assistant'").fetchone()
    assert raw[0] is None and "TAL-0" not in (raw[1] or "") and "TAL-0" not in raw[2]


def test_catalogs_that_cannot_be_read_withhold_everything(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "restricted_databases", lambda: frozenset({"*"}))
    C.save("c1", "jdoe", "f=*", "t", _turns(), path=tmp_path / "x.db")
    assert C.load("c1", "jdoe", path=tmp_path / "x.db")[1]["results"] == []


def test_a_huge_result_is_withheld_for_its_size(db, monkeypatch):
    monkeypatch.setattr(C, "MAX_RESULTS_BYTES", 200)
    C.save("c1", "jdoe", "f=*", "t", _turns(), path=db)
    a = C.load("c1", "jdoe", path=db)[1]
    assert a["results"] == [] and a["results_withheld"] == "size" and "| TAL-0 |" in a["text"]


def test_one_write_per_question_appends_from_an_index(db):
    t = _turns()
    C.save("c1", "jdoe", "f=*", "TAL orders", t, path=db)
    more = t + [{"role": "user", "text": "buyer wise"}, {**t[1], "request_id": "r2", "q": "buyer wise"}]
    C.save("c1", "jdoe", "f=*", "TAL orders", more, from_idx=2, path=db)
    back = C.load("c1", "jdoe", path=db)
    assert len(back) == 4 and back[3]["request_id"] == "r2"


def test_recent_rename_delete_and_purge(db):
    for i in range(25):
        C.save(f"c{i}", "jdoe", "f=*", f"Conversation {i}", _turns(), path=db)
    assert len(C.recent("jdoe", path=db)) == 20 and C.recent("jdoe", limit=3, path=db)[0]["id"] == "c24"
    assert C.rename("c3", "jdoe", "  Renamed  ", path=db) and not C.rename("c3", "ann", "x", path=db)
    assert next(r for r in C.recent("jdoe", limit=30, path=db) if r["id"] == "c3")["title"] == "Renamed"
    assert C.delete("c3", "jdoe", path=db) and C.load("c3", "jdoe", path=db) is None
    assert not C.delete("c4", "ann", path=db)
    assert C.purge(90, path=db) == 1                                       # only the deleted one
    assert C.purge(0, now=datetime.now() + timedelta(days=1), path=db) == 24   # everything older than the cutoff


def test_recent_questions_are_answered_distinct_and_newest_first(db):
    t = _turns()
    not_found = [{"role": "user", "text": "zzz?"}, {"role": "assistant", "q": "zzz?", "text": "System …", "not_found": True}]
    hello = [{"role": "user", "text": "hi"}, {"role": "assistant", "q": "hi", "text": "Hello!", "kind": "social"}]
    C.save("c1", "jdoe", "f=*", "a", t + not_found + hello + t, path=db)
    assert C.recent_questions("jdoe", path=db) == ["Which TAL orders have a PCD next week?"]
    assert C.recent_questions("ann", path=db) == []


def test_a_missing_file_reads_as_empty(tmp_path):
    p = tmp_path / "none.db"
    assert C.recent("jdoe", path=p) == [] and C.load("c", "jdoe", path=p) is None and C.purge(1, path=p) == 0
    assert C.recent_questions("jdoe", path=p) == [] and not p.exists()
