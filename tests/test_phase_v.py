"""Phase V (v1.18.0): the PPC fabric & trims quality data, the data horizon on this restored server, deterministic
not-found sentences, and the capability / routing phrasings from the 3 Nov 2026 conversation."""
from datetime import date, timedelta

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.data.freshness as freshness
import ragbot.data.tools as tools
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent import meta, not_found, router
from ragbot.agent.state import ConversationState
from ragbot.auth.models import Scope
from ragbot.data.catalog import load_catalogs
from ragbot.data.tools import LATEST, LATEST_NOTE, load_fixed_tools, match_fixed_tool
from ragbot.models import QueryResult

from test_answer_stream import FakeChat

TOOLS = load_fixed_tools()
DAY = date(2026, 10, 29)


def _tool(name):
    return next(t for t in TOOLS if t["name"] == name)


# ---------------------------------------------------------------- the catalog
def test_every_ppc_view_is_scoped_and_dates_its_newest_record():
    cat = load_catalogs()["Production"]
    ppc = [v for v in cat.views if v.name.startswith(("rag.vw_Fabric", "rag.vw_Trims", "rag.vw_Metal",
                                                      "rag.vw_Nickel", "rag.vw_Lot"))]
    assert len(ppc) == 11
    for v in ppc:
        assert v.scope_column == "Factory", v.name
        assert v.freshness_columns and all(c in v.columns for c in v.freshness_columns), v.name
        assert "BuyerID" in v.columns or v.name == "rag.vw_Placeholder"
    assert cat.label == "Production (PPM & PPC)" and cat.connection_database == "Production"
    assert cat.max_rendered_columns == 70


def test_the_bowing_table_is_described_and_the_test_copies_are_hidden():
    cats = load_catalogs()
    assert "bowing" in cats["Production"].table("PPC.HeadCutting").description.lower()
    assert "yarn count" in cats["Production"].table("PPC.FabricYernCount").description.lower()
    planning = cats["PlanningTNA"]
    assert planning.excluded("dbo.PPC_HeadCutting") and planning.excluded("dbo.VwPPCHeadCutting")
    assert cats["Production"].excluded("PPC.VisualInspectionChild_DupDeleted_20260726")


# ---------------------------------------------------------------- the questions of 3 Nov 2026 and their tools
@pytest.mark.parametrize("q,name", [
    ("Show me yesterdays bowing test report", "fabric_bowing_summary"),
    ("Bowing test", "fabric_bowing_summary"),
    ("bowing test of each roll of FR-26-2382", "fabric_bowing_by_roll"),
    ("yarn count?", "fabric_yarn_count"),
    ("shrinkage test summary on 29 Oct 2026", "fabric_shrinkage_summary"),
    ("which fabric failed 4-point inspection yesterday?", "fabric_4point_failed"),
    ("4-point inspection results today", "fabric_4point_inspections"),
    ("fabric received in PPC yesterday", "fabric_receipts_window"),
    ("Which trims failed AQL inspection this week?", "trims_failed_inspections"),
    ("status of fabric receipt GR-26-09820", "fabric_lot_status"),
    ("metal detection tests", "trims_metal_detection"),
    ("nickel test results", "trims_nickel_tests"),
])
def test_ppc_questions_reach_their_tools(q, name):
    hit = match_fixed_tool(q, TOOLS)
    assert hit is not None and hit[0]["name"] == name


@pytest.mark.parametrize("q", ["Goods Receipt yesterday", "goods receipts of item 1178108 at company 02",
                               "How many shade variation defects today?", "trims stock balance of item 1178108",
                               "What was the AQL result of the last final audit?"])
def test_other_questions_are_not_taken_by_a_ppc_tool(q):
    hit = match_fixed_tool(q, TOOLS)
    assert hit is None or hit[1] is None or not hit[0]["name"].startswith(("fabric_", "trims_"))


