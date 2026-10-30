"""Phase T4: a follow-up the stored rows cannot answer runs the earlier query again (data/requery.py)."""
from datetime import date, datetime

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent import refine
from ragbot.agent.citations import verify
from ragbot.agent.events import Final, Stage
from ragbot.agent.state import ConversationState
from ragbot.auth.models import Scope
from ragbot.data import requery as RQ
from ragbot.data import tools as T
from ragbot.data.guard import GuardError
from ragbot.llm.base import ChatModel, ChatReply
from ragbot.models import QueryResult

U = Scope.unrestricted()


def _orders(n=200, tool="orders_by_factory_ship_window", sql=None, **params):
    rows = [[f"TAL-25-{1000 + i}-1", "TAL", "Zara", 100 + i, date(2026, 11, 3)] for i in range(n)]
    p = {"factory": "TAL", "window": "next week", "from": date(2026, 11, 2), "to": date(2026, 11, 9), **params}
    return QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], tool=tool,
                       sql=sql or T.tool_sql(next(t for t in T.load_fixed_tools() if t["name"] == tool), p),
                       columns=["ExportOrderID", "Factory", "Buyer", "OrderQty", "ShipDate"], rows=rows, params=p,
                       key_columns=["ExportOrderID"], as_of=datetime(2026, 10, 30, 9))


def _tool(name):
    return next(t for t in T.load_fixed_tools() if t["name"] == name)


@pytest.fixture
def ran(monkeypatch):
    """Captures what run_fixed_tool / _run_guarded were asked to run, and returns a small result."""
    calls = []

    def fixed(tool, params, cats, user="", refresh=False, *, scope, page=0):
        calls.append(("fixed", tool["name"], dict(params), page))
        return QueryResult(database=tool["database"], engine="sqlserver", views=["rag.vw_ExportOrder"],
                           sql=T.tool_sql(tool, params), params=params, tool=tool["name"],
                           columns=["ExportOrderID", "OrderQty"], rows=[[f"E{i}", i] for i in range(30)])

    def guarded(sql, cat, *, scope, user, refresh, tool="generated", considered=(), agent="", cats=None,
                max_offset=0):
        calls.append(("guarded", sql, max_offset))
        if "BAD" in sql:
            raise GuardError("table not in catalog")
        return QueryResult(database=cat.database, engine="sqlserver", views=["rag.vw_ExportOrder"], sql=sql,
                           tool=tool, columns=["ExportOrderID", "OrderQty"], rows=[[f"G{i}", i] for i in range(12)])
    monkeypatch.setattr(T, "run_fixed_tool", fixed)
    monkeypatch.setattr(T, "_run_guarded", guarded)
    monkeypatch.setattr(RQ, "_tables_text", lambda *a: "(none)")
    return calls


class Chat(ChatModel):
    name = "fake"

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def _chat(self, messages, system, max_tokens, temperature):
        self.seen.append((system, [m["content"] for m in messages]))
        return ChatReply(text=self.replies.pop(0), input_tokens=1, output_tokens=1, model="fake", stop_reason="stop")

    def _stream(self, *a):
        raise AssertionError


# ---------------------------------------------------------------- 1. pages
def test_the_next_page_of_a_fixed_tool_runs_its_own_sql_with_no_model(ran, monkeypatch):
    monkeypatch.setattr(RQ, "get_chat", lambda: (_ for _ in ()).throw(AssertionError("no model for a page")))
    prior = _orders()
    r = RQ.requery(refine.parse("next", prior), prior, "TAL orders shipping next week?", scope=U, parent="r1")
    assert ran[0][:2] == ("fixed", "orders_by_factory_ship_window") and ran[0][3] == 1
    assert ran[0][2]["factory"] == "TAL" and "of_rows_label" not in ran[0][2]
    assert r.page == 1 and r.transform == "page 2" and r.derived_from == "r1"
    assert r.params["rows_from_label"] == "201" and r.params["rows_to_label"] == "230"


def test_a_generated_page_is_guarded_with_paging_allowed(ran):
    prior = _orders(tool="generated", sql="SELECT TOP (200) ExportOrderID, OrderQty FROM rag.vw_ExportOrder "
                                          "ORDER BY OrderQty DESC")
    r = RQ.requery(refine.parse("show more", prior), prior, "q", scope=U)
    kind, sql, max_offset = ran[0]
    assert kind == "guarded" and "OFFSET 200 ROWS FETCH FIRST 200 ROWS ONLY" in sql and max_offset == 2000
    assert r.page == 1


