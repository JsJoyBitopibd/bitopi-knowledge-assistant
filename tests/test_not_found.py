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


def test_empty_filter_sentence_uses_the_users_words_and_candidates_become_chips():
    """P2: a model-written statement with filters found nothing after the lookups: the user's literals in the
    sentence, never a column, a table or a recorded value; the recorded values are offered as chips."""
    gen = QueryResult(database="Inventory", engine="sqlserver", views=["dbo.Balance"], tool="generated", sql="SELECT 1",
                      columns=["Balance"], rows=[[None]], checked=True,
                      filters={"ItemCode": "1178108", "CompanyID": "02", "SLolcation": "mantainance store"},
                      candidates=["Central Store (Misami)", "Maintenance Store (Misami)"])
    s = not_found.empty_filter([gen])
    assert s.startswith("I checked Inventory (store system) for '1178108', '02', 'mantainance store' and found no matching records")
    assert "SLolcation" not in s and "dbo." not in s and "Maintenance Store" not in s
    assert not_found.empty_filter([gen.model_copy(update={"checked": False})]) is None     # nothing was looked up
    assert not_found.empty_filter([gen.model_copy(update={"rows": [[5]]})]) is None        # it has an answer
    chips = not_found.suggestions("balance of 1178108 in the mantainance store of misami", [gen])
    assert chips[:2] == ["balance of 1178108 in the Central Store (Misami) of misami",
                         "balance of 1178108 in the Maintenance Store (Misami) of misami"]


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
              "QC summary day wise this week", "Daily DHU this month", "Show me the defect summary per day"]:
        hit = tools.match_fixed_tool(q, ts)
        assert hit is None or hit[0]["name"] not in vistaq, (q, hit and hit[0]["name"])
    # factories, periods (months and weekdays too) and grouping words are not qualifiers
    for q in ["What is the reject rate of RHL this week?", "Which styles had the most defects in TAL today?",
              "QC summary for Today", "stage wise QC for BGL yesterday", "Show the QC results by stage today",
              "QC summary for Monday", "QC summary for October"]:
        hit = tools.match_fixed_tool(q, ts)
        assert hit is not None and hit[0]["name"] in vistaq, q


def test_older_line_questions_still_reach_their_tools():
    ts = tools.load_fixed_tools()
    assert tools.match_fixed_tool("Which lines missed their target?", ts)[0]["name"] == "lines_below_target"
    assert tools.match_fixed_tool("Which RHL lines are below target?", ts)[0]["name"] == "lines_below_target_by_factory"


# ---------------------------------------------------------------- written dates (owner's screenshots, 8 Oct 2026)
_THU = date(2026, 10, 8)       # a Thursday


@pytest.mark.parametrize("text, start, end, label", [
    ("6-10-2026", date(2026, 10, 6), date(2026, 10, 7), "on 6 Oct 2026"),          # day first, as in Bangladesh
    ("on 06/10/2026", date(2026, 10, 6), date(2026, 10, 7), "on 6 Oct 2026"),
    ("2026-10-06", date(2026, 10, 6), date(2026, 10, 7), "on 6 Oct 2026"),
    ("06.10.26", date(2026, 10, 6), date(2026, 10, 7), "on 6 Oct 2026"),
    ("7th october", date(2026, 10, 7), date(2026, 10, 8), "on 7 Oct 2026"),
    ("7 of oct, 2026", date(2026, 10, 7), date(2026, 10, 8), "on 7 Oct 2026"),
    ("october 7", date(2026, 10, 7), date(2026, 10, 8), "on 7 Oct 2026"),
    ("25 december", date(2025, 12, 25), date(2025, 12, 26), "on 25 Dec 2025"),     # no year: the latest past one
    ("from 1 oct to 5 oct", date(2026, 10, 1), date(2026, 10, 6), "from 1 Oct 2026 to 5 Oct 2026"),
    ("between 28 sep and 2 oct 2026", date(2026, 9, 28), date(2026, 10, 3), "from 28 Sep 2026 to 2 Oct 2026"),
    ("1-10-2026 to 5-10-2026", date(2026, 10, 1), date(2026, 10, 6), "from 1 Oct 2026 to 5 Oct 2026"),
    ("in september", date(2026, 9, 1), date(2026, 10, 1), "in September 2026"),
    ("for october", date(2026, 10, 1), date(2026, 11, 1), "in October 2026"),
    ("in december", date(2025, 12, 1), date(2026, 1, 1), "in December 2025"),
    ("september 2026", date(2026, 9, 1), date(2026, 10, 1), "in September 2026"),
    ("monday", date(2026, 10, 5), date(2026, 10, 6), "on 5 Oct 2026"),
    ("thursday", date(2026, 10, 8), date(2026, 10, 9), "on 8 Oct 2026"),
    ("last thursday", date(2026, 10, 1), date(2026, 10, 2), "on 1 Oct 2026"),
])
def test_written_periods(text, start, end, label):
    assert tools._explicit_window(text, _THU) == (start, end, label)


def test_relative_words_are_not_written_periods_and_bad_dates_raise():
    for t in ("today", "this week", "last 7 days", "last month"):
        assert tools._explicit_window(t, _THU) is None
    for t in ("31-02-2026", "from 5 oct to 1 oct"):
        with pytest.raises(ValueError):
            tools._explicit_window(t, _THU)


