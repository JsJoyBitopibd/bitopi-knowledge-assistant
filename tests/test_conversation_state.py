"""Phase T1: the conversation's earlier results reach the orchestrator (agent/state.py)."""
from datetime import datetime

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent.events import Final
from ragbot.agent.state import ConversationState, PriorTurn
from ragbot.auth.models import Scope
from ragbot.models import QueryResult

from test_answer_stream import FakeChat, _chunk


def _res(rows, tool="orders_by_factory_pcd_window", **kw):
    return QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"],
                       sql="SELECT TOP (200) ExportOrderID, PCD FROM rag.vw_ExportOrder", tool=tool,
                       columns=["ExportOrderID", "PCD"], rows=rows, as_of=datetime(2026, 10, 30, 9, 0), **kw)


def _turn(q, results, rid, **kw):
    return {"role": "assistant", "text": f"answer to {q}", "q": q, "results": results, "request_id": rid,
            "route": kw.pop("route", "data"), "rewritten": kw.pop("rewritten", q), **kw}


ROWS = [["TAL-1", "2026-11-02"], ["TAL-2", "2026-11-03"]]


def test_from_history_keeps_data_turns_with_rows_newest_last():
    h = [{"role": "user", "text": "q1"}, _turn("q1", [_res(ROWS)], "r1"),
         {"role": "user", "text": "q2"}, _turn("q2", [], "r2", route="documents"),
         {"role": "user", "text": "q3"}, _turn("q3", [_res(ROWS)], "r3"),
         {"role": "user", "text": "q4"}, _turn("q4", [_res([])], "r4"),                     # no rows
         {"role": "user", "text": "q5"}, _turn("q5", [_res(ROWS, error="timeout")], "r5"),  # errored
         {"role": "user", "text": "q6"}, _turn("q6", [_res(ROWS)], "r6", not_found=True)]
    st = ConversationState.from_history(h, "c1")
    assert [t.request_id for t in st.turns] == ["r1", "r3"]
    assert st.last_question == "q6" and st.last_request_id == "r6" and st.last_not_found
    assert st.conversation_id == "c1"


def test_the_limit_keeps_only_the_newest():
    h = [_turn(f"q{i}", [_res(ROWS)], f"r{i}") for i in range(6)]
    assert [t.request_id for t in ConversationState.from_history(h, "c", limit=3).turns] == ["r3", "r4", "r5"]
    assert ConversationState.from_history(h, "c", limit=0).turns == []


def test_last_result_only_when_the_data_turn_is_the_last_answer():
    data = _turn("q1", [_res(ROWS)], "r1")
    st = ConversationState.from_history([data], "c")
    assert st.last_result().rows == ROWS and st.key() == "c:r1"
    after_docs = ConversationState.from_history([data, _turn("policy?", [], "r2", route="documents")], "c")
    assert after_docs.last is not None and after_docs.last_result() is None   # nothing of its own to sort


def test_last_result_skips_lookups_and_empty_state():
    lookup_first = _turn("q", [_res(ROWS, tool="lookup"), _res(ROWS, tool="generated")], "r1")
    assert ConversationState.from_history([lookup_first], "c").last_result().tool == "generated"
    empty = ConversationState.from_history([], "c")
    assert empty.last is None and empty.last_result() is None and empty.key() == "c:"


def test_a_clarify_turn_becomes_a_pending_clarification():
    t = _turn("how many PPM meetings this week?", [], "r1", route="clarify",
              clarify={"slot": "factory", "choices": ["TAL", "RHL"]})
    st = ConversationState.from_history([t], "c")
    assert st.pending_clarification == {"slot": "factory", "choices": ["TAL", "RHL"],
                                        "question": "how many PPM meetings this week?"}
    st2 = ConversationState.from_history([t, _turn("other", [], "r2", route="documents")], "c")
    assert st2.pending_clarification is None                 # answered or abandoned: never carried further


def test_the_state_round_trips_through_json():
    st = ConversationState.from_history([_turn("q1", [_res(ROWS)], "r1")], "c")
    back = ConversationState.model_validate_json(st.model_dump_json())
    assert back == st and isinstance(back.turns[0], PriorTurn) and back.last_result().as_of == st.last_result().as_of


# ---------------------------------------------------------------- the orchestrator accepts it and nothing changes
@pytest.fixture
def wire(monkeypatch):
    logged = []
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: logged.append(a) or a)
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "retrieve", lambda q, where=None, scope=None: [_chunk()])
    monkeypatch.setattr(orch, "_route", lambda q, user: "documents")
    return monkeypatch


def test_answer_stream_with_or_without_state_gives_the_same_answer(wire):
    good = "Use form PCD-02 [P1]."
    outs = []
    for state in (None, ConversationState(conversation_id="c")):
        chat = FakeChat([good])
        wire.setattr(orch, "get_chat", lambda c=chat: c)
        evs = list(orch.answer_stream("Which form approves a PCD change?", scope=Scope.unrestricted(), state=state))
        outs.append(evs[-1].answer)
        assert isinstance(evs[-1], Final)
    assert outs[0].text == outs[1].text == good
    assert [r.marker for r in outs[0].references] == [r.marker for r in outs[1].references]


def test_a_reshape_that_found_nothing_or_a_greeting_keeps_the_table_on_screen():
    data = _turn("RHL orders next week", [_res(ROWS)], "r1")
    failed = {**_turn("only zara", [], "r2"), "kind": "requery", "not_found": True}
    hello = {**_turn("hi", [], "r3", route="meta"), "kind": "social"}
    for after in ([failed], [hello], [failed, hello]):
        st = ConversationState.from_history([data, *after], "c")
        assert st.last_result() is not None and st.anchor_request_id == "r1", after
    docs = _turn("who approves a PCD change?", [], "r4", route="documents")
    gone = {**_turn("x", [], "r5"), "not_found": True}
    for after in ([docs], [gone], [failed, docs]):
        assert ConversationState.from_history([data, *after], "c").last_result() is None, after
