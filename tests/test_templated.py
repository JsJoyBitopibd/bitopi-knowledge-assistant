"""C7: zero-LLM templated answers for fixed-tool results."""
from datetime import date, datetime
from decimal import Decimal

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.data.tools as tools
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent.citations import verify
from ragbot.agent.templated import fmt, try_template
from ragbot.models import QueryResult
from ragbot.auth.models import Scope

from test_answer_stream import FakeChat

NF = "System doesn't have the data."
WINDOW = {"factory": "TAL", "from": date(2026, 9, 28), "to": date(2026, 10, 5), "window": "next week"}


def _qr(cols, rows, tool="ppm_meetings_count_by_factory_window", params=None):
    return QueryResult(database="Production", engine="sqlserver", views=["rag.vw_PPMMeeting"], sql="SELECT 1",
                       tool=tool, params=dict(WINDOW if params is None else params), columns=cols, rows=rows)


def test_count_of_zero_is_an_answer_not_a_miss():
    r = _qr(["Meetings"], [[0]])
    text = try_template(r, {"answer": "{factory} has {value} PPM meeting(s) {window}"})
    assert text == "TAL has 0 PPM meeting(s) next week [D1]."
    assert verify(text, [], [r], NF)[0]


def test_generic_scalar_without_template():
    assert try_template(_qr(["Orders"], [[12]]), None) == "Orders: 12 [D1]."


def test_list_becomes_a_verified_table():
    rows = [["TAL-25-1493-215", "H&M", datetime(2026, 9, 28), Decimal("1200.00")],
            ["TAL-25-1493-216", "H&M", datetime(2026, 9, 29, 14, 30), None]]
    r = _qr(["ExportOrderID", "Buyer", "PCD", "OrderQty"], rows, tool="orders_by_factory_pcd_window")
    text = try_template(r, {"answer_list": "{n} {factory} order(s) have a PCD {window}"})
    assert text.startswith("2 TAL order(s) have a PCD next week [D1]:")
    assert "| TAL-25-1493-215 | H&M | 2026-09-28 | 1200 |" in text
    assert "| TAL-25-1493-216 | H&M | 2026-09-29 14:30 | — |" in text
    assert verify(text, [], [r], NF)[0]


def test_long_lists_are_cut_with_a_note(monkeypatch):
    rows = [[f"TAL-25-1493-{i}"] for i in range(200, 240)]
    r = _qr(["ExportOrderID"], rows, tool="orders_by_factory_pcd_window")
    text = try_template(r, None)
    assert text.count("\n| TAL-") == 25 and "returned 40 in total" in text
    assert verify(text, [], [r], NF)[0]


def test_not_templated_generated_error_or_empty():
    assert try_template(_qr(["n"], [[1]], tool="generated"), None) is None
    assert try_template(_qr(["n"], []), None) is None
    bad = _qr(["n"], [[1]]); bad.error = "timeout"
    assert try_template(bad, None) is None


def test_template_with_unknown_placeholder_falls_back_to_generic():
    assert try_template(_qr(["Orders"], [[3]]), {"answer": "{nope} has {value}"}) == "Orders: 3 [D1]."


def test_fmt():
    assert fmt(None) == "—" and fmt(date(2026, 1, 2)) == "2026-01-02" and fmt(Decimal("12.50")) == "12.50"
    assert fmt("a|b\nc") == "a/b c"


def test_date_window_param_keeps_the_phrase():
    tool, params = tools.match_fixed_tool("How many PPM meetings does TAL have next week?", tools.load_fixed_tools())
    assert tool["name"] == "ppm_meetings_count_by_factory_window" and params["window"] == "next week"


@pytest.fixture
def wired(monkeypatch):
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: a)
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "_route", lambda q, user: "data")

    def install(result, replies):
        monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [result])
        chat = FakeChat(replies)
        monkeypatch.setattr(orch, "get_chat", lambda: chat)
        return chat
    return install


def test_orchestrator_answers_fixed_tool_without_a_model_call(wired):
    wired(_qr(["Meetings"], [[0]]), [])           # no scripted replies: any model call would fail
    a = orch.answer("How many PPM meetings does TAL have next week?", scope=Scope.unrestricted())
    assert a.text == "TAL has 0 PPM meeting(s) next week [D1]." and not a.not_found
    assert a.usage.calls == 0 and "templated answer (no model call)" in a.warnings
    assert [r.marker for r in a.references] == ["D1"]


def test_generated_sql_is_templated_by_default(wired):
    """J3: a model-written query's rows are the answer; no answer-model call."""
    wired(_qr(["Orders"], [[7]], tool="generated"), [])
    a = orch.answer("some free-form data question", scope=Scope.unrestricted())
    assert a.text == "Orders: 7 [D1]." and a.usage.calls == 0 and not a.not_found
    assert a.results and a.results[0].tool == "generated"


