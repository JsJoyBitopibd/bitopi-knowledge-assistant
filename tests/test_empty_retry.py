"""P2.2: a model-written statement that returns nothing is repaired within the bounds — a DISTINCT lookup, a
corrected re-run on a close match, one re-ask with the recorded values or the other listed tables — or it ends
as a not-found that says what was checked. Offline: scripted model, fake runner."""
from datetime import datetime

import pytest

import ragbot.data.empty_retry as er
import ragbot.data.schema_index as si
import ragbot.data.tools as tools
import ragbot.llm.base as base
from ragbot.auth.models import Scope
from ragbot.data.catalog import Catalog, Table, View
from ragbot.models import QueryResult

from test_schema_rag import ScriptedSQL, Settings

STORES = ["Central Store (Misami)", "Maintenance Store (Misami)", "Misami Production Floor"]
Q = "closing balance of 1178108 at company 02 in the mantainance store"
FIRST = ("DATABASE: Demo\nSELECT TOP (200) SUM(GRNQty) - SUM(IssueQty) AS Balance FROM dbo.Balance "
         "WHERE ItemCode = '1178108' AND CompanyID = '02' AND SLolcation = 'mantainance store'")


def _cat():
    col = lambda n: {"name": n, "type": "varchar"}  # noqa: E731
    view = View("rag.vw_Stock", "one row per item", "Stock.", ["ItemCode"], {"ItemCode": "id", "StoreName": "store"},
                definition="SELECT ItemCode, StoreName, Qty FROM dbo.Balance", scope_column="Factory")
    return {"Demo": Catalog("Demo", "tsql", "SQLSERVER_CONN_DEMO", [], [view], tables=[
        Table("dbo.Balance", rows=100, columns=[col("ItemCode"), col("CompanyID"), col("SLolcation"), col("GRNQty"), col("IssueQty")]),
        Table("dbo.Other", rows=10, columns=[col("ItemCode"), col("Qty")])])}


@pytest.fixture
def wired(monkeypatch):
    si._CACHE.clear()
    tools._SQL.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr("ragbot.data.schema_usage.log_selection", lambda *a, **k: None)
    monkeypatch.setattr(tools, "log_generation", lambda **kw: None)
    monkeypatch.setattr(tools, "catalog_stamp", lambda folder=None: ("stamp",))
    monkeypatch.setattr(si, "json_sha", lambda cat: "x")
    monkeypatch.setattr(si, "vectors_path", lambda db: __import__("pathlib").Path("does-not-exist.npz"))
    import ragbot.retrieve.retriever as rt
    monkeypatch.setattr(rt, "_embed_query", lambda q: (_ for _ in ()).throw(RuntimeError("no model")))
    ran: list[tuple[str, str]] = []

    def install(replies, runner, **over):
        monkeypatch.setattr(tools, "settings", lambda: Settings(**over))
        monkeypatch.setattr(er, "settings", lambda: Settings(**over))      # the caps live in the repair module

        def fake_run(engine, sql, params, **kw):
            ran.append((kw.get("tool", "generated"), sql))
            return runner(sql, kw.get("tool", "generated"))
        monkeypatch.setattr(tools, "cached_run", fake_run)
        chat = ScriptedSQL(replies)
        monkeypatch.setattr(tools, "get_chat", lambda: chat)
        return chat, ran
    yield install
    tools._SQL.clear()


def _runner(balance_for=("Maintenance Store (Misami)",)):
    def run(sql, tool):
        if tool == "lookup":
            return ["SLolcation"], [[s] for s in STORES], datetime.now()
        if any(f"'{s}'" in sql for s in balance_for):
            return ["Balance"], [[0.875]], datetime.now()
        return ["Balance"], [[None]], datetime.now()
    return run


