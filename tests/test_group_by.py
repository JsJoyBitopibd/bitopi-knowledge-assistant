"""Grouped VistaQ answers show what was asked, nothing else (owner, 8 Oct 2026): groupings and figures read from the
question (tools._group_by / _measures, rendered by tool_sql), PO/EO answered per file with the ERP's export orders as
a companion result [D2], and the production dashboard reached by the pasted card list."""
from datetime import date

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.data.tools as tools
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.auth.models import Scope
from ragbot.models import QueryResult


@pytest.fixture(scope="module")
def ts():
    return tools.load_fixed_tools()


def _hit(q, ts):
    hit = tools.match_fixed_tool(q, ts)
    assert hit is not None, q
    return hit


def _select_list(tool, params):
    """The rendered SELECT list after the [Date] column, as written (one line per tool here)."""
    sql = tools.tool_sql(tool, params)
    return sql.split("AS [Date],", 1)[1].split("FROM", 1)[0].strip()


# ---------------------------------------------------------------- exactly the columns asked
def test_the_screenshot_question_gets_exactly_unit_line_buyer_dhu(ts):
    tool, p = _hit("show me Unit, Line and Buyer wise DHU of TAL", ts)
    assert tool["name"] == "quality_by_group_window"
    assert p["factory"] == "TAL" and p["dims"] == ["Unit", "Line", "Buyer"] and p["measures"] == ["dhu"]
    cols = _select_list(tool, p)
    assert cols.startswith("Unit, Line, Buyer, CAST(100.0 * SUM(Defects)") and cols.endswith("AS DHU")
    assert "Factory" not in cols and "Checked" not in cols.split("NULLIF")[0]
    sql = tools.tool_sql(tool, p)
    assert "GROUP BY Unit, Line, Buyer" in sql and "ORDER BY Unit, Line, Buyer" in sql
    assert p["by"] == "unit, line and buyer" and p["note"] == ""


@pytest.mark.parametrize("q, dims, measures", [
    ("buyer wise QC yesterday", ["Factory", "Buyer"], ["checked", "defects", "dhu"]),        # no figure named
    ("line wise FTP and reject rate of RHL", ["Line"], ["ftp_pct", "reject_pct"]),
    ("defects by style and colour yesterday", ["Factory", "Style", "Color"], ["defects"]),
    ("show me DHU by Unit, Line and Buyer of TAL", ["Unit", "Line", "Buyer"], ["dhu"]),
    ("line wise DHU, FTP, defective rate and reject rate of RHL", ["Line"], ["dhu", "ftp_pct", "defective_pct", "reject_pct"]),
    ("buyer wise production yesterday", ["Factory", "Buyer"], ["output"]),
    ("style wise rejected and rectified qty this week", ["Factory", "Style"], ["rejected", "rectified"]),
])
def test_groupings_and_figures_come_from_the_question(ts, q, dims, measures):
    tool, p = _hit(q, ts)
    assert tool["name"] == "quality_by_group_window", q
    assert (p["dims"], p["measures"]) == (dims, measures), q


def test_po_and_eo_mean_the_file_with_a_note(ts):
    for q in ("PO wise DHU of TAL yesterday", "EO wise defects this week", "DHU by export order today"):
        tool, p = _hit(q, ts)
        assert tool["name"] == "quality_by_group_window", q
        assert "FileNo" in p["dims"] and "VistaQ records QC per file" in p["note"], q
        assert tool["companion"]["column"] == "FileNo" and tool["companion"]["database"] == "BitopiSplint"


def test_nothing_from_the_question_reaches_the_sql(ts):
    tool, p = _hit("show me DHU by buyer; DROP TABLE x wise", ts)
    sql = tools.tool_sql(tool, p)
    assert "DROP" not in sql and "TABLE x" not in sql
    # a value outside the whitelist is dropped even if it got into the params
    sql = tools.tool_sql(tool, {**p, "dims": ["Buyer", "1; DELETE FROM y"], "measures": ["dhu", "evil"]})
    assert "DELETE" not in sql and "evil" not in sql and "Buyer, CAST" in sql


def test_without_a_dimension_word_the_tool_does_not_match(ts):
    """'by stage' names no dimension the tool has: the stage tool answers (Factory is not added to an empty list)."""
    assert _hit("Show the QC results by stage today", ts)[0]["name"] == "qc_by_stage_window"
    assert _hit("stage wise QC for BGL yesterday", ts)[0]["name"] == "qc_by_stage_window"


def test_ranking_questions_keep_their_tools(ts):
    assert _hit("Which lines have the highest DHU?", ts)[0]["name"] == "dhu_by_line"
    assert _hit("Which TAL lines have the highest DHU yesterday?", ts)[0]["name"] == "dhu_by_line_by_factory"
    assert _hit("Which buyers had the most defects this week?", ts)[0]["name"] == "defects_by_buyer_window"
    assert _hit("Which lines are above the DHU target today?", ts)[0]["name"] == "lines_above_dhu_target_window"
    assert _hit("Show me the QC summary today", ts)[0]["name"] == "qc_summary_window"


