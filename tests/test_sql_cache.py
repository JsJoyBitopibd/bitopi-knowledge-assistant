"""J2: a question answered today replays its generated SQL instead of calling the model again."""
from datetime import date, datetime

import pytest

import ragbot.data.schema_index as si
import ragbot.data.tools as tools
import ragbot.llm.base as base
from ragbot.auth.models import Scope
from ragbot.data.catalog import Catalog, View

from test_schema_rag import ScriptedSQL, Settings

SQL = "DATABASE: Demo\nSELECT TOP (5) ExportOrderID FROM rag.vw_ExportOrder"


def _cats():
    view = View("rag.vw_ExportOrder", "one row per order", "Orders.", ["ExportOrderID"], {"ExportOrderID": "id"})
    return {"Demo": Catalog("Demo", "tsql", "SQLSERVER_CONN_DEMO", ["Use TOP (n)."], [view])}


@pytest.fixture
def wired(monkeypatch):
    si._CACHE.clear()
    tools._SQL.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr("ragbot.data.schema_usage.log_selection", lambda *a, **k: None)
    monkeypatch.setattr(tools, "catalog_stamp", lambda folder=None: ("stamp",))
    ran: list[str] = []
    fail = {"next": False}

    def fake_run(engine, sql, params, **kw):
        if fail["next"]:
            fail["next"] = False
            raise RuntimeError("Invalid column name 'ExportOrderID'")
        ran.append(sql)
        return ["ExportOrderID"], [["TAL-25-1"]], datetime.now()
    monkeypatch.setattr(tools, "cached_run", fake_run)

    def install(replies, **over):
        monkeypatch.setattr(tools, "settings", lambda: Settings(**over))
        chat = ScriptedSQL(replies)
        monkeypatch.setattr(tools, "get_chat", lambda: chat)
        return chat, ran, fail
    yield install
    tools._SQL.clear()


def test_repeat_question_replays_the_sql_without_a_model_call(wired):
    chat, ran, _ = wired([SQL])                       # one scripted reply: a second model call would fail
    q = "Which export orders are listed?"
    first = tools.generate_and_run(q, _cats(), scope=Scope.unrestricted())
    second = tools.generate_and_run(q.upper() + "  ", _cats(), scope=Scope.unrestricted())   # same question, normalised
    assert first.error is None and second.error is None and not chat.replies
    assert not first.sql_cached and second.sql_cached
    assert ran[0] == ran[1] and second.sql == first.sql
    assert first.databases_considered == ["Demo"]


def test_refresh_generates_again(wired):
    chat, ran, _ = wired([SQL, SQL])
    tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    r = tools.generate_and_run("q", _cats(), refresh=True, scope=Scope.unrestricted())
    assert not chat.replies and not r.sql_cached and len(ran) == 2


def test_cache_off(wired):
    chat, ran, _ = wired([SQL, SQL], **{"data.sql_cache_ttl_seconds": 0})
    tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    r = tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    assert not chat.replies and not r.sql_cached


def test_failed_replay_is_dropped_and_regenerated(wired):
    chat, ran, fail = wired([SQL, SQL])
    tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    fail["next"] = True                                # the cached statement no longer runs (schema changed)
    r = tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    assert r.error is None and not r.sql_cached and not chat.replies and len(ran) == 2


# ---------------------------------------------------------------- P0: an empty result is not the day's answer
NULL_SQL = "DATABASE: Demo\nSELECT TOP (5) SUM(Qty) FROM rag.vw_ExportOrder"
BAD_SQL = "DATABASE: Demo\nDELETE FROM rag.vw_ExportOrder"      # refused by the guard every time