def test_yesterdays_is_yesterday_and_no_period_is_the_latest_day_on_record():
    _, params = match_fixed_tool("Show me yesterdays bowing test report", TOOLS)
    assert params["from"] == date.today() - timedelta(days=1) and params["window"] == "yesterday"
    assert params["window_note"] == ""
    _, params = match_fixed_tool("Bowing test", TOOLS)
    assert params["window"] == LATEST and "from" not in params


def test_the_latest_day_is_read_from_the_tools_own_test_date(monkeypatch):
    seen = {}

    def newest(database, view, column="", **kw):
        seen.update(database=database, view=view, column=column)
        return DAY
    monkeypatch.setattr(freshness, "newest", newest)
    tool = _tool("fabric_bowing_summary")
    _, params = match_fixed_tool("Bowing test", TOOLS)
    cat = load_catalogs()["Production"]
    tools._resolve_latest(tool, params, cat, tools.tool_sql(tool, params), "", Scope.unrestricted())
    assert seen == {"database": "Production", "view": "rag.vw_FabricRoll", "column": "BowingTestDate"}
    assert (params["from"], params["to"]) == (DAY, DAY + timedelta(days=1))
    assert params["window"] == "on 29 Oct 2026" and params["window_note"] == LATEST_NOTE


def test_an_unreadable_latest_day_falls_back_to_today(monkeypatch):
    monkeypatch.setattr(freshness, "newest", lambda *a, **k: None)
    tool = _tool("fabric_bowing_summary")
    _, params = match_fixed_tool("Bowing test", TOOLS)
    tools._resolve_latest(tool, params, load_catalogs()["Production"], tools.tool_sql(tool, params), "",
                          Scope.unrestricted())
    assert params["from"] == date.today() and params["window"] == "today"


def test_a_buyer_binds_its_code_and_keeps_its_name_in_the_label(monkeypatch):
    from ragbot.data import values
    hit = values.ValueHit("ppc_buyer", "H&M", "C/09/7", "H&M", 1.0, "Production", "rag.vw_FabricReceipt.BuyerID")
    monkeypatch.setattr(values, "resolve", lambda q, scope, entity, **k: (hit, []) if entity == "ppc_buyer"
                        else (None, []))
    _, params = match_fixed_tool("bowing test report for H&M on 29 Oct 2026", TOOLS)
    assert params["buyer"] == "C/09/7" and params["buyer_label"] == "for H&M"


def test_a_file_reference_is_its_own_parameter():
    _, params = match_fixed_tool("bowing test of each roll of FR-26-2382", TOOLS)
    assert params["file"] == "FR-26-2382" and params["file_label"] == "for file FR-26-2382"
    _, params = match_fixed_tool("Bowing test", TOOLS)
    assert params["file"] == "" and params["file_label"] == ""


# ---------------------------------------------------------------- the data horizon
@pytest.mark.parametrize("sql,col,start,end,label", [
    ("SELECT TOP (200) * FROM rag.vw_GoodsReceipt WHERE GRNDate >= '2026-11-02' AND GRNDate < '2026-11-03'",
     "GRNDate", date(2026, 11, 2), date(2026, 11, 3), "on 2 Nov 2026"),
    ("SELECT COUNT(*) FROM rag.vw_FabricRoll WHERE BuyerID = 'C/09/7' AND BowingTestDate BETWEEN '2026-11-01' "
     "AND '2026-11-02'", "BowingTestDate", date(2026, 11, 1), date(2026, 11, 3), "from 1 Nov 2026 to 2 Nov 2026"),
    ("SELECT * FROM rag.vw_FabricReceipt WHERE ReceiveDate = '2026-11-02'", "ReceiveDate", date(2026, 11, 2),
     date(2026, 11, 3), "on 2 Nov 2026"),
    ("SELECT * FROM rag.vw_FabricReceipt WHERE '2026-11-01' <= ReceiveDate", "ReceiveDate", date(2026, 11, 1), None,
     "from 1 Nov 2026"),
])
def test_the_date_range_a_statement_filtered_on(sql, col, start, end, label):
    dw = freshness.date_window(sql, "tsql")
    assert dw is not None and (dw.column, dw.start, dw.end, dw.label) == (col, start, end, label)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM rag.vw_FabricRoll r JOIN rag.vw_FabricReceipt g ON g.ReceiveID = r.ReceiveID "
    "WHERE r.BowingTestDate >= '2026-11-02'",                                         # a join
    "SELECT * FROM rag.vw_FabricRoll WHERE BowingTestDate >= '2026-11-01' AND ShadeDate < '2026-11-02'",   # 2 columns
    "SELECT * FROM rag.vw_FabricRoll WHERE BowingTestDate < '2026-11-02'",          # no lower bound
    "SELECT * FROM rag.vw_FabricRoll WHERE BuyerID = 'C/09/7'",                      # no date at all
])
def test_no_date_range_is_read_where_there_is_none_to_report(sql):
    assert freshness.date_window(sql, "tsql") is None