# ---------------------------------------------------------------- the pieces
def test_shape_finds_the_text_and_identifier_filters_of_a_single_table_statement():
    sh = er.shape_of(FIRST.split("\n", 1)[1], "tsql")
    assert sh.table == "dbo.Balance"
    assert [(f.column, f.literal) for f in sh.filters] == [("ItemCode", "1178108"), ("CompanyID", "02"), ("SLolcation", "mantainance store")]
    assert [f.column for f in sh.text_filters] == ["SLolcation"]
    assert [f.column for f in sh.identifier_filters] == ["ItemCode", "CompanyID"]
    assert er.shape_of("SELECT a.X FROM dbo.A a JOIN dbo.B b ON a.I = b.I WHERE a.N = 'x'", "tsql").table == ""


def test_lookup_sql_keeps_the_other_filters_and_drops_the_one_looked_up():
    sh = er.shape_of(FIRST.split("\n", 1)[1], "tsql")
    sql = er.lookup_sql(sh, sh.text_filters[0], "tsql", 20)
    assert sql == ("SELECT DISTINCT TOP (20) [SLolcation] FROM dbo.Balance WHERE ItemCode = '1178108' "
                   "AND CompanyID = '02' ORDER BY [SLolcation]")


def test_closest_exact_match_tie_and_none():
    assert er.closest("maintenance store (misami)", STORES, 0.85) == ("exact", "Maintenance Store (Misami)")
    assert er.closest("mantainance store", STORES, 0.85) == ("match", "Maintenance Store (Misami)")
    assert er.closest("xyz store", STORES, 0.85) == ("none", None)
    two = ["Maintenance Store (Misami)", "Maintenance Store (Tarasima)"]
    assert er.closest("mantainance store", two, 0.85) == ("none", None)        # a tie is never guessed between


def test_substitute_replaces_one_quoted_literal_only():
    sql = "SELECT 1 FROM t WHERE a = 'x' AND b = 'x'"
    assert er.substitute(sql, "x", "y") == "SELECT 1 FROM t WHERE a = 'y' AND b = 'x'"
    assert er.substitute(sql, "o'k", "fine") == sql


# ---------------------------------------------------------------- through generate_and_run
def test_a_misspelled_literal_is_corrected_from_the_recorded_values_without_a_model_call(wired):
    chat, ran = wired([FIRST], _runner())                      # one scripted reply: a second model call would fail
    r = tools.generate_and_run(Q, _cat(), scope=Scope.unrestricted())
    assert r.rows == [[0.875]] and not chat.replies
    assert r.corrections == [["SLolcation", "mantainance store", "Maintenance Store (Misami)"]]
    assert r.params["SLolcation"] == "Maintenance Store (Misami)" and r.filters["SLolcation"] == "Maintenance Store (Misami)"
    assert "SLolcation = 'Maintenance Store (Misami)'" in r.sql and r.checked
    assert [t for t, _ in ran] == ["generated", "lookup", "generated"]
    assert len(r.lookups) == 1 and r.lookups[0].tool == "lookup" and r.lookups[0].rows == [[s] for s in STORES]
    replay = tools.generate_and_run(Q, _cat(), scope=Scope.unrestricted())            # the corrected statement is the day's
    assert replay.sql_cached and replay.rows == [[0.875]]


def test_an_exact_recorded_value_means_the_zero_is_real(wired):
    first = FIRST.replace("mantainance store", "Maintenance Store (Misami)")
    chat, ran = wired([first], _runner(balance_for=()))
    r = tools.generate_and_run(Q, _cat(), scope=Scope.unrestricted())
    assert r.rows == [[None]] and r.corrections == [] and r.candidates == [] and r.checked
    assert [t for t, _ in ran] == ["generated", "lookup"] and not chat.replies


def test_no_close_match_asks_the_model_once_with_the_recorded_values(wired):
    far = FIRST.replace("mantainance store", "xyz store")
    second = "DATABASE: Demo\nSELECT TOP (200) SUM(GRNQty) AS Balance FROM dbo.Balance WHERE SLolcation = 'Central Store (Misami)'"
    chat, ran = wired([far, second], _runner(balance_for=("Central Store (Misami)",)))
    r = tools.generate_and_run(Q, _cat(), scope=Scope.unrestricted())
    assert r.rows == [[0.875]] and not chat.replies and r.checked
    assert "Maintenance Store (Misami)" in chat.messages_seen[-1]["content"]      # the recorded values were shown
    assert [t for t, _ in ran] == ["generated", "lookup", "generated"]


