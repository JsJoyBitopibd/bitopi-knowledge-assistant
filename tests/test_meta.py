"""Phase T7: messages about the conversation itself (agent/meta.py, agent/capabilities.py)."""
import dataclasses

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent import capabilities, meta
from ragbot.agent.events import Final
from ragbot.agent.state import ConversationState
from ragbot.auth.models import Scope
from ragbot.data import catalog as C
from ragbot.llm.base import ChatModel, ChatReply

from test_answer_stream import FakeChat, _chunk


def _state(q="How many BGL orders are shipping next week?", error=False):
    hist = [{"role": "user", "text": q}, {"role": "assistant", "text": "BGL has 12 …", "q": q, "request_id": "r1",
                                          "route": "data", "error_kind": "quota" if error else "", "results": []}]
    return ConversationState.from_history(hist, "c1")


@pytest.mark.parametrize("msg,kind,asked,shown", [
    ("You showed me the BGL , not TAL", "wrong_entity", "TAL", "BGL"),          # logged in chat.csv
    ("you gave me BGL data not TAL", "wrong_entity", "TAL", "BGL"),
    ("I asked for RHL", "wrong_entity", "RHL", ""),
    ("i meant TAL, not BGL", "wrong_entity", "TAL", "BGL"),
    ("you haven't replied?", "no_reply", "", ""),                              # logged: was rewritten into a query
    ("you didn't answer", "no_reply", "", ""),
    ("?", "no_reply", "", ""),
    ("say that again", "repeat", "", ""),
    ("which database access do you have?", "capability", "", ""),              # logged
    ("tell me what information I can get from the Inventory?", "capability", "", ""),
    ("what can you tell me about quality", "capability", "", ""),
    ("hi", "greeting", "", ""),
    ("Good morning!", "greeting", "", ""),
    ("thanks", "greeting", "", ""),
])
def test_classify(msg, kind, asked, shown):
    m = meta.classify(msg, _state())
    assert m is not None and (m.kind, m.asked, m.shown) == (kind, asked, shown)


@pytest.mark.parametrize("msg", ["line wise?", "their names", "only TAL", "Which data shows the PCD for TAL?",
                                 "What is the PCD for export order TAL-25-1493-215?", "b+ blood group",
                                 "how many PPM meetings this week?", "Which TAL lines have the highest DHU?"])
def test_ordinary_questions_are_not_meta(msg):
    assert meta.classify(msg, _state()) is None


def test_without_an_earlier_answer_there_is_nothing_to_repeat_or_correct():
    assert meta.classify("you haven't replied?", None) is None
    assert meta.classify("You showed me the BGL, not TAL", None) is None


def test_capability_topic():
    assert meta.classify("tell me what information I can get from the Inventory?", None).topic == "Inventory"
    assert meta.classify("which database access do you have?", None).topic == ""


def test_swap_the_record():
    q = "How many BGL orders are shipping next week?"
    assert meta.swap(q, "TAL", "BGL") == "How many TAL orders are shipping next week?"
    assert meta.swap(q, "TAL") == "How many TAL orders are shipping next week?"
    assert meta.swap("Which orders ship next week?", "TAL") == "Which orders ship next week for TAL?"
    assert meta.swap(q, "BGL", "BGL") is None


@pytest.mark.parametrize("text,ok", [
    ("Hello! I can help with export orders, line quality and store stock. Try asking which TAL orders ship next week.",
     True),
    ("We have 5 factories.", False),
    ("Your PCD is 3 Nov.", False),
    ("See [D1].", False),
    ("I read rag.vw_ExportOrder for that.", False),
    ("SELECT Buyer FROM orders WHERE x", False),
    ("Ask me about DaywisePayHour.", False),
    ("x" * 700, False),
])
def test_social_ok(text, ok):
    assert meta.social_ok(text, {"daywisepayhour", "vw_exportorder"}) is ok


# ---------------------------------------------------------------- capabilities
def test_the_sheet_and_capability_answers_name_no_table_and_only_readable_areas(monkeypatch):
    sheet = capabilities.sheet(Scope.unrestricted())
    assert "Inventory" in sheet and "rag." not in sheet and "dbo." not in sheet
    names = capabilities.names()
    assert "vw_exportorder" in names and "buyer" not in names
    assert not any(n in sheet.lower() for n in names)
    text, chips = capabilities.describe("Inventory", Scope.unrestricted())
    assert "Inventory" in text and chips and len(chips) <= 3
    assert not any(n in text.lower() for n in names)
    every, more = capabilities.describe("", Scope.unrestricted())
    assert every.count("- **") >= 4 and more