def test_newest_reads_only_declared_columns_and_never_raises(monkeypatch):
    calls = []

    def fake_run(engine, sql_exec, params, **kw):
        calls.append((sql_exec, kw["tool"], kw["ttl"]))
        return ["Newest"], [["2026-10-29 18:34:01"]], None
    monkeypatch.setattr("ragbot.data.cache.cached_run", fake_run)
    s = Scope.unrestricted()
    assert freshness.newest("Production", "rag.vw_FabricRoll", "BowingTestDate", scope=s) == DAY
    assert "MAX([BowingTestDate])" in calls[0][0] and calls[0][1] == "freshness" and calls[0][2] == 3600
    assert freshness.newest("Production", "rag.vw_FabricRoll", "Shade", scope=s) is None        # not declared
    assert freshness.newest("Production", "rag.vw_PPMMeeting", scope=s) is None                 # declares none
    assert freshness.newest("NoSuchDb", "rag.vw_FabricRoll", scope=s) is None
    monkeypatch.setattr("ragbot.data.cache.cached_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert freshness.newest("Production", "rag.vw_FabricRoll", scope=s) is None


def _empty_tool(window="yesterday", horizon=DAY):
    return QueryResult(database="Production", engine="sqlserver", views=["rag.vw_FabricRoll"], sql="SELECT 1",
                       params={"window": window}, tool="fabric_bowing_summary", columns=["BuyerID"], rows=[],
                       horizon=horizon)


def test_an_empty_window_past_the_data_says_how_far_the_data_goes():
    s = not_found.empty([_empty_tool()])
    assert s == ("I checked Production (PPM & PPC) for yesterday and found no records; the newest record on this "
                 "server is from 29 Oct 2026, so ask about that day or earlier.")
    assert not_found.empty([_empty_tool(horizon=None)]).startswith("I checked Production (PPM & PPC) for yesterday "
                                                                    "and found no matching records")


def test_a_model_written_empty_window_is_reported_the_same_way():
    gen = QueryResult(database="Inventory", engine="sqlserver", views=["rag.vw_GoodsReceipt"], sql="SELECT 1",
                      tool="generated", columns=["GRNID"], rows=[], window="on 2 Nov 2026", checked=True,
                      horizon=date(2026, 10, 28))
    assert not_found.empty_window([gen]).startswith("I checked Inventory (store system) on 2 Nov 2026 and found no "
                                                    "records; the newest record on this server is from 28 Oct 2026")
    no_horizon = gen.model_copy(update={"horizon": None, "filters": {"StoreName": "Central"}})
    assert not_found.empty_window([no_horizon]) is None                       # empty_filter says more
    assert "on 2 Nov 2026" in not_found.empty_window([no_horizon], with_filters=True)


def test_the_chip_asks_the_same_question_for_the_newest_day():
    chips = not_found.suggestions("Show me yesterdays bowing test report", [_empty_tool()], tools=TOOLS,
                                  scope=Scope.unrestricted())
    assert chips[0] == "Show me bowing test report on 29 Oct 2026"
    assert "this week" not in " ".join(chips)                              # a wider period is empty too
    hit = match_fixed_tool(chips[0], TOOLS)
    assert hit[0]["name"] == "fabric_bowing_summary" and hit[1]["from"] == DAY


# ---------------------------------------------------------------- deterministic not-found sentences
@pytest.fixture
def wired(monkeypatch):
    logged = []
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: logged.append(a) or a)
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(tools, "needs_clarification", lambda q: None)
    return monkeypatch