def test_still_nothing_ends_with_filters_and_candidates_for_the_not_found_reply(wired):
    far = FIRST.replace("mantainance store", "xyz store")
    chat, ran = wired([far, far], _runner(balance_for=()))
    r = tools.generate_and_run(Q, _cat(), scope=Scope.unrestricted())
    assert r.rows == [[None]] and r.candidates == STORES and r.checked
    assert r.filters == {"ItemCode": "1178108", "CompanyID": "02", "SLolcation": "xyz store"}
    assert not chat.replies                                                          # exactly one re-ask


def test_identifier_only_filters_get_one_table_retry(wired, monkeypatch):
    ids = "DATABASE: Demo\nSELECT TOP (200) Qty FROM dbo.Other WHERE ItemCode = '1178108'"
    better = "DATABASE: Demo\nSELECT TOP (200) GRNQty FROM dbo.Balance WHERE ItemCode = '1178108'"

    def run(sql, tool):
        return (["GRNQty"], [[9]], datetime.now()) if "dbo.Balance" in sql else (["Qty"], [], datetime.now())
    chat, ran = wired([ids, better], run)
    monkeypatch.setattr(tools, "_schema_selection", lambda q, cats, qvec=None: {"Demo": ["dbo.Other", "dbo.Balance"]})
    r = tools.generate_and_run("qty of item 1178108", _cat(), scope=Scope.unrestricted())
    assert r.rows == [[9]] and not chat.replies and r.checked
    assert "dbo.Balance" in chat.messages_seen[-1]["content"]


def test_the_caps_hold(wired):
    far = FIRST.replace("mantainance store", "xyz store")
    chat, ran = wired([far, far, far], _runner(balance_for=()), **{"data.empty_retry.max_model_calls": 0})
    r = tools.generate_and_run(Q, _cat(), scope=Scope.unrestricted())
    assert r.rows == [[None]] and len(chat.replies) == 2                             # no re-ask at all
    assert [t for t, _ in ran] == ["generated", "lookup"]                            # the lookup is free


def test_off_by_setting_or_route_keeps_the_single_shot(wired):
    chat, ran = wired([FIRST], _runner(), **{"data.empty_retry.enabled": False})
    r = tools.generate_and_run(Q, _cat(), scope=Scope.unrestricted())
    assert r.rows == [[None]] and ran == [("generated", ran[0][1])] and not r.checked
    chat, ran = wired([FIRST], _runner())
    ran.clear()
    with er.allowed(False):
        r = tools.generate_and_run(Q + " ", _cat(), scope=Scope.unrestricted())
    assert r.rows == [[None]] and [t for t, _ in ran] == ["generated"]


def test_a_lookup_the_guard_refuses_is_skipped(wired):
    big = FIRST.replace("dbo.Balance", "dbo.Big").replace("AND CompanyID = '02' ", "").replace("ItemCode = '1178108' AND ", "")
    cats = _cat()
    cats["Demo"].tables.append(Table("dbo.Big", rows=5_000_000, columns=[{"name": "SLolcation", "type": "varchar"},
                                                                         {"name": "GRNQty", "type": "int"}, {"name": "IssueQty", "type": "int"}]))
    cats["Demo"].__dict__.pop("_offered", None)
    chat, ran = wired([big, big], _runner(balance_for=()), **{"data.empty_retry.max_model_calls": 0})
    r = tools.generate_and_run("stock in the mantainance store", cats, scope=Scope.unrestricted())
    assert [t for t, _ in ran] == ["generated"]            # the DISTINCT over 5M rows with no WHERE never ran
    assert r.candidates == [] and r.lookups == []