def test_a_restricted_database_is_never_described_to_a_user_without_its_group(monkeypatch):
    real = C.load_catalogs()
    armed = {}
    for n, c in real.items():
        if n in ("HR", "Hrms5Misami", "HWATT"):
            c = dataclasses.replace(c, restricted_to=("KA-HR",))
        armed[n] = c
    monkeypatch.setattr(C, "load_catalogs", lambda *a, **k: armed)
    mgr = Scope(factories=["*"], groups=["ka-managers"])
    hr = Scope(factories=["*"], groups=["ka-hr"])
    assert "People" not in capabilities.sheet(mgr) and "HR" not in capabilities.sheet(mgr).split(":")[0]
    assert any("hr" in line.lower() for line in capabilities.sheet(hr).splitlines())
    assert not any("manpower" in q.lower() for q in capabilities.examples(mgr, limit=20))


# ---------------------------------------------------------------- through the orchestrator
@pytest.fixture
def wired(monkeypatch):
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: a)
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    return monkeypatch


def test_no_reply_shows_the_last_answer_again_without_a_model(wired):
    wired.setattr(orch, "_route", lambda *a: (_ for _ in ()).throw(AssertionError("no routing")))
    a = list(orch.answer_stream("you haven't replied?", history=[{"role": "user", "text": "x"}],
                                scope=Scope.unrestricted(), state=_state()))[-1].answer
    assert a.kind == "meta" and a.repeat_of == "r1" and a.usage.calls == 0
    err = list(orch.answer_stream("you haven't replied?", history=[{"role": "user", "text": "x"}],
                                  scope=Scope.unrestricted(), state=_state(error=True)))[-1].answer
    assert err.repeat_of == "" and err.follow_ups == ["How many BGL orders are shipping next week?"]


def test_a_capability_question_costs_no_model_call(wired):
    wired.setattr(orch, "_route", lambda *a: (_ for _ in ()).throw(AssertionError("no routing")))
    a = list(orch.answer_stream("tell me what information I can get from the Inventory?",
                                scope=Scope.unrestricted()))[-1].answer
    assert a.kind == "meta" and a.usage.calls == 0 and a.follow_ups and a.references == []


class SmallChat(ChatModel):
    name = "small"

    def __init__(self, text):
        self.text = text

    def _chat(self, messages, system, max_tokens, temperature):
        assert "Inventory" in system and "{sheet}" not in system
        return ChatReply(text=self.text, input_tokens=5, output_tokens=5, model="small")


def test_a_greeting_gets_a_checked_model_reply_or_the_fixed_paragraph(wired):
    import ragbot.llm as llm
    wired.setattr(llm, "get_small_chat", lambda: SmallChat("Hello! I can help with orders, quality and stores. "
                                                           "Try asking about next week's PCDs."))
    a = list(orch.answer_stream("hi", scope=Scope.unrestricted()))[-1].answer
    assert a.kind == "social" and a.text.startswith("Hello!") and a.usage.calls == 1 and "social: model" in a.warnings
    wired.setattr(llm, "get_small_chat", lambda: SmallChat("We shipped 12 orders yesterday."))
    b = list(orch.answer_stream("hi", scope=Scope.unrestricted()))[-1].answer
    assert "12" not in b.text and "social: fallback (checked)" in b.warnings


def test_a_correction_re_asks_the_previous_question_with_the_meant_record(wired):
    seen = []
    import ragbot.data.tools as tools
    from ragbot.models import QueryResult
    wired.setattr(orch, "_route", lambda q, user: "data")
    wired.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None, **k: seen.append(q) or [
        QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], sql="SELECT 7",
                    tool="orders_count_by_factory_ship_window", columns=["Orders"], rows=[[7]],
                    params={"factory": "TAL", "window": "next week"})])
    evs = list(orch.answer_stream("You showed me the BGL , not TAL", history=[{"role": "user", "text": "x"}],
                                  scope=Scope.unrestricted(), state=_state()))
    a = evs[-1].answer
    assert isinstance(evs[-1], Final) and seen == ["How many TAL orders are shipping next week?"]
    assert a.text.startswith("You asked about TAL; my previous answer covered BGL. Here is TAL:")
    assert a.question == "You showed me the BGL , not TAL" and "7" in a.text