def test_no_page_beyond_the_last_one_allowed(ran):
    deep = _orders().model_copy(update={"page": 10})                     # rows 2,001+: past data.max_page_offset
    assert RQ.requery(refine.parse("next", deep), deep, "q", scope=U) is None
    assert ran == []


def test_run_fixed_tool_refuses_to_page_a_python_filtered_tool():
    """Checked before any connection is opened: the dashboard reads every factory and keeps one in Python."""
    r = T.run_fixed_tool(_tool("production_dashboard_window"), {"factory": "TAL", "window": "today",
                                                                "from": date(2026, 10, 30), "to": date(2026, 10, 31)},
                         RQ.load_catalogs(), scope=U, page=1)
    assert r.error == "this list cannot be paged" and r.rows == []


# ---------------------------------------------------------------- 2. a fixed tool run again
def test_another_period_reruns_the_tool_with_its_window(ran):
    prior = _orders(30)
    r = RQ.requery(refine.parse("last week?", prior), prior, "TAL orders shipping next week?", scope=U)
    kind, name, params, page = ran[0]
    assert (kind, name, page) == ("fixed", "orders_by_factory_ship_window", 0)
    assert params["window"] == "last week" and params["factory"] == "TAL" and params["from"] < params["to"]
    assert r.derived_from == ""


def test_a_sort_on_a_cut_list_reruns_the_tool_sorted_in_sql_then_cuts(ran):
    prior = _orders(200)
    plan = refine.parse("sort by quantity, top 10", prior)
    assert refine.needs_requery(plan, prior, 200) == "capped"
    r = RQ.requery(plan, prior, "q", scope=U)
    params = ran[0][2]
    assert params["order_by"] == "[OrderQty] DESC" and params["sort_label"] == "sorted by OrderQty (largest first)"
    assert len(r.rows) == 10 and r.params["shown_label"] == "first 10" and r.params["of_rows_label"] == "30"


def test_a_regrouping_reruns_a_group_by_tool(ran):
    tool = _tool("quality_by_group_window")
    spec = next(sp for sp in tool["params"].values() if sp.get("type") == "group_by")
    prior = QueryResult(database=tool["database"], engine="sqlserver", views=["rag.vw_PlanQualityDay"],
                        tool=tool["name"], sql=T.tool_sql(tool), columns=["Factory", "Unit", "DHU"],
                        rows=[["TAL", "U1", 2.1], ["TAL", "U2", 3.0], ["RHL", "U1", 1.5]],
                        params={"window": "today", "from": date(2026, 10, 30), "to": date(2026, 10, 31),
                                "factory": ""}, as_of=datetime(2026, 10, 30, 9))
    plan = refine.parse("style wise", prior)
    assert plan.regroup == ["style"]
    RQ.requery(plan, prior, "unit wise DHU today", scope=U)
    kind, name, params, _ = ran[0]
    assert (kind, name) == ("fixed", tool["name"]) and spec["columns"]["style"] in params[[n for n, sp in
                                                         tool["params"].items() if sp.get("type") == "group_by"][0]]


# ---------------------------------------------------------------- 3. one model call
def test_a_missing_column_asks_the_model_to_modify_the_statement(ran, monkeypatch):
    chat = Chat(["DATABASE: BitopiSplint\nSELECT TOP (200) ExportOrderID, Buyer, StyleName FROM rag.vw_ExportOrder"])
    monkeypatch.setattr(RQ, "get_chat", lambda: chat)
    prior = _orders(30)
    plan = refine.parse("show the style beside each order", prior)
    assert plan.add == ["style"]
    r = RQ.requery(plan, prior, "TAL orders shipping next week?", scope=U)
    system, msgs = chat.seen[0]
    assert "rag.vw_ExportOrder" in system and "Today is" in system
    m = msgs[0]
    assert "Previous question: TAL orders shipping next week?" in m and "@factory = TAL" in m
    assert "also return the column(s) for: style" in m and "Its columns: ExportOrderID, Factory" in m
    assert "TAL-25-1000-1" not in m                                          # never a row to the model
    assert ran[0][0] == "guarded" and r.rows and not r.error