def test_generated_sql_uses_the_model_when_templating_is_off(wired, monkeypatch):
    chat = wired(_qr(["Orders"], [[7]], tool="generated"), ["There are 7 orders [D1]."])
    real = orch.settings()

    class Off:
        def __getitem__(self, k):
            return real[k]

        def get(self, k, default=None):
            return False if k == "answer.templated_generated" else real.get(k, default)
    monkeypatch.setattr(orch, "settings", lambda: Off())
    a = orch.answer("some free-form data question", scope=Scope.unrestricted())
    assert a.text == "There are 7 orders [D1]." and a.usage.calls == 1 and not chat.replies


def test_generated_zero_or_null_scalar_is_left_to_the_model():
    assert try_template(_qr(["n"], [[0]], tool="generated", params={}), None, allow_generated=True) is None
    assert try_template(_qr(["n"], [[None]], tool="generated", params={}), None, allow_generated=True) is None
    assert try_template(_qr(["n"], [[0]], params={}), {"answer": "{value} found"}) == "0 found [D1]."   # fixed tool: an answer


def test_generated_unnamed_count_column_gets_a_label():
    """Live 2026-10-04: `SELECT COUNT(*) FROM dbo.tblEmployee` answered ': 13199 [D1].'"""
    r = QueryResult(database="Hrms5Misami", engine="sqlserver", views=["dbo.tblEmployee"],
                    sql="SELECT TOP (200) COUNT(*) FROM dbo.tblEmployee", tool="generated", columns=[""], rows=[[13199]])
    assert try_template(r, None, allow_generated=True) == "Count: 13199 [D1]."
    r.sql = "SELECT TOP (1) MAX(Qty) FROM dbo.X"
    assert try_template(r, None, allow_generated=True) == "Value: 13199 [D1]."


def test_generated_single_row_is_one_line():
    r = _qr(["ExportOrderID", "PCD", "Qty"], [["TAL-25-1", date(2026, 10, 12), 1200]], tool="generated", params={})
    text = try_template(r, None, allow_generated=True)
    assert text == "ExportOrderID: TAL-25-1 · PCD: 2026-10-12 · Qty: 1200 [D1]."
    assert verify(text, [], [r], NF) == (True, [])
    assert try_template(r, None) is None                      # fixed-tool-only behaviour without the flag


def test_missing_order_id_asks_instead_of_querying(wired, monkeypatch):
    wired(_qr(["n"], [[1]]), [])
    monkeypatch.setattr(tools, "answer_from_data", lambda *a, **k: pytest.fail("must not query the database"))
    a = orch.answer("What is the status of the order?", scope=Scope.unrestricted())
    assert a.route == "clarify" and a.text.startswith("Which export order") and a.usage.calls == 0


# ---------------------------------------------------------------- a one-value answer names what was counted
CASE_99 = ("SELECT COUNT(*) AS EmployeeCount FROM dbo.tblEmployeeDetails "
           "WHERE BloodGroup IN ('O-','O-ve','O Negative')")
CASE_99_TEXT = "29 — EmployeeCount where BloodGroup is O-, O-ve or O Negative [D1]."


def _generated(sql, value=29, column="EmployeeCount", engine="sqlserver"):
    return QueryResult(database="Hrms5Misami", engine=engine, views=["dbo.tblEmployeeDetails"], sql=sql,
                       tool="generated", columns=[column], rows=[[value]])


def test_case_99_says_what_was_counted():
    """Golden case 99 answered "EmployeeCount: 29 [D1].", which does not say which blood group was counted."""
    r = _generated(CASE_99)
    text = try_template(r, None, allow_generated=True)
    assert text == CASE_99_TEXT
    assert verify(text, [], [r], NF) == (True, [])
    assert try_template(_generated(CASE_99.replace("SELECT ", "SELECT TOP (200) ", 1)), None,
                        allow_generated=True) == CASE_99_TEXT                 # as the guard writes it
    assert try_template(r, None) is None                                      # fixed-tool-only without the flag


def test_case_99_through_the_orchestrator_shows_the_filter_on_the_card(wired):
    r = _generated(CASE_99)
    wired(r, [])                                       # no scripted replies: a model call would fail
    a = orch.answer("find the number of employees who has O negative blood", scope=Scope.unrestricted())
    assert a.text == CASE_99_TEXT and a.usage.calls == 0 and not a.not_found
    assert a.references[0].params == {"BloodGroup": "O-, O-ve, O Negative"}
    assert r.params == {}                              # the caller's result is not changed; the answer holds a copy


