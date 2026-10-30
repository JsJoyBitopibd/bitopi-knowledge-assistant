"""Phase T6: a clarifying question that remembers it was asked (agent/clarify.py)."""
import pytest

import ragbot.agent.orchestrator as orch
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent.clarify import choices, merge_reply
from ragbot.agent.events import Final
from ragbot.agent.state import ConversationState
from ragbot.auth.models import Scope
from ragbot.data import tools as T
from ragbot.models import QueryResult

PPM = {"slot": "factory", "choices": ["TAL", "RHL", "BGL", "KTL", "CKDL", "All factories"],
       "question": "how many PPM meetings this week?"}
ORDER = {"slot": "order", "choices": [], "question": "What is the status of the order?"}


@pytest.mark.parametrize("pending,reply,want", [
    (PPM, "TAL", "how many PPM meetings at TAL this week?"),
    (PPM, "rhl", "how many PPM meetings at RHL this week?"),
    (PPM, "for BGL", "how many PPM meetings at BGL this week?"),
    (PPM, "All factories", "how many PPM meetings in all factories this week?"),
    (PPM, "tarasima", "how many PPM meetings at TAL this week?"),                  # a company name, read as TAL
    (ORDER, "TAL-25-1493-215", "What is the status of export order TAL-25-1493-215?"),
    (ORDER, "it is tal-25-1493-215", "What is the status of export order TAL-25-1493-215?"),
    (PPM, "Which LCs expire next month and who is the buyer of each one?", None),  # a new question
    (PPM, "hello", None),
    (ORDER, "the big one", None),
    (None, "TAL", None),
])
def test_merge_reply(pending, reply, want):
    assert merge_reply(pending, reply) == want


def test_the_merged_question_reaches_the_fixed_tool():
    tool, params = T.match_fixed_tool(merge_reply(PPM, "TAL"), T.load_fixed_tools())
    assert tool["name"] == "ppm_meetings_count_by_factory_window" and params["factory"] == "TAL"


def test_choices_are_never_wider_than_the_scope():
    rule = T.clarification_rule("how many PPM meetings this week?")
    assert rule["slot"] == "factory"
    assert choices(rule, Scope(factories=["TAL", "RHL"])) == ["TAL", "RHL"]
    everyone = choices(rule, Scope.unrestricted())
    assert everyone[-1] == "All factories" and "TAL" in everyone
    assert choices(T.clarification_rule("What is the status of the order?"), Scope.unrestricted()) == []


# ---------------------------------------------------------------- through the orchestrator
@pytest.fixture
def wired(monkeypatch):
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: a)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "_route", lambda q, user: "data")
    monkeypatch.setattr(orch, "standalone_question",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no rewrite for a clarified reply")))
    return monkeypatch


def test_ask_then_reply_answers_with_the_tool(wired):
    scope = Scope(factories=["TAL", "RHL"])
    first = list(orch.answer_stream("how many PPM meetings this week?", scope=scope))[-1].answer
    assert first.route == "clarify" and first.kind == "clarify"
    assert first.clarify == {"slot": "factory", "choices": ["TAL", "RHL"]}
    seen = []
    import ragbot.data.tools as tools
    wired.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None, **k: seen.append(q) or [
        QueryResult(database="Production", engine="sqlserver", views=["rag.vw_PPMMeeting"], sql="SELECT 3",
                    tool="ppm_meetings_count_by_factory_window", columns=["Meetings"], rows=[[3]],
                    params={"factory": "TAL", "window": "this week"})])
    hist = [{"role": "user", "text": "how many PPM meetings this week?"},
            {"role": "assistant", "text": first.text, "q": "how many PPM meetings this week?", "route": "clarify",
             "clarify": first.clarify, "request_id": "r1", "rewritten": first.rewritten_question}]
    state = ConversationState.from_history(hist, "c1")
    evs = list(orch.answer_stream("TAL", history=hist, scope=scope, state=state))
    a = evs[-1].answer
    assert isinstance(evs[-1], Final) and seen == ["how many PPM meetings at TAL this week?"]
    assert a.rewritten_question == "how many PPM meetings at TAL this week?" and "conversation: clarified" in a.warnings
    assert "3" in a.text and not a.not_found
