"""Phase U2: "my" and "our" read with the signed-in person's identity (agent/personal.py)."""
import pytest

import ragbot.agent.orchestrator as orch
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent.clarify import merge_reply
from ragbot.agent.personal import personalize
from ragbot.auth.models import Identity, Scope
from ragbot.data import tools as T
from ragbot.models import QueryResult

ME = Identity(employee_code="E0456", company_id="06", unit_id="1", home_factory="TAL")
ALL = Scope.unrestricted()


@pytest.mark.parametrize("q,want", [
    ("my attendance this month", "attendance this month of employee E0456"),
    ("How many days of leave do I have? my leave balance", "How many days of leave do I have? leave balance of employee E0456"),
    ("show me my overtime last week", "show employee E0456 overtime last week"),
    ("our orders shipping next week", "TAL orders shipping next week"),
    ("Which of our lines are below target?", "Which of TAL lines are below target?"),
    ("my factory's lines below target", "TAL lines below target"),
    ("what is the DHU in our unit today?", "what is the DHU in TAL today?"),
    ("Which RHL orders ship next week?", "Which RHL orders ship next week?"),          # no "my"/"our": unchanged
    ("our RHL orders next week", "our RHL orders next week"),                        # names its own factory
    ("Who approves a PCD change in our company policy?", "Who approves a PCD change in TAL policy?"),
])
def test_personalize(q, want):
    assert personalize(q, ME, ALL) == want


def test_never_a_factory_the_scope_does_not_allow_and_nothing_without_an_identity():
    rhl_only = Scope(factories=["RHL"])
    assert personalize("our orders shipping next week", ME, rhl_only) == "our orders shipping next week"
    assert personalize("our orders shipping next week", Identity(), ALL) == "our orders shipping next week"
    assert personalize("our orders shipping next week", None, ALL) == "our orders shipping next week"
    no_factory = Identity(employee_code="E1", company_id="02")
    assert personalize("our orders next week", no_factory, ALL) == "our orders next week"
    assert personalize("my attendance today", no_factory, rhl_only) == "attendance today of employee E1"


def test_the_personalised_question_reaches_the_fixed_tool():
    tool, params = T.match_fixed_tool(personalize("Which of our orders have a ship date next week?", ME, ALL),
                                      T.load_fixed_tools())
    assert params.get("factory") == "TAL"


def test_without_an_identity_a_personal_hr_question_asks_for_the_code():
    assert T.needs_clarification("my attendance this month")
    assert T.needs_clarification("how much overtime did I do? my overtime this month")
    assert T.needs_clarification("show me attendance of TAL today") is None        # "show me" is not about the asker
    assert T.needs_clarification("attendance this month of employee E0456") is None
    rule = T.clarification_rule("my attendance this month")
    pending = {"slot": rule["slot"], "choices": [], "question": "my attendance this month"}
    assert merge_reply(pending, "E0456") == "attendance this month of employee E0456"
    assert merge_reply(pending, "my code is E-0456") == "attendance this month of employee E-0456"
    assert merge_reply(pending, "not sure") is None


@pytest.fixture
def wired(monkeypatch):
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: a)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "_route", lambda q, user: "data")
    seen = []
    import ragbot.data.tools as tools
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None, **k: seen.append(q) or [
        QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], sql="SELECT 4",
                    tool="orders_count_by_factory_ship_window", columns=["Orders"], rows=[[4]],
                    params={"factory": "TAL", "window": "next week"})])
    return seen


def test_through_the_orchestrator_the_question_is_read_for_the_asker(wired):
    a = list(orch.answer_stream("How many of our orders are shipping next week?", scope=ALL, me=ME))[-1].answer
    assert wired == ["How many of TAL orders are shipping next week?"]
    assert a.question == "How many of our orders are shipping next week?"
    assert a.rewritten_question == "How many of TAL orders are shipping next week?"     # shown as "Understood as"
    wired.clear()
    list(orch.answer_stream("How many of our orders are shipping next week?", scope=ALL, me=None))
    assert wired == ["How many of our orders are shipping next week?"]