def test_the_model_gets_one_retry_and_may_say_it_cannot(ran, monkeypatch):
    chat = Chat(["DATABASE: BitopiSplint\nSELECT TOP (200) x FROM BAD", "DATABASE: BitopiSplint\nSELECT TOP (200) "
                 "ExportOrderID FROM rag.vw_ExportOrder"])
    monkeypatch.setattr(RQ, "get_chat", lambda: chat)
    prior = _orders(30)
    r = RQ.requery(refine.parse("show the style beside each order", prior), prior, "q", scope=U)
    assert len(chat.seen) == 2 and "table not in catalog" in chat.seen[1][1][-1] and not r.error
    monkeypatch.setattr(RQ, "get_chat", lambda: Chat(["CANNOT: no style column"]))
    assert RQ.requery(refine.parse("show the style beside each order", prior), prior, "q", scope=U) is None


def test_a_database_the_user_may_not_read_is_never_requeried(ran, monkeypatch):
    prior = _orders(30)
    monkeypatch.setattr(RQ, "load_catalogs", lambda: {"BitopiSplint": type("C", (), {"restricted_to": ("KA-HR",)})()})
    assert RQ.requery(refine.parse("last week?", prior), prior, "q", scope=Scope(factories=["*"])) is None
    assert ran == []


# ---------------------------------------------------------------- through the orchestrator
@pytest.fixture
def wired(monkeypatch):
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: a)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    for name in ("retrieve", "_route", "standalone_question"):
        monkeypatch.setattr(orch, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(name)))


def _state(prior):
    return ConversationState.from_history([{"role": "assistant", "text": "…", "q": "TAL orders shipping next week?",
                                            "results": [prior], "request_id": "req1", "route": "data"}], "c1")


def test_next_page_through_the_orchestrator(wired, ran):
    evs = list(orch.answer_stream("show the next 200 rows", scope=U, state=_state(_orders())))
    assert [e.name for e in evs if isinstance(e, Stage)] == ["understanding", "querying"]
    a = evs[-1].answer
    assert isinstance(evs[-1], Final) and a.route == "requery" and a.kind == "requery" and a.prior_request_id == "req1"
    assert a.text.startswith("Rows 201 to 230 of the same query, the last ones [D1]:")
    assert verify(a.text, [], a.results, "System doesn't have the data.")[0]
    assert a.usage.calls == 0


def test_another_period_through_the_orchestrator_uses_the_tool_lead(wired, ran):
    a = list(orch.answer_stream("last week?", scope=U, state=_state(_orders(30))))[-1].answer
    assert a.route == "requery" and "TAL order(s) have a ship date last week" in a.text


def test_only_a_value_not_in_the_rows_asks_the_model_to_add_the_filter(ran, monkeypatch):
    chat = Chat(["DATABASE: BitopiSplint\nSELECT TOP (200) ExportOrderID FROM rag.vw_ExportOrder WHERE Factory = 'TAL' "
                 "AND Buyer LIKE '%uniqlo%'"])
    monkeypatch.setattr(RQ, "get_chat", lambda: chat)
    prior = _orders(200)
    plan = refine.parse("only uniqlo", prior)
    assert plan.unknown_filter == "uniqlo"
    RQ.requery(plan, prior, "TAL orders shipping next week?", scope=U)
    m = chat.seen[0][1][0]
    assert 'keep only the rows for "uniqlo"' in m and "keep every other filter" in m and "@factory = TAL" in m
    assert ran[0][0] == "guarded"                    # not a fixed tool re-run: the tool has no buyer parameter


def test_an_empty_requery_says_nothing_matches_instead_of_asking_afresh(wired, monkeypatch):
    empty = QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], sql="SELECT …",
                        columns=["ExportOrderID"], rows=[])
    monkeypatch.setattr(RQ, "requery", lambda *a, **k: empty)
    a = list(orch.answer_stream("only uniqlo", scope=U, state=_state(_orders())))[-1].answer
    assert a.route == "requery" and a.not_found
    assert a.text == ("System doesn't have the data. I ran the earlier question again for \"uniqlo\" only, "
                      "and no rows match.")
