"""Phase T5: follow-up or new question (agent/continuation.py), and the previous turn for the SQL model."""
from datetime import date, datetime

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent.continuation import classify
from ragbot.agent.state import ConversationState, SqlContext
from ragbot.auth.models import Scope
from ragbot.config import prompt
from ragbot.data import tools as T
from ragbot.models import QueryResult

from test_answer_stream import FakeChat, _chunk


def _state(q, cols=("Line", "DHU"), rows=None, tool="dhu_by_line", sql="SELECT TOP (200) Line, DHU FROM rag.v"):
    r = QueryResult(database="VistaQ_Default_RHL", engine="sqlserver", views=["rag.vw_LineDayQuality"], tool=tool,
                    sql=sql, columns=list(cols), rows=rows or [["2101", 3.2], ["2102", 4.1]],
                    params={"factory": "TAL", "window": "this week", "from": date(2026, 10, 26), "factory_label": "TAL"},
                    as_of=datetime(2026, 10, 30, 9))
    hist = [{"role": "user", "text": q}, {"role": "assistant", "text": "…", "q": q, "results": [r],
                                          "request_id": "req1", "route": "data"}]
    return ConversationState.from_history(hist, "c1"), hist


DHU = "Which lines are above the DHU target this week?"


@pytest.mark.parametrize("prev,msg,want", [
    (DHU, "b+ blood group", "new"),                                          # logged: was glued onto the DHU question
    (DHU, "include the buyer and style for the lines above target this week", "follow_up"),  # long, no pronoun
    ("line wise production summary of today", "TAL line wise production summary of today with style showing",
     "new"),                                       # logged; since Phase Q a fixed tool answers it on its own
    ("What is the PCD for export order TAL-25-1493-215?", "And what is its ship date?", "follow_up"),
    (DHU, "what about RHL", "follow_up"),
    (DHU, "RHL?", "follow_up"),
    (DHU, "line 2102", "follow_up"),
    ("Which TAL orders have a PCD next week?", "Who approves a PCD change after fabric in-house?", "new"),
    (DHU, "Which RHL orders have a ship date next week?", "new"),            # a fixed tool answers it fully
    (DHU, "SELECT TOP 5 Line FROM rag.vw_LineDayQuality", "new"),
    (DHU, "show it for yesterday instead", "follow_up"),
    (DHU, "dhu of unit 2", "follow_up"),
])
def test_classify(prev, msg, want):
    state, hist = _state(prev)
    assert classify(msg, state, hist) == want


def test_without_history_everything_is_new():
    assert classify("what about RHL", None, []) == "new"


def test_the_sql_context_never_carries_a_row():
    state, _ = _state(DHU, rows=[["SECRET-ROW-VALUE", 9.9]] * 3)
    ctx = SqlContext.from_state(state)
    text = ctx.render()
    assert "SECRET-ROW-VALUE" not in text and "9.9" not in text
    assert "Question: " + DHU in text and "Columns it returned: Line, DHU" in text and "Rows: 3" in text
    assert "factory = TAL" in text and "factory_label" not in text and "SELECT TOP (200) Line, DHU" in text
    long = SqlContext(question="q", sql="SELECT " + "x, " * 2000 + "y FROM rag.v", columns=["c"] * 60)
    assert len(long.render()) <= 2400
    assert SqlContext.from_state(None) is None


def test_the_prompts_carry_the_context_block():
    assert "{context}" in prompt("sql_generate") and "{context}" in prompt("sql_context")


def test_a_statement_written_with_context_is_cached_for_that_conversation_only():
    cats, scope = {}, Scope.unrestricted()
    plain = T._sql_key("sort by date", cats, scope)
    a = T._sql_key("sort by date", cats, scope, context_key="c1:req1")
    b = T._sql_key("sort by date", cats, scope, context_key="c2:req9")
    assert len({plain, a, b}) == 3


# ---------------------------------------------------------------- through the orchestrator
@pytest.fixture
def wired(monkeypatch):
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: a)
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "retrieve", lambda q, where=None, scope=None: [_chunk()])
    monkeypatch.setattr(orch, "_route", lambda q, user: "documents")
    return monkeypatch


def test_a_new_question_is_not_rewritten_and_sees_no_earlier_turns(wired):
    wired.setattr(orch, "standalone_question", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rewrite")))
    chat = FakeChat(["Use form PCD-02 [P1]."])
    seen = []
    orig = chat.stream
    wired.setattr(chat, "stream", lambda messages, **kw: (seen.append(messages[0]["content"]), orig(messages, **kw))[1])
    wired.setattr(orch, "get_chat", lambda: chat)
    state, hist = _state(DHU)
    a = list(orch.answer_stream("Who approves a PCD change after fabric in-house?", history=hist,
                                scope=Scope.unrestricted(), state=state))[-1].answer
    assert a.rewritten_question == "Who approves a PCD change after fabric in-house?"
    assert "Earlier in this conversation" not in seen[0] and "conversation: new" in a.warnings


def test_a_follow_up_is_rewritten_even_when_long_and_without_a_pronoun(wired):
    calls = []
    wired.setattr(orch, "standalone_question", lambda q, h, u="", force=False: calls.append(force) or "rewritten q")
    wired.setattr(orch, "get_chat", lambda: FakeChat(["Use form PCD-02 [P1]."]))
    state, hist = _state(DHU)
    a = list(orch.answer_stream("include the buyer and style for the lines above target this week", history=hist,
                                scope=Scope.unrestricted(), state=state))[-1].answer
    assert calls == [True] and a.rewritten_question == "rewritten q"
    assert "conversation: follow_up (with the previous query)" in a.warnings
