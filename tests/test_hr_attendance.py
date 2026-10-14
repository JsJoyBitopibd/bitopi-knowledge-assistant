"""Phase R: "what is the number of manpower present today?" answered honestly.

Three separate defects stood between the question and an answer, and each has a test here.

1. The router sent it to VistaQ's PLANNED line manpower, which is a plan and not who came to work
   (fixed by config/agents/hr.yaml, covered in tests/test_route_keywords.py).
2. DayStatus is a column of CODES, so the model's `DayStatus = 'present'` matched no row. The fixed
   tool spells the codes out.
3. Attendance stopped on 2026-09-09 and covers one instance's 992 employees, not the group's 17,972
   (config/data_quality.yaml watches both). So the tool answers for the newest day the table holds,
   returns that date as a column, and its lead says both things.

Plus the reference-card fix the tool exposed: run_fixed_tool reported only `rag.*` views, so a tool
over a discovered table produced a [D#] card with no table name at all, against docs/REFERENCE_FORMAT.md.
"""
import re

import pytest
import yaml

from ragbot.config import ROOT
from ragbot.data.tools import load_fixed_tools, match_fixed_tool, tool_sql

TOOLS = load_fixed_tools()
TOOL = next(t for t in TOOLS if t["name"] == "manpower_present_latest")


def _match(q):
    hit = match_fixed_tool(q, TOOLS)
    return (hit[0]["name"], hit[1]) if hit else (None, {})


@pytest.mark.parametrize("q", [
    "what is the number of manpower present today?",
    "How many manpower are present today?",
    "how many are present?",
    "number of employees present",
    "manpower present",
    "staff attendance today",
    "how many absent?",
    "what is today's attendance",
])
def test_the_owners_question_reaches_the_presence_tool(q):
    assert _match(q)[0] == "manpower_present_latest", q


@pytest.mark.parametrize("q", [
    "how many were present on 2026-08-15?",             # a named day: the model writes that WHERE
    "how many were present in August?",
    "how many present last month?",
    "how many operators are present on TAL line 2102?",  # a line: that is the production plan
    "what is the planned manpower for RHL?",
    "how many are present in the ERP & IT department?",  # a breakdown this single row does not give
    "how many present per department?",
    "is employee id 250109595 present?",                 # one person, not the day's count
])
def test_a_question_the_single_row_cannot_answer_is_left_to_the_model(q):
    assert _match(q)[0] != "manpower_present_latest", q


def test_presence_is_counted_from_the_codes_not_the_word_present():
    """DayStatus is varchar(10) holding P, L, PW, PH, LW, LH, A, LV, MLV, W, H — never the word."""
    sql = tool_sql(TOOL)
    assert "'P', 'L', 'PW', 'PH', 'LW', 'LH'" in sql, "the present codes, including the late ones"
    assert "'LV', 'MLV'" in sql and "'W', 'H'" in sql
    assert "'present'" not in sql.lower().replace("'p'", "")


def test_the_answer_is_for_the_newest_day_in_the_table_and_shows_that_date():
    sql = tool_sql(TOOL)
    assert "MAX(WorkDate)" in sql, "attendance stopped on 2026-09-09; never assume today"
    assert re.search(r"AS\s+WorkDate", sql), "the date is a column, so the reader and the verifier see it"
    assert "AS OnRoll" in sql, "the population the count is out of"


def test_the_lead_says_it_is_not_today_and_not_the_group():
    lead = TOOL["answer_list"]
    assert "Nothing is loaded for today" in lead and "not the whole group" in lead
    # the citation check compares every number in the answer with the sources: a date in the lead
    # would have to appear in a row, so the lead carries no digits at all
    assert not any(c.isdigit() for c in lead)


def test_a_fixed_tool_over_a_discovered_table_names_it_on_the_reference_card():
    """rewrite_virtual reports only rag.* views; _views_in finds the table. Without the second line the
    [D#] card read "Hrms5Misami (sqlserver) · " with no object name."""
    from ragbot.data.catalog import load_catalogs
    from ragbot.data.tools import _views_in
    cat = load_catalogs()["Hrms5Misami"]
    assert _views_in(tool_sql(TOOL), cat) == ["dbo.DaywisePayHour"]
    assert cat.table("dbo.DaywisePayHour") is not None, "opened by sensitive_patterns: the name contains 'pay'"


def test_the_hr_quality_checks_watch_every_attendance_source():
    """Each source was checked live on 2026-10-13 and each had stopped; the checks keep saying so."""
    checks = yaml.safe_load((ROOT / "config" / "data_quality.yaml").read_text(encoding="utf-8"))["checks"]
    hr = {c["name"]: c for c in checks if c.get("agent") == "hr"}
    assert {"hrms_attendance_fresh", "hrms_punches_fresh", "hwatt_punches_fresh",
            "hrms_overtime_fresh"} <= set(hr)
    assert hr["hrms_attendance_fresh"]["sql"].strip().endswith("dbo.DaywisePayHour")
    # the code list the presence tool counts from is also asserted against the live data
    unknown = hr["hrms_unknown_day_status"]["sql"]
    for code in ("'P'", "'L'", "'PW'", "'PH'", "'LW'", "'LH'", "'A'", "'LV'", "'MLV'", "'W'", "'H'"):
        assert code in unknown, code
    # a total from the overtime table is six people, and a headcount must not come from tblEmployee
    assert "hrms_overtime_barely_recorded" in hr and "hrms_attendance_covers_the_group" in hr


def test_the_charter_separates_the_two_employee_populations():
    """tblEmployee has 992 active, the group list 17,972: answering headcount from the first is wrong."""
    charter = (ROOT / "prompts" / "agents" / "hr.txt").read_text(encoding="utf-8")
    assert "vwAllCompanyEmployeeList" in charter and "17,972" in charter
    head = next(ln for ln in charter.splitlines() if ln.startswith("- Headcount"))
    assert "vwAllCompanyEmployeeList" in head and "tblEmployee" not in head
    assert "DayStatus = 'present'" in charter, "the mistake the model actually made, named"
