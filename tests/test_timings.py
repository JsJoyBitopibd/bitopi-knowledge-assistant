"""I1: every answer carries a request id and per-stage timings; the id reaches chat.csv, calls.csv and sql.csv,
also from the worker threads of the "both" route, and the caller's context is never changed."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.data.tools as tools
import ragbot.index_version as iv
import ragbot.logs as logs
from ragbot import trace
from ragbot.auth.models import Scope
from ragbot.data import connectors
from ragbot.llm.base import LLMError
from ragbot.models import QueryResult

from test_answer_stream import FakeChat, _chunk


def test_outside_a_request_everything_is_a_no_op():
    with trace.span("retrieve"):
        trace.add("data.db", 1.0)
        trace.first("first_token")
    assert trace.current() is None and trace.request_id() == ""


def test_steps_run_in_the_request_context_and_the_caller_keeps_its_own():
    def steps():
        with trace.span("route"):
            time.sleep(0.01)
        yield trace.request_id()
        trace.add("data.db", 0.5)
        trace.add("data.db", 0.25)
        trace.first("first_token", 1.0)
        trace.first("first_token", 9.0)                      # only the first value counts
        yield trace.request_id()

    t, ctx = trace.start()
    ids = list(trace.run_steps(ctx, steps()))
    assert ids == [t.id, t.id] and len(t.id) == 12
    snap = t.snapshot()
    assert snap["route"] >= 0.01 and snap["data.db"] == 0.75 and snap["first_token"] == 1.0 and "total" in snap
    assert trace.current() is None                            # nothing leaked into the caller


def test_submit_carries_the_request_to_a_worker_thread():
    pool = ThreadPoolExecutor(max_workers=1)
    seen = {}

    def work():
        seen["thread"] = threading.current_thread().name
        seen["id"] = trace.request_id()
        trace.add("retrieve", 0.2)

    t, ctx = trace.start()
    ctx.run(lambda: trace.submit(pool, work).result())
    assert seen["id"] == t.id and seen["thread"] != threading.current_thread().name
    assert t.snapshot()["retrieve"] == 0.2
    pool.submit(work).result()                                # plain submit: no request in the worker
    assert seen["id"] == ""


@pytest.fixture
def rows(monkeypatch):
    """Capture every log row by file name instead of writing logs/."""
    got: dict[str, list[dict]] = {}

    def capture(name, header, row, folder=None):
        got.setdefault(name, []).append(dict(zip(header, row)))

    orch._ANSWERS.clear()
    monkeypatch.setattr(orch, "append_row", capture)
    monkeypatch.setattr(logs, "append_row", capture)           # calls.csv and sql.csv import it lazily
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    return got


def test_a_documents_answer_is_timed_and_its_rows_share_the_request_id(rows, monkeypatch):
    monkeypatch.setattr(orch, "_route", lambda q, user: "documents")
    monkeypatch.setattr(orch, "retrieve", lambda q, where=None, scope=None: [_chunk()])
    chat = FakeChat(["Use form PCD-02 [P1]."])
    monkeypatch.setattr(orch, "get_chat", lambda: chat)
    a = orch.answer("Which form approves a PCD change?", scope=Scope.unrestricted())
    assert len(a.request_id) == 12
    for key in ("route", "answer", "verify", "llm.answer", "llm.answer.first_token", "first_token", "total"):
        assert key in a.timings, key
    assert a.timings["first_token"] <= a.timings["total"]
    chat_row, = rows["chat.csv"]
    call_row, = rows["calls.csv"]
    assert chat_row["request_id"] == call_row["request_id"] == a.request_id
    assert json.loads(chat_row["timings"]) == a.timings and chat_row["seconds"] == a.timings["total"]
    again = orch.answer("Which form approves a PCD change?", scope=Scope.unrestricted())   # answer cache hit
    assert again.request_id != a.request_id and set(again.timings) == {"total"}


def test_the_both_route_times_both_sources_and_tags_sql_rows(rows, monkeypatch):
    monkeypatch.setattr(orch, "_route", lambda q, user: "both")

    def docs(q, where=None, scope=None):
        with trace.span("retrieve"):
            return [_chunk()]

    def data(q, user="", refresh=False, scope=None):
        with trace.span("data"):
            connectors._log("sqlserver", "eo_status", "SELECT 1", "SELECT 1", {}, 1, 40.0, user=user)
            return [QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"],
                                sql="SELECT Status FROM rag.vw_ExportOrder", columns=["Status"], rows=[["Shipped"]])]

    monkeypatch.setattr(orch, "retrieve", docs)
    monkeypatch.setattr(tools, "answer_from_data", data)
    chat = FakeChat(["Use form PCD-02 [P1]. The order status is Shipped [D1]."])
    monkeypatch.setattr(orch, "get_chat", lambda: chat)
    a = orch.answer("What is the status of the order and which form approves a PCD change?",
                    scope=Scope.unrestricted())
    assert {"retrieve", "data", "data.db"} <= set(a.timings) and a.timings["data.db"] == 0.04
    sql_row, = rows["sql.csv"]                                # written in a worker thread
    assert sql_row["request_id"] == a.request_id == rows["chat.csv"][0]["request_id"]


def test_a_provider_failure_is_still_logged_with_its_request(rows, monkeypatch):
    monkeypatch.setattr(orch, "_route", lambda q, user: "documents")
    monkeypatch.setattr(orch, "retrieve", lambda q, where=None, scope=None: [_chunk()])
    chat = FakeChat([LLMError("quota", "429 Too Many Requests")])
    monkeypatch.setattr(orch, "get_chat", lambda: chat)
    a = orch.answer("Which form approves a PCD change?", scope=Scope.unrestricted())
    assert a.error_kind == "quota" and a.request_id and "total" in a.timings
    assert rows["chat.csv"][0]["request_id"] == a.request_id
