"""B4: independent steps run concurrently. Each fake step sleeps DELAY seconds; serial execution would
take at least 2 x DELAY, so finishing well under that proves the overlap."""
import time
from types import SimpleNamespace as NS

import ragbot.agent.orchestrator as orch
import ragbot.data.tools as tools
import ragbot.index_version as iv
import ragbot.llm.base as base
import ragbot.retrieve.retriever as rt
from ragbot.models import Chunk, QueryResult

from test_answer_stream import FakeChat, _chunk

DELAY = 0.4


def test_both_route_searches_documents_while_querying_database(monkeypatch):
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user: a)
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "_route", lambda q, user: "both")

    def slow_retrieve(q, where=None):
        time.sleep(DELAY)
        return [_chunk()]

    def slow_data(q, user="", refresh=False):
        time.sleep(DELAY)
        return [QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"],
                            sql="SELECT Status FROM rag.vw_ExportOrder", columns=["Status"], rows=[["Shipped"]])]

    monkeypatch.setattr(orch, "retrieve", slow_retrieve)
    monkeypatch.setattr(tools, "answer_from_data", slow_data)
    chat = FakeChat(["Use form PCD-02 [P1]. The order status is Shipped [D1]."])
    monkeypatch.setattr(orch, "get_chat", lambda: chat)

    t0 = time.perf_counter()
    a = orch.answer("What is the status of the order and which form approves a PCD change?")
    elapsed = time.perf_counter() - t0
    assert elapsed < 1.5 * DELAY, f"steps ran serially ({elapsed:.2f}s)"
    assert sorted(r.marker for r in a.references) == ["D1", "P1"]


def test_retriever_runs_keyword_search_alongside_embedding(monkeypatch):
    c = _chunk()
    rt._embed_query.cache_clear()

    def slow_embed(texts):
        time.sleep(DELAY)
        return [[0.0] * 4]

    def slow_kw(question, k, where=None):
        time.sleep(DELAY)
        return [(c.id, 1.0)]

    store = NS(query=lambda vec, k, where: [c], get=lambda ids: [c] if c.id in ids else [])
    monkeypatch.setattr(rt, "get_store", lambda: store)
    monkeypatch.setattr(rt, "get_keyword_index", lambda: NS(search=slow_kw))
    monkeypatch.setattr(rt, "get_embedder", lambda: NS(embed=slow_embed))
    monkeypatch.setattr(rt, "get_reranker", lambda: None)

    t0 = time.perf_counter()
    got = rt.retrieve("which form approves a PCD change?")
    elapsed = time.perf_counter() - t0
    assert [x.id for x in got] == [c.id]
    assert elapsed < 1.5 * DELAY, f"keyword search ran after embedding ({elapsed:.2f}s)"
