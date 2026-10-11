"""Phase Q (Q3): a question about one named sewing line is answered from VistaQ's plan, with no model call.

Until 2026-10-11 the shared `unless` of every VistaQ tool skipped any question naming a line number, so
"What style is running on TAL line 2102?" went to the SQL model, which wrote `Line = 2102` against a text
column and failed. These tools take the line as a parameter instead.
"""
import pytest

from ragbot.data.tools import load_fixed_tools, match_fixed_tool, tool_sql

TOOLS = load_fixed_tools()


def _match(q):
    hit = match_fixed_tool(q, TOOLS)
    return (hit[0]["name"], hit[1]) if hit else (None, {})


@pytest.mark.parametrize("q,factory,line", [
    ("What style is running on TAL line 2102?", "TAL", "2102"),
    ("on TAL in line 2102, which style is running?", "TAL", "2102"),
    ("which buyer and style are running on line no. 2102 of TAL today", "TAL", "2102"),
    ("what is running on TAL L-2102 yesterday", "TAL", "2102"),
    ("which style is running on line 2102?", "", "2102"),
    ("what style is planned on tarasima line 2102 today", "TAL", "2102"),
])
def test_the_owners_question_reaches_the_running_style_tool(q, factory, line):
    name, params = _match(q)
    assert name == "running_style_by_line", q
    assert params["factory"] == factory and params["line"] == line


def test_the_running_style_answer_shows_only_what_the_owner_asked_for():
    tool = next(t for t in TOOLS if t["name"] == "running_style_by_line")
    select = tool_sql(tool).split("FROM")[0]
    assert "[Date]" in select and "Factory" in select and "Line" in select
    assert "Buyer" in select and "Style" in select
    for unwanted in ("SMV", "Manpower", "PlanEfficiency", "Combination", "OrderQty"):
        assert unwanted not in select, unwanted


def test_a_line_question_about_quality_reaches_the_quality_tool():
    name, params = _match("DHU of TAL line 2102 today")
    assert name == "quality_by_line_window" and params["line"] == "2102" and params["measures"] == ["dhu"]
    name, params = _match("Show me the defect summary for line 2322 today")
    assert name == "quality_by_line_window" and params["line"] == "2322" and params["measures"] == ["defects"]


def test_two_lines_or_a_qc_stage_still_go_to_the_model():
    assert _match("style running on lines 2102 and 2103")[0] is None
    assert _match("Which buyer had the most defects at Waist QC yesterday?")[0] != "quality_by_line_window"
    assert _match("day wise style of line 2102 this week")[0] is None


def test_lines_running_a_style_and_the_line_wise_plan():
    name, params = _match("Which lines are running style 71245 today?")
    assert name == "lines_running_style" and params["style"] == "71245"
    assert _match("Line wise style of today")[0] == "line_plan_by_factory_window"
    assert _match("line wise style of TAL today")[1]["factory"] == "TAL"


def test_the_production_summary_question_still_reaches_the_dashboard_tool():
    """"with style showing" must keep reaching the dashboard, which now names each line's styles."""
    name, params = _match("TAL line wise production summary of today with style showing")
    assert name == "production_by_line_window" and params["factory"] == "TAL"
    name, params = _match("all factory line wise production summary of today with style showing")
    assert name == "production_by_line_window" and params["factory"] == ""
    sql = tool_sql(next(t for t in TOOLS if t["name"] == "production_by_line_window"))
    assert "STRING_AGG" in sql and "st.Styles" in sql and "StyleCount" in sql


def test_the_vistaq_prototype_catalog_is_never_routed():
    from ragbot.data.catalog import load_catalogs
    from ragbot.data.db_router import evidence, pick_catalogs
    cats = load_catalogs()
    if "VISTAQ" not in cats:
        pytest.skip("the prototype catalog is not loaded here")
    assert cats["VISTAQ"].routable is False
    for q in ["which styles are in the production plans", "what style is running on TAL line 2102?"]:
        assert "VISTAQ" not in pick_catalogs(q, cats), q
        assert all(e.database != "VISTAQ" for e in evidence(q, cats)), q


def test_the_running_style_question_picks_vistaq_and_order_questions_still_pick_the_erp():
    from ragbot.data.catalog import load_catalogs
    from ragbot.data.db_router import evidence
    cats = load_catalogs()
    if "VistaQ_Default_RHL" not in cats or "BitopiSplint" not in cats:
        pytest.skip("the real catalogs are not loaded here")
    assert evidence("What style is running on TAL line 2102?", cats)[0].database == "VistaQ_Default_RHL"
    assert evidence("Which TAL orders have a PCD next week?", cats)[0].database == "BitopiSplint"


def test_a_question_that_also_asks_for_a_figure_goes_to_the_model():
    """The plan tools return the style and its buyer. A question that also wants output, efficiency or a
    quality rate is answered by the model over the same views, not half-answered here."""
    for q in ["which styles ran on TAL line 2102 last week and how many pieces were output",
              "what style is running on line 2102 and its efficiency today",
              "which lines are running style 71245 and their output today"]:
        name, _ = _match(q)
        assert name not in ("running_style_by_line", "lines_running_style"), (q, name)