def test_a_blank_result_is_replayed_only_for_minutes_not_the_day(wired, monkeypatch):
    """8 Oct 2026: a SUM that returned NULL was cached for the day and failed again nine minutes later."""
    chat, ran, _ = wired([NULL_SQL, SQL])
    calls = {"n": 0}

    def blank_then_rows(engine, sql, params, **kw):
        calls["n"] += 1
        ran.append(sql)
        return (["Qty"], [[None]], datetime.now()) if calls["n"] == 1 else (["Qty"], [[42]], datetime.now())
    monkeypatch.setattr(tools, "cached_run", blank_then_rows)
    now = [1000.0]
    monkeypatch.setattr(tools._SQL, "clock", lambda: now[0])
    first = tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    assert first.rows == [[None]] and not first.sql_cached and len(chat.replies) == 1
    now[0] += 60                                       # a minute later: still replayed, no model call
    second = tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    assert second.sql_cached and len(chat.replies) == 1
    now[0] += 200                                      # past data.result_cache_ttl_seconds: generated afresh
    third = tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    assert not third.sql_cached and not chat.replies and third.rows == [[42]]


def test_a_rowed_statement_that_turns_blank_on_replay_is_forgotten(wired, monkeypatch):
    chat, ran, _ = wired([SQL, SQL])
    results = iter([(["N"], [[7]]), (["N"], [])])          # rows at first, nothing on the replay (data changed)
    monkeypatch.setattr(tools, "cached_run", lambda e, s, p, **kw: (*next(results), datetime.now()))
    now = [1000.0]
    monkeypatch.setattr(tools._SQL, "clock", lambda: now[0])
    tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    r = tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    assert r.sql_cached and r.rows == []
    key = tools._sql_key("q", _cats(), Scope.unrestricted())
    assert tools._SQL.get(key) is not None             # kept, but only briefly
    now[0] += 200
    assert tools._SQL.get(key) is None


def test_a_statement_that_fails_on_replay_and_on_regeneration_is_really_dropped(wired):
    """tools.py used to call TTLCache.put(key, None, 0), which is a no-op (ttl <= 0 returns early)."""
    chat, ran, fail = wired([SQL, BAD_SQL, BAD_SQL])
    tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    key = tools._sql_key("q", _cats(), Scope.unrestricted())
    assert tools._SQL.get(key) is not None
    fail["next"] = True
    r = tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    assert r.error and not chat.replies
    assert tools._SQL.get(key) is None


def test_every_attempt_is_logged_to_sql_gen(wired, monkeypatch):
    rows = []
    monkeypatch.setattr(tools, "log_generation", lambda **kw: rows.append(kw))
    chat, ran, _ = wired([BAD_SQL, SQL])
    r = tools.generate_and_run("q", _cats(), scope=Scope.unrestricted())
    assert r.error is None
    assert [x["kind"] for x in rows] == ["guard", "retry"]
    assert rows[0]["guard_error"].startswith("GuardError") and rows[0]["database_line"] == "DATABASE: Demo"
    assert rows[1]["rows"] == 1 and rows[1]["raw_reply"].startswith("DATABASE: Demo") and rows[1]["considered"] == ["Demo"]


def test_key_changes_with_day_catalogs_and_scope(monkeypatch):
    monkeypatch.setattr(tools, "catalog_stamp", lambda folder=None: ("stamp",))
    cats = _cats()
    k1 = tools._sql_key("How many orders?", cats, Scope.unrestricted())
    assert k1 == tools._sql_key("  how many ORDERS ", cats, Scope.unrestricted())

    class Tomorrow(date):
        @classmethod
        def today(cls):
            return date.today().fromordinal(date.today().toordinal() + 1)
    monkeypatch.setattr(tools, "date", Tomorrow)
    assert tools._sql_key("How many orders?", cats, Scope.unrestricted()) != k1          # literal dates go stale
    monkeypatch.setattr(tools, "date", date)
    monkeypatch.setattr(tools, "catalog_stamp", lambda folder=None: ("edited",))
    assert tools._sql_key("How many orders?", cats, Scope.unrestricted()) != k1          # an edited catalog