def test_a_filter_value_with_digits_verifies_only_through_the_params_copy(wired):
    """02 is in no row: like a P2 correction, the filter values are licensed as the query's parameters."""
    from ragbot.agent.templated import filter_params, scalar_filters
    r = _generated("SELECT TOP (200) COUNT(*) AS EmployeeCount FROM dbo.tblEmployeeDetails "
                   "WHERE CompanyID = '02' AND BloodGroup = N'O-'")
    text = try_template(r, None, allow_generated=True)
    assert text == "29 — EmployeeCount where CompanyID is 02 and BloodGroup is O- [D1]."
    ok, problems = verify(text, [], [r], NF)
    assert not ok and problems == ["number not in sources: 02"]
    copy = r.model_copy(update={"params": filter_params(scalar_filters(r))})
    assert copy.params == {"CompanyID": "02", "BloodGroup": "O-"}
    assert verify(text, [], [copy], NF) == (True, [])
    wired(r, [])
    a = orch.answer("how many employees at company 02 have O negative blood", scope=Scope.unrestricted())
    assert a.text == text and a.usage.calls == 0
    assert a.references[0].params == {"CompanyID": "02", "BloodGroup": "O-"}


@pytest.mark.parametrize("where", [
    "BloodGroup = 'O-' OR BloodGroup = 'O-ve'",
    "BloodGroup LIKE 'O%'",
    "JoinDate >= '2026-01-01'",
    "Age BETWEEN 20 AND 30",
    "BloodGroup IN (SELECT BloodGroup FROM dbo.tblBloodGroups)",
    "NOT BloodGroup = 'O-'",
    "BloodGroup <> 'O-'",
    "BloodGroup IS NULL",
    "UPPER(BloodGroup) = 'O-'",
])
def test_a_where_that_cannot_be_stated_exactly_keeps_todays_text(where):
    r = _generated(f"SELECT COUNT(*) AS EmployeeCount FROM dbo.tblEmployeeDetails WHERE {where}")
    assert try_template(r, None, allow_generated=True) == "EmployeeCount: 29 [D1]."


def test_where_filters_reads_only_plain_anded_equalities():
    from ragbot.data.empty_retry import where_filters
    assert where_filters(CASE_99, "tsql") == [("BloodGroup", ["O-", "O-ve", "O Negative"])]
    assert where_filters("SELECT COUNT(*) FROM dbo.T WHERE (A = N'x' AND (B IN (N'p', N'P', 'q'))) AND C = 5",
                         "tsql") == [("A", ["x"]), ("B", ["p", "q"]), ("C", ["5"])]    # N'…', case-insensitive dedupe
    assert where_filters("SELECT COUNT(*) FROM `t` WHERE a = 'x' AND b IN (1, 2)", "mysql") == [("a", ["x"]),
                                                                                               ("b", ["1", "2"])]
    six = ", ".join(f"'{c}'" for c in "abcdef")
    assert where_filters(f"SELECT COUNT(*) FROM dbo.T WHERE A IN ({six})", "tsql") == [("A", list("abcdef"))]
    for sql in [
        f"SELECT COUNT(*) FROM dbo.T WHERE A IN ({six}, 'g')",                         # more than 6: never "and N more"
        "SELECT COUNT(*) FROM dbo.T",                                                   # no WHERE
        "WITH c AS (SELECT * FROM dbo.T WHERE A = 'x') SELECT COUNT(*) FROM c WHERE B = 'y'",
        "SELECT COUNT(*) FROM (SELECT * FROM dbo.T WHERE A = 'x') s WHERE B = 'y'",
        "SELECT COUNT(*) FROM dbo.T WHERE A = 'x' UNION SELECT COUNT(*) FROM dbo.U WHERE A = 'x'",
        "SELECT A, COUNT(*) FROM dbo.T WHERE B = 'x' GROUP BY A HAVING COUNT(*) > 1",
        "SELECT COUNT(*) FROM dbo.T WHERE A = 'x' AND A = 'y'",                        # a column named twice
        "SELECT COUNT(*) FROM dbo.T WHERE A = -1",
        "SELECT COUNT(*) FROM dbo.T WHERE A = ''",
        "SELECT COUNT(*) FROM dbo.T WHERE A = B",
        "SELECT COUNT(*) FROM dbo.T WHERE 'x' = A",
        "not sql at all",
    ]:
        assert where_filters(sql, "tsql") is None, sql


def test_is_blank_result():
    from ragbot.agent.templated import is_blank_result
    assert is_blank_result([], [])
    assert is_blank_result(["n"], [])
    assert is_blank_result(["n"], [[None]]) and is_blank_result(["n"], [[0]]) and is_blank_result(["n"], [[" "]])
    assert not is_blank_result(["n"], [[1]])
    assert not is_blank_result(["a", "b"], [[None, None]])      # two columns: a row, whatever it holds
    assert not is_blank_result(["n"], [[0], [0]])
