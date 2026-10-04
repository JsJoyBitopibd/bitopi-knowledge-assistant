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