# ---------------------------------------------------------------- EO and file
def test_an_eo_or_file_reaches_its_files_qc(ts):
    tool, p = _hit("Show the DHU of EO TAL-26-1985-1 this week", ts)
    assert tool["name"] == "quality_by_eo_window" and p["eo_file"] == "26-1985" and p["measures"] == ["dhu"]
    assert "FileNo = 'FR-' + @eo_file" in tools.tool_sql(tool, p)
    tool, p = _hit("Show the DHU of file FR-26-1985 this week", ts)
    assert tool["name"] == "quality_by_eo_window" and p["eo_file"] == "26-1985"


def test_a_files_orders_come_from_the_erp(ts):
    tool, p = _hit("Which export orders are in file FR-26-1985?", ts)
    assert tool["name"] == "orders_by_file_id" and p["file"] == "FR-26-1985" and tool["database"] == "BitopiSplint"


# ---------------------------------------------------------------- companion
def test_list_params_are_bind_parameters():
    sql, params = tools._expand_list("SELECT 1 FROM v WHERE FileRefID IN (@files)", "files", ["FR-1", "x'; --"])
    assert sql == "SELECT 1 FROM v WHERE FileRefID IN (@files_0, @files_1)"
    assert params == {"files_0": "FR-1", "files_1": "x'; --"}


def test_the_companion_runs_only_when_its_column_has_values(ts):
    tool = next(t for t in ts if t["name"] == "quality_by_group_window")
    no_col = QueryResult(database="VistaQ_Default_RHL", engine="sqlserver", views=[], sql="", tool=tool["name"],
                         columns=["Date", "Buyer", "DHU"], rows=[["2026-10-07", "H&M", 3.1]])
    empty = no_col.model_copy(update={"columns": ["Date", "FileNo", "DHU"], "rows": [["2026-10-07", None, 3.1]]})
    for r in (no_col, empty, no_col.model_copy(update={"rows": []})):
        assert tools.run_companion(tool, r, {"BitopiSplint": object()}, scope=Scope.unrestricted()) is None


@pytest.fixture
def two_sources(monkeypatch):
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: a)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "_route", lambda q, user: "data")
    monkeypatch.setattr(tools, "needs_clarification", lambda q: None)
    params = {"from": date(2026, 10, 7), "to": date(2026, 10, 8), "window": "yesterday", "factory": "TAL",
              "dims": ["FileNo"], "measures": ["dhu"], "by": "file",
              "note": ". VistaQ records QC per file (FR number), not per PO or export order; each file's export "
                      "orders and POs from the ERP follow"}
    main = QueryResult(database="VistaQ_Default_RHL", engine="sqlserver", views=["rag.vw_PlanQualityDay"],
                       sql="SELECT …", params=params, tool="quality_by_group_window",
                       columns=["Date", "FileNo", "DHU"], rows=[["2026-10-07", "FR-26-1985", 21.45]])
    comp = QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], sql="SELECT …",
                       params={"files_0": "FR-26-1985"}, tool="quality_by_group_window:companion",
                       columns=["FileNo", "Buyer", "ExportOrders", "POs", "FirstEO", "LastEO", "OrderQty"],
                       rows=[["FR-26-1985", "CARTER'S", 17, 15, "TAL-26-1985-1", "TAL-26-1985-17", 52218]])
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [main, comp])


def test_file_qc_and_its_erp_orders_are_one_templated_answer(two_sources):
    a = orch.answer("PO wise DHU of TAL yesterday", scope=Scope.unrestricted())
    assert not a.not_found and "templated answer (no model call)" in a.warnings
    assert "[D1]" in a.text and "[D2]" in a.text and "VistaQ records QC per file" in a.text
    assert a.text.index("[D1]") < a.text.index("[D2]")
    assert [r.marker for r in a.references] == ["D1", "D2"] and len(a.results) == 2
    assert a.references[1].database == "BitopiSplint"


# ---------------------------------------------------------------- production dashboard
PASTED = """No. of Working Styles

Line:

icon
Planned Production


RTT


Sewing Production

Variance
vs RTT


WIP



Efficiency


AVRG SMV

Man Hours


Plan:

SAH



FTP
Qty:


Defective Rate
Qty:


Rectified Rate
Qty


Reject Rate
Qty:


Overall DHU


Defects:



show me these data of yesterday"""


def test_the_pasted_dashboard_card_list_reaches_the_dashboard(ts):
    tool, p = _hit(PASTED, ts)
    assert tool["name"] == "production_dashboard_window" and p["window"] == "yesterday" and p["factory"] == ""


def test_dashboard_by_factory_and_by_line(ts):
    tool, p = _hit("Show the TAL production dashboard yesterday", ts)
    assert tool["name"] == "production_dashboard_window" and p["factory"] == "TAL"
    assert _hit("Show the production dashboard yesterday line wise", ts)[0]["name"] == "production_by_line_window"
    assert _hit("What is the efficiency today?", ts)[0]["name"] == "production_dashboard_window"
    # a grouping the dashboard cannot apply goes to model-written SQL
    assert tools.match_fixed_tool("efficiency by buyer yesterday", ts) is None
