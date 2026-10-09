"""B2: orchestrator.answer_stream() — event order, verify-before-Final, Replace, provider errors.
Offline: routing, retrieval and the chat model are replaced with fakes."""
import pytest

import ragbot.agent.orchestrator as orch
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent.events import Final, Replace, Stage, Token
from ragbot.llm.base import ChatModel, ChatReply, LLMError
from ragbot.models import Chunk
from ragbot.auth.models import Scope


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
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: logged.append(a) or a)
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "retrieve", lambda q, where=None, scope=None: [_chunk()])

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
    evs = list(orch.answer_stream("Which form approves a PCD change?", scope=Scope.unrestricted()))
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
    evs = list(orch.answer_stream("Which form approves a PCD change?", scope=Scope.unrestricted()))
    kinds = [type(e).__name__ for e in evs]
    first_replace = kinds.index("Replace")
    assert "Token" in kinds[:first_replace] and "Token" in kinds[first_replace:]
    a = evs[-1].answer
    assert a.text == GOOD and any("attempt 1" in w for w in a.warnings)
    assert a.usage.calls == 2


def test_both_attempts_fail_gives_not_found(wire):
    wire("documents", [BAD, BAD])
    a = orch.answer("Which form approves a PCD change?", scope=Scope.unrestricted())
    assert a.not_found and a.text.startswith("System doesn't have the data.") and not a.references


def test_provider_error_mid_answer_becomes_friendly_final(wire):
    wire("documents", [LLMError("quota", "429 Too Many Requests")])
    evs = list(orch.answer_stream("Which form approves a PCD change?", scope=Scope.unrestricted()))
    a = evs[-1].answer
    assert isinstance(evs[-1], Final) and a.error_kind == "quota" and a.not_found
    assert "429" not in a.text     # friendly text only; raw cause is kept in warnings
    assert any("429" in w for w in a.warnings)


def test_chitchat_yields_final_without_tokens(wire):
    wire("chitchat", [])
    evs = list(orch.answer_stream("hello", scope=Scope.unrestricted()))
    assert not any(isinstance(e, Token) for e in evs) and isinstance(evs[-1], Final)


def test_answer_equals_stream_final(wire):
    wire("documents", [GOOD])
    assert orch.answer("Which form approves a PCD change?", scope=Scope.unrestricted()).text == GOOD


def test_data_miss_clears_draft_before_documents_retry(wire, monkeypatch):
    """A data answer that ends not-found is retried against the documents; the not-found draft must
    be cleared BEFORE the (slow) document search starts, not after it. (Two results with real but irrelevant
    rows reach the model; a single blank 0/NULL result takes the shorter path tested below.)"""
    import ragbot.data.tools as tools
    from ragbot.models import QueryResult
    wire("data", ["System doesn't have the data.", GOOD])
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [
        QueryResult(database="DB", engine="sqlserver", views=["rag.vw_X"], sql="SELECT 1", columns=["n"], rows=[[5]]),
        QueryResult(database="DB", engine="sqlserver", views=["rag.vw_Y"], sql="SELECT 2", columns=["m"], rows=[[3]])])
    evs = list(orch.answer_stream("how many licences does the Group have?", scope=Scope.unrestricted()))
    names = [type(e).__name__ + (":" + e.name if isinstance(e, Stage) else "") for e in evs]
    retry_search = names.index("Stage:searching")
    assert names[retry_search - 1] == "Replace"
    assert evs[-1].answer.text == GOOD


def test_the_writing_stage_names_its_sources_before_the_answer_arrives(wire):
    """I3: the provider sends the answer in one burst, so the sources are what the user sees first."""
    wire("documents", [GOOD])
    evs = list(orch.answer_stream("Which form approves a PCD change?", scope=Scope.unrestricted()))
    writing = next(e for e in evs if isinstance(e, Stage) and e.name == "writing")
    assert writing.sources == ["SOP p. 4"]
    first_token = next(i for i, e in enumerate(evs) if isinstance(e, Token))
    assert evs.index(writing) < first_token


def test_a_blank_generated_scalar_goes_straight_to_the_documents(wire, monkeypatch):
    """P0: SUM over no rows returns [[None]] — truthy, so it used to cost an answer-model call that said
    not-found, and only then the documents search. Now it is no rows: one search, no model call for the data."""
    import ragbot.data.tools as tools
    from ragbot.models import QueryResult
    logged = wire("data", [GOOD])                       # one reply: a data-side answer call would exhaust it
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [QueryResult(
        database="Inventory", engine="sqlserver", views=["dbo.vw_productItemBalance"], sql="SELECT SUM(x)",
        columns=[""], rows=[[None]], databases_considered=["FM", "Inventory"])])
    evs = list(orch.answer_stream("closing balance of 1178108 at company 02", scope=Scope.unrestricted()))
    names = [type(e).__name__ + (":" + e.name if isinstance(e, Stage) else "") for e in evs]
    assert "Replace" not in names and names.count("Stage:searching") == 1
    assert evs[-1].answer.text == GOOD
    assert logged[-1].databases_considered == ["FM", "Inventory"]