def test_the_models_not_found_sentence_is_replaced_by_the_agents(wired):
    gave_up = QueryResult(database="Production", engine="sqlserver", views=[], sql="", columns=[], rows=[],
                          error="the generated SQL reads no view or table (the model found none)",
                          agent="fabric_quality")
    searched = []
    wired.setattr(orch, "_route", lambda q, user: "data")
    wired.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None, **k: [gave_up])
    wired.setattr(orch, "retrieve", lambda *a, **k: searched.append(1) or [])
    a = orch.answer("How was the blanket wash of yesterday?", scope=Scope.unrestricted())
    assert a.not_found and not searched                       # no document search for a PPC question
    assert a.text.startswith("System doesn't have the data. Fabric and trims test records I can read are the PPC")


def test_a_documents_not_found_ends_with_the_fixed_sentence_not_the_models(wired):
    from test_answer_stream import _chunk
    wired.setattr(orch, "_route", lambda q, user: "documents")
    wired.setattr(orch, "retrieve", lambda *a, **k: [_chunk()])
    chat = FakeChat(["System doesn't have the data. Testing records are maintained by the quality department."])
    wired.setattr(orch, "get_chat", lambda: chat)
    a = orch.answer("What is the bowing tolerance of the moon?", scope=Scope.unrestricted())
    assert a.not_found and "quality department" not in a.text
    assert a.text.startswith("System doesn't have the data. I couldn't find this in the company documents")
    assert any(w.startswith("not-found sentence replaced: Testing records") for w in a.warnings)


# ---------------------------------------------------------------- routing and the capability reply
@pytest.mark.parametrize("q", ["yarn count?", "Bowing test", "Show me yesterdays bowing test report",
                               "Goods received in PPC yesterday", "4-point inspection results"])
def test_ppc_test_questions_are_data_without_a_model_call(q, monkeypatch):
    monkeypatch.setattr(router, "get_chat", lambda: (_ for _ in ()).throw(AssertionError("model router called")),
                        raising=False)
    assert router.route(q) == "data"


def test_a_bowing_standard_in_a_document_is_still_a_document_question():
    assert router.route("What is the bowing tolerance in the H&M SOP?") == "documents"


@pytest.mark.parametrize("q", ["WHich modules and datas you have access?", "Which modules and data do you have?",
                               "what systems are you connected to?", "Which areas can you answer about?"])
def test_capability_questions_of_3_nov(q):
    hist = [{"role": "user", "text": "Hi"}, {"role": "assistant", "text": "Hello", "route": "chitchat"}]
    m = meta.classify(q, ConversationState.from_history(hist, "c1"))
    assert m is not None and m.kind == "capability"


def test_the_capability_reply_names_the_ppc_area_and_its_data_horizon(monkeypatch):
    from ragbot.agent import capabilities
    monkeypatch.setattr(freshness, "newest", lambda *a, **k: DAY)
    text, _chips = capabilities.describe("", Scope.unrestricted())
    assert "**Fabric & trims quality agent**" in text
    assert "Data on this server runs to 29 Oct 2026." in text
    assert "rag." not in text and "PPC.HeadCutting" not in text


@pytest.mark.parametrize("q,chip", [
    ("Show me yesterdays bowing test report", "Show me bowing test report on 29 Oct 2026"),
    ("Show me the bowing test report for 31 Dec 2030", "Show me the bowing test report on 29 Oct 2026"),
    ("bowing tests today?", "bowing tests on 29 Oct 2026?"),
    ("bowing tests", "bowing tests on 29 Oct 2026"),
])
def test_the_newest_day_chip_reads_naturally(q, chip):
    assert not_found._on_day(q, DAY) == chip
    assert match_fixed_tool(chip, TOOLS)[0]["name"] == "fabric_bowing_summary"
