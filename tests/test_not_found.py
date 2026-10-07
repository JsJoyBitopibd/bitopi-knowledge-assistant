"""agent/not_found.py: the plain sentence after "System doesn't have the data." and the questions offered instead;
plus the VistaQ question set (config/fixed_tools.yaml) reaching its tools with the right period and factory."""
from datetime import date, timedelta

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.data.tools as tools
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent import not_found
from ragbot.auth.models import Scope
from ragbot.models import QueryResult


def _empty(tool="qc_summary_window", window="today", database="VistaQ_Default_RHL"):
    params = {"window": window} if window else {}
    return QueryResult(database=database, engine="sqlserver", views=["rag.vw_LineDayQuality"], sql="SELECT 1",
                       params=params, tool=tool, columns=["Factory"], rows=[])


# ---------------------------------------------------------------- sentences
def test_empty_fixed_tool_names_the_system_and_the_period():
    s = not_found.empty([_empty()])
    assert s.startswith("I checked VistaQ for today and found no matching records")
    assert "rag." not in s and "[D" not in s


def test_empty_period_reads_naturally_and_unknown_database_keeps_its_name():
    assert "for the last 7 days" in not_found.empty([_empty(window="last 7 days")])
    s = not_found.empty([_empty(window="", database="NoSuchDb")])
    assert s.startswith("I checked NoSuchDb and found no matching records")


def test_generated_or_failed_queries_are_not_called_empty():
    gen = _empty(tool="generated")
    failed = _empty().model_copy(update={"error": "timeout"})
    assert not_found.empty([gen]) is None and not_found.empty([failed]) is None and not_found.empty([]) is None


def test_general_uses_the_agents_where_sentence_else_the_default():
    assert not_found.general("What was the AQL result of the last final audit?").startswith("Quality data I can read")
    assert not_found.general("What is the colour of the sky?").startswith("I couldn't find this")


# ---------------------------------------------------------------- suggestions
def test_empty_today_offers_yesterday_and_this_week_first():
    out = not_found.suggestions("Show me the defect summary today", [_empty(tool="defect_summary_window")])
    assert out[:2] == ["Show me the defect summary yesterday", "Show me the defect summary this week"]
    ts = tools.load_fixed_tools()
    for q in out[:2]:                       # each one answers without a model call
        assert tools.match_fixed_tool(q, ts)[0]["name"] == "defect_summary_window"


def test_a_question_without_a_period_gets_one_appended():
    out = not_found.suggestions("What is the DHU?", [_empty()])
    assert out[0] == "What is the DHU yesterday?"


def test_related_examples_never_name_another_factory():
    for q in ["Which lines had defects?", "What was the AQL of the RHL audit?"]:
        out = not_found.suggestions(q)
        assert out, q
        assert not any(c in s for s in out for c in ("TAL", "BGL")), (q, out)


def test_examples_about_one_record_are_not_offered():
    out = not_found.suggestions("Who signed the buyer's final inspection report for the last shipment?")
    assert not any(any(ch.isdigit() for ch in s) for s in out), out


def test_unrelated_question_gets_no_suggestions():
    assert not_found.suggestions("What is the colour of the sky?") == []


# ---------------------------------------------------------------- orchestrator
@pytest.fixture
def no_rows(monkeypatch):
    logged = []
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: logged.append(a) or a)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "_route", lambda q, user: "data")
    monkeypatch.setattr(orch, "retrieve", lambda *a, **k: [])
    monkeypatch.setattr(tools, "needs_clarification", lambda q: None)
    monkeypatch.setattr(tools, "answer_from_data",
                        lambda q, user="", refresh=False, scope=None: [_empty(tool="defect_summary_window")])
    return logged


def test_empty_period_reply_is_plain_and_offers_wider_periods(no_rows):
    a = orch.answer("Show me the defect summary today", scope=Scope.unrestricted())
    assert a.not_found
    assert a.text.startswith("System doesn't have the data. I checked VistaQ for today")
    assert "view" not in a.text and "rag." not in a.text
    assert a.follow_ups[:2] == ["Show me the defect summary yesterday", "Show me the defect summary this week"]


