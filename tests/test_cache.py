"""B5: TTL cache, SQL result cache (as-of kept, refresh bypass, key), documents-answer cache."""
import pytest

import ragbot.agent.orchestrator as orch
import ragbot.data.cache as dcache
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.ttl_cache import TTLCache
from ragbot.auth.models import Scope

from test_answer_stream import GOOD, FakeChat, _chunk


# ---------------------------------------------------------------- TTLCache
def test_ttl_cache_expires_and_evicts_lru():
    now = [0.0]
    c = TTLCache(maxsize=2, clock=lambda: now[0])
    c.put("a", 1, ttl=10)
    c.put("b", 2, ttl=10)
    assert c.get("a") == 1              # touch "a": "b" is now least recently used
    c.put("c", 3, ttl=10)
    assert c.get("b") is None and c.get("a") == 1 and c.get("c") == 3
    now[0] = 10.0
    assert c.get("a") is None           # expired
    c.put("z", 9, ttl=0)
    assert c.get("z") is None           # ttl 0 = not cached


# ---------------------------------------------------------------- SQL result cache
class _Settings(dict):
    def get(self, k, default=None):
        return super().get(k, default)


@pytest.fixture
def sql(monkeypatch):
    dcache.clear()
    calls, logged = [], []

    def fake_run(engine, sql_exec, params, **kw):
        calls.append((sql_exec, dict(params)))
        return ["Status"], [["Shipped"]]

    monkeypatch.setattr(dcache, "run", fake_run)
    monkeypatch.setattr(dcache, "_log_sql", lambda *a, **kw: logged.append(a))
    monkeypatch.setattr(dcache, "settings", lambda: _Settings({"data.result_cache_ttl_seconds": 180}))
    return calls, logged


def _q(params, refresh=False):
    return dcache.cached_run("sqlserver", "SELECT Status FROM x WHERE id=@id", params, conn_env="SQLSERVER_CONN_X",
                             display_sql="SELECT Status FROM rag.vw_X", tool="eo_status", refresh=refresh)


def test_sql_cache_hit_keeps_original_as_of(sql):
    calls, logged = sql
    cols1, rows1, asof1 = _q({"id": 1})
    cols2, rows2, asof2 = _q({"id": 1})
    assert len(calls) == 1 and (cols1, rows1) == (cols2, rows2) and asof2 == asof1
    assert logged and logged[-1][1] == "cache:eo_status"


def test_sql_cache_refresh_and_params_miss(sql):
    calls, _ = sql
    _q({"id": 1})
    _q({"id": 2})                        # different parameters: a different statement result
    _q({"id": 1}, refresh=True)          # Refresh data: always live
    assert len(calls) == 3


def test_sql_cache_ttl_zero_disables(sql, monkeypatch):
    calls, _ = sql
    monkeypatch.setattr(dcache, "settings", lambda: _Settings({"data.result_cache_ttl_seconds": 0}))
    _q({"id": 1})
    _q({"id": 1})
    assert len(calls) == 2


def test_cached_rows_cannot_be_mutated_through_a_result(sql):
    _, rows, _ = _q({"id": 1})
    rows[0][0] = "tampered"
    assert _q({"id": 1})[1] == [["Shipped"]]


# ---------------------------------------------------------------- documents-answer cache
@pytest.fixture
def docs(monkeypatch):
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user: a)
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "retrieve", lambda q, where=None, scope=None: [_chunk()])
    version = [1]
    monkeypatch.setattr(iv, "index_version", lambda: version[0])

    def install(route, replies):
        monkeypatch.setattr(orch, "_route", lambda q, user: route)
        chat = FakeChat(replies)
        monkeypatch.setattr(orch, "get_chat", lambda: chat)
        return chat
    return install, version


def test_repeat_documents_question_is_served_from_cache(docs):
    install, _ = docs
    chat = install("documents", [GOOD])          # only ONE scripted reply: a second model call would fail
    a1 = orch.answer("Which form approves a PCD change?", scope=Scope.unrestricted())
    a2 = orch.answer("  which form approves a PCD change ", scope=Scope.unrestricted())   # same question, different spacing/case
    assert a1.text == a2.text == GOOD and a2.warnings == ["answer cache hit"] and a2.usage.calls == 0
    assert [r.marker for r in a2.references] == ["P1"] and not chat.replies


def test_answer_cache_skips_follow_ups_other_filters_and_new_index(docs):
    install, version = docs
    install("documents", [GOOD, GOOD, GOOD, GOOD])
    orch.answer("Which form approves a PCD change?", scope=Scope.unrestricted())
    hist = [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"}]
    assert "answer cache hit" not in orch.answer("Which form approves a PCD change?", history=hist, scope=Scope.unrestricted()).warnings
    assert "answer cache hit" not in orch.answer("Which form approves a PCD change?", where={"category": ["SOP"]}, scope=Scope.unrestricted()).warnings
    version[0] = 2                               # an ingest finished
    assert "answer cache hit" not in orch.answer("Which form approves a PCD change?", scope=Scope.unrestricted()).warnings


def test_data_answers_are_not_cached(docs, monkeypatch):
    import ragbot.data.tools as tools
    from ragbot.models import QueryResult
    install, _ = docs
    install("data", ["The order status is Shipped [D1].", "The order status is Shipped [D1]."])
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [QueryResult(
        database="BitopiSplint", engine="sqlserver", views=["rag.vw_X"], sql="SELECT 1", columns=["Status"],
        rows=[["Shipped"]])])
    orch.answer("status of EO 25-1234", scope=Scope.unrestricted())
    assert "answer cache hit" not in orch.answer("status of EO 25-1234", scope=Scope.unrestricted()).warnings
