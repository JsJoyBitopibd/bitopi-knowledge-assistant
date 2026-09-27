"""B2: orchestrator.answer_stream() — event order, verify-before-Final, Replace, provider errors.
Offline: routing, retrieval and the chat model are replaced with fakes."""
import pytest

import ragbot.agent.orchestrator as orch
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent.events import Final, Replace, Stage, Token
from ragbot.llm.base import ChatModel, ChatReply, LLMError
from ragbot.models import Chunk


def _chunk():
    return Chunk(id="d#p4#c1", text="SOP › 3.2\nApproval on form PCD-02 by the Planning Head within 2 days.",
                 source="SOP.pdf", title="SOP", page=4, section="3.2")


class FakeChat(ChatModel):
    """Replays scripted replies; each reply streams as two deltas. An Exception entry is raised."""
    name = "fake"

    def __init__(self, replies):
        self.replies = list(replies)

    def _chat(self, messages, system, max_tokens, temperature):
        raise AssertionError("the answer path must stream")

    def _stream(self, messages, system, max_tokens, temperature):
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        half = len(r) // 2
        yield r[:half]
        yield r[half:]
        yield ChatReply(text=r, input_tokens=10, output_tokens=5, model="fake", stop_reason="stop")


@pytest.fixture
def wire(monkeypatch):
    """Returns a function that installs a route and scripted model replies."""
    logged = []
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user: logged.append(a) or a)
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "retrieve", lambda q, where=None: [_chunk()])

    def install(route, replies):
        monkeypatch.setattr(orch, "_route", lambda q, user: route)
        chat = FakeChat(replies)
        monkeypatch.setattr(orch, "get_chat", lambda: chat)
        return logged
    return install


GOOD = "Use form PCD-02 [P1]."
BAD = "Use form PCD-07 within 5 days [P3]."   # unknown marker + a number not in the source


def test_documents_answer_streams_then_final(wire):
    logged = wire("documents", [GOOD])
    evs = list(orch.answer_stream("Which form approves a PCD change?"))
    stages = [e.name for e in evs if isinstance(e, Stage)]
    assert stages == ["understanding", "searching", "writing"]
    assert "".join(e.text for e in evs if isinstance(e, Token)) == GOOD
    assert not any(isinstance(e, Replace) for e in evs)
    assert isinstance(evs[-1], Final) and sum(isinstance(e, Final) for e in evs) == 1
    a = evs[-1].answer
    assert a.text == GOOD and [r.marker for r in a.references] == ["P1"] and not a.not_found
    assert logged == [a]          # only the verified Final reaches chat.csv


def test_failed_verification_replaces_draft(wire):
    wire("documents", [BAD, GOOD])
    evs = list(orch.answer_stream("Which form approves a PCD change?"))
    kinds = [type(e).__name__ for e in evs]
    first_replace = kinds.index("Replace")
    assert "Token" in kinds[:first_replace] and "Token" in kinds[first_replace:]
    a = evs[-1].answer
    assert a.text == GOOD and any("attempt 1" in w for w in a.warnings)
    assert a.usage.calls == 2


def test_both_attempts_fail_gives_not_found(wire):
    wire("documents", [BAD, BAD])
    a = orch.answer("Which form approves a PCD change?")
    assert a.not_found and a.text.startswith("System doesn't have the data.") and not a.references


def test_provider_error_mid_answer_becomes_friendly_final(wire):
    wire("documents", [LLMError("quota", "429 Too Many Requests")])
    evs = list(orch.answer_stream("Which form approves a PCD change?"))
    a = evs[-1].answer
    assert isinstance(evs[-1], Final) and a.error_kind == "quota" and a.not_found
    assert "429" not in a.text     # friendly text only; raw cause is kept in warnings
    assert any("429" in w for w in a.warnings)


def test_chitchat_yields_final_without_tokens(wire):
    wire("chitchat", [])
    evs = list(orch.answer_stream("hello"))
    assert not any(isinstance(e, Token) for e in evs) and isinstance(evs[-1], Final)


def test_answer_equals_stream_final(wire):
    wire("documents", [GOOD])
    assert orch.answer("Which form approves a PCD change?").text == GOOD


def test_data_miss_clears_draft_before_documents_retry(wire, monkeypatch):
    """A data answer that ends not-found is retried against the documents; the not-found draft must
    be cleared BEFORE the (slow) document search starts, not after it."""
    import ragbot.data.tools as tools
    from ragbot.models import QueryResult
    wire("data", ["System doesn't have the data.", GOOD])
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False: [QueryResult(
        database="DB", engine="sqlserver", views=["rag.vw_X"], sql="SELECT 1", columns=["n"], rows=[[0]])])
    evs = list(orch.answer_stream("how many licences does the Group have?"))
    names = [type(e).__name__ + (":" + e.name if isinstance(e, Stage) else "") for e in evs]
    retry_search = names.index("Stage:searching")
    assert names[retry_search - 1] == "Replace"
    assert evs[-1].answer.text == GOOD