# ---------------------------------------------------------------- the VistaQ question set
def test_vistaq_questions_reach_their_tools_with_period_and_factory():
    ts = tools.load_fixed_tools()
    today = date.today()
    expect = {
        "defect summary toady": ("defect_summary_window", "today", None),
        "What is the DHU today?": ("qc_summary_window", "today", None),
        "DHU of TAL yesterday": ("qc_summary_by_factory_window", "yesterday", "TAL"),
        "QC summary last week": ("qc_summary_window", "last week", None),
        "Which RHL lines had the most defects today?": ("dhu_by_line_by_factory", "today", "RHL"),
        "Which lines have the highest DHU?": ("dhu_by_line", "today", None),
        "Which TAL lines are above the DHU target this week?": ("lines_above_dhu_target_window", "this week", "TAL"),
        "Top defects in BGL this week": ("defect_summary_by_factory_window", "this week", "BGL"),
        "Show the TAL line output yesterday": ("endline_output_by_line_by_factory_window", "yesterday", "TAL"),
        "What is the reject rate of RHL this week?": ("rejects_window", "this week", "RHL"),
        "Which buyers had the most defects this month?": ("defects_by_buyer_window", "this month", ""),
        "stage wise QC for BGL yesterday": ("qc_by_stage_window", "yesterday", "BGL"),
        "How many corrective actions were raised this week?": ("corrective_actions_count_window", "this week", ""),
        "Show RHL CAPA last week": ("corrective_actions_window", "last week", "RHL"),
        "How many pieces were cut in TAL yesterday?": ("cutting_output_window", "yesterday", "TAL"),
    }
    for q, (name, window, factory) in expect.items():
        hit = tools.match_fixed_tool(q, ts)
        assert hit is not None and hit[0]["name"] == name, (q, hit and hit[0]["name"])
        params = hit[1]
        assert params["window"] == window, q
        if factory is not None:
            assert params["factory"] == factory, q
    _, p = tools.match_fixed_tool("defect summary toady", ts)
    assert (p["from"], p["to"]) == (today, today + timedelta(days=1))


def test_a_qualifier_the_tool_cannot_apply_goes_to_the_model():
    """Found in the app 2026-10-07: "Which buyer had the most defects at Waist QC yesterday?" was answered by
    defects_by_buyer_window over every QC stage. A stage, line, unit, PO, style or named buyer skips the tools."""
    ts = tools.load_fixed_tools()
    vistaq = {t["name"] for t in ts if t["database"] == "VistaQ_Default_RHL"}
    for q in ["Which buyer had the most defects at Waist QC yesterday?",
              "Show me the defect summary for line 2322 today",
              "Which lines have the highest DHU in Unit 03?",
              "Show the defect summary for H&M this week",
              "How many pieces were rejected at Output QC today?",
              "Show me the QC summary for PO 594520-9192",
              "QC summary for Monday"]:
        hit = tools.match_fixed_tool(q, ts)
        assert hit is None or hit[0]["name"] not in vistaq, (q, hit and hit[0]["name"])
    # factories, periods and grouping words are not qualifiers
    for q in ["What is the reject rate of RHL this week?", "Which styles had the most defects in TAL today?",
              "QC summary for Today", "stage wise QC for BGL yesterday", "Show the QC results by stage today"]:
        hit = tools.match_fixed_tool(q, ts)
        assert hit is not None and hit[0]["name"] in vistaq, q


def test_older_line_questions_still_reach_their_tools():
    ts = tools.load_fixed_tools()
    assert tools.match_fixed_tool("Which lines missed their target?", ts)[0]["name"] == "lines_below_target"
    assert tools.match_fixed_tool("Which RHL lines are below target?", ts)[0]["name"] == "lines_below_target_by_factory"