def test_a_blank_generated_scalar_with_no_documents_is_not_found_without_a_model_call(wire, monkeypatch):
    import ragbot.data.tools as tools
    from ragbot.models import QueryResult
    logged = wire("data", [])                           # no reply available: any model call would raise
    monkeypatch.setattr(orch, "retrieve", lambda q, where=None, scope=None: [])
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [QueryResult(
        database="Inventory", engine="sqlserver", views=["dbo.X"], sql="SELECT COUNT(*)", columns=["n"], rows=[[0]])])
    evs = list(orch.answer_stream("how many x", scope=Scope.unrestricted()))
    assert evs[-1].answer.not_found and evs[-1].answer.text.startswith("System doesn't have the data.")


def _repaired(monkeypatch):
    import ragbot.data.tools as tools
    from ragbot.models import QueryResult
    lookup = QueryResult(database="Inventory", engine="sqlserver", views=["dbo.Balance"], tool="lookup",
                         sql="SELECT DISTINCT TOP (20) [SLolcation] FROM dbo.Balance WHERE ItemCode = '1178108'",
                         columns=["SLolcation"], rows=[["Central Store (Misami)"], ["Maintenance Store (Misami)"]])
    main = QueryResult(database="Inventory", engine="sqlserver", views=["dbo.Balance"], tool="generated",
                       sql="SELECT TOP (200) SUM(GRNQty) - SUM(IssueQty) AS Balance FROM dbo.Balance WHERE ItemCode = '1178108' AND SLolcation = 'Maintenance Store (Misami)'",
                       params={"SLolcation": "Maintenance Store (Misami)"}, columns=["Balance"], rows=[[0.875]],
                       filters={"ItemCode": "1178108", "SLolcation": "Maintenance Store (Misami)"},
                       corrections=[["SLolcation", "mantainance store", "Maintenance Store (Misami)"]], checked=True)
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [main, lookup])


def test_a_repaired_result_is_templated_with_a_cited_note_and_no_model_call(wire, monkeypatch):
    """P2: the misspelled store was corrected from the recorded values; the answer says so, citing the lookup."""
    wire("data", [])
    _repaired(monkeypatch)
    evs = list(orch.answer_stream("closing balance of 1178108 in the mantainance store", scope=Scope.unrestricted()))
    a = evs[-1].answer
    assert not a.not_found and a.usage.calls == 0 and a.text.startswith("Balance: 0.875 [D1].")
    assert '"mantainance store" in the question was matched to "Maintenance Store (Misami)" as recorded in the data [D2].' in a.text
    assert [r.marker for r in a.references] == ["D1", "D2"] and a.results[1].tool == "lookup"


def test_a_checked_empty_result_is_not_found_in_the_users_words_with_chips_and_no_document_search(wire, monkeypatch):
    import ragbot.data.tools as tools
    from ragbot.models import QueryResult
    wire("data", [])
    monkeypatch.setattr(orch, "retrieve", lambda q, where=None, scope=None: pytest.fail("the documents cannot rescue a checked data question"))
    empty = QueryResult(database="Inventory", engine="sqlserver", views=["dbo.Balance"], tool="generated", sql="SELECT 1",
                        columns=["Balance"], rows=[[None]], filters={"ItemCode": "1178108", "SLolcation": "xyz store"},
                        candidates=["Central Store (Misami)", "Maintenance Store (Misami)"], checked=True)
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [empty])
    evs = list(orch.answer_stream("closing balance of 1178108 in the xyz store", scope=Scope.unrestricted()))
    a = evs[-1].answer
    assert a.not_found and a.usage.calls == 0
    assert a.text == ("System doesn't have the data. I checked Inventory (store system) for '1178108', 'xyz store' and found "
                      "no matching records; please check the number, name or spelling in the question.")
    assert a.follow_ups[:2] == ["closing balance of 1178108 in the Central Store (Misami)",
                                "closing balance of 1178108 in the Maintenance Store (Misami)"]


def test_a_fixed_tool_zero_is_still_an_answer(wire, monkeypatch):
    import ragbot.data.tools as tools
    from ragbot.models import QueryResult
    wire("data", [])
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [QueryResult(
        database="P", engine="sqlserver", views=["rag.vw_PPMMeeting"], sql="SELECT 1", tool="some_count_tool",
        columns=["Meetings"], rows=[[0]])])
    evs = list(orch.answer_stream("how many meetings", scope=Scope.unrestricted()))
    assert not evs[-1].answer.not_found and "0" in evs[-1].answer.text