def test_period_grammar_finds_the_date_inside_a_question():
    import re
    rx = re.compile(rf"\b({tools.PERIOD})\b", re.I)
    assert rx.search("show me line & factory wise DHU of 6-10-2026")[1] == "6-10-2026"
    assert rx.search("show me line & factory wise DHU of 7th october.")[1] == "7th october"
    assert rx.search("QC summary for october 7")[1] == "october 7"             # not the whole month
    assert rx.search("DHU from 1 Oct to 5 Oct please")[1] == "from 1 Oct to 5 Oct"
    assert rx.search("May I see the QC summary?") is None                       # "May" is not a month here


def _expect_day(month: int, day: int) -> date:
    today = date.today()
    d = date(today.year, month, day)
    return d if d <= today else date(today.year - 1, month, day)


def test_the_screenshot_questions_use_their_dates():
    ts = tools.load_fixed_tools()
    # grouped questions get exactly the columns they name (quality_by_group_window, owner's rule of 8 Oct)
    tool, p = tools.match_fixed_tool("show me line & factory wise DHU of 6-10-2026", ts)
    assert tool["name"] == "quality_by_group_window" and p["from"] == date(2026, 10, 6) and p["window"] == "on 6 Oct 2026"
    assert (p["dims"], p["measures"]) == (["Line", "Factory"], ["dhu"])
    tool, p = tools.match_fixed_tool("show me line & factory wise DHU of 7th october.", ts)
    assert tool["name"] == "quality_by_group_window" and p["from"] == _expect_day(10, 7)
    tool, p = tools.match_fixed_tool("Show me the defect summary for today broken down by line.", ts)
    assert tool["name"] == "quality_by_group_window" and p["window"] == "today"
    assert (p["dims"], p["measures"]) == (["Factory", "Line"], ["defects"])
    tool, p = tools.match_fixed_tool("Show date beside the data for factory and line-wise DHU today", ts)
    assert tool["name"] == "quality_by_group_window" and p["dims"] == ["Factory", "Line"]
    assert tools.match_fixed_tool("Which RHL lines had the most defects today?", ts)[0]["name"] == "dhu_by_line_by_factory"


def test_a_date_we_cannot_read_goes_to_the_model():
    assert tools.match_fixed_tool("QC summary on 31-02-2026", tools.load_fixed_tools()) is None


def test_follow_ups_with_a_date_label_reach_their_tool_for_the_same_day():
    from ragbot.agent.templated import follow_ups
    ts = tools.load_fixed_tools()
    tool, p = tools.match_fixed_tool("Show me the defect summary on 6-10-2026", ts)
    assert tool["name"] == "defect_summary_window"
    r = QueryResult(database=tool["database"], engine="sqlserver", views=[], sql="", params=p, tool=tool["name"],
                    columns=["Defect"], rows=[["x"]])
    for q in follow_ups(r, tool):
        hit = tools.match_fixed_tool(q, ts)
        assert hit is not None and hit[1]["from"] == date(2026, 10, 6), q
    tool, p = tools.match_fixed_tool("Show me the defect summary on 6 Oct 2026 line wise", ts)
    assert tool["name"] == "quality_by_group_window" and p["from"] == date(2026, 10, 6)
    _, p = tools.match_fixed_tool("Show me the QC summary in October 2026", ts)
    assert (p["from"], p["to"], p["window"]) == (date(2026, 10, 1), date(2026, 11, 1), "in October 2026")


def test_every_grouped_vistaq_answer_shows_its_date():
    for t in tools.load_fixed_tools():
        if t["database"] == "VistaQ_Default_RHL" and t["name"] != "corrective_actions_window":
            assert "AS [Date]" in t["sql"] and t["sql"].index("AS [Date]") < t["sql"].index("FROM"), t["name"]
        if t["database"] == "VistaQ_Default_RHL":
            assert " for {window}" not in t.get("answer_list", ""), t["name"]


def test_empty_sentence_for_a_written_date():
    assert not_found.empty([_empty(window="on 6 Oct 2026")]).startswith(
        "I checked VistaQ on 6 Oct 2026 and found no matching records")


def test_a_lookup_that_found_nothing_is_not_reported_as_the_users_query():
    """P2 runs DISTINCT lookups to repair a blank result; an empty lookup is evidence about the data, not the
    question the user asked, so it must not produce "I checked <system> and found no matching records"."""
    lookup = QueryResult(database="PlanningTNA", engine="sqlserver", views=["APP.vw_X"], tool="lookup",
                         sql="SELECT DISTINCT TOP (20) [FactoryID] FROM APP.vw_X", columns=["FactoryID"], rows=[])
    generated = QueryResult(database="PlanningTNA", engine="sqlserver", views=["APP.vw_X"], tool="generated",
                            sql="SELECT 1", columns=["StyleName"], rows=[], filters={"FactoryID": "TAL"},
                            checked=True)
    assert not_found.empty([generated, lookup]) is None
    assert not_found.empty_filter([generated, lookup]).startswith("I checked PlanningTNA for 'TAL'")
