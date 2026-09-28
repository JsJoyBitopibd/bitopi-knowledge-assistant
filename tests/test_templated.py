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


def test_generated_sql_still_uses_the_model(wired):
    chat = wired(_qr(["Orders"], [[7]], tool="generated"), ["There are 7 orders [D1]."])
    a = orch.answer("some free-form data question", scope=Scope.unrestricted())
    assert a.text == "There are 7 orders [D1]." and a.usage.calls == 1 and not chat.replies


def test_missing_order_id_asks_instead_of_querying(wired, monkeypatch):
    wired(_qr(["n"], [[1]]), [])
    monkeypatch.setattr(tools, "answer_from_data", lambda *a, **k: pytest.fail("must not query the database"))
    a = orch.answer("What is the status of the order?", scope=Scope.unrestricted())
    assert a.route == "clarify" and a.text.startswith("Which export order") and a.usage.calls == 0
