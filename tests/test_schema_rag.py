"""C5: generated SQL sees the tables a question selected, and the guard uses the catalog's full
offered set. Offline: fake model, fake SQL runner, a small catalog with a discovered tier."""
from datetime import datetime

import pytest

import ragbot.data.schema_index as si
import ragbot.data.tools as tools
import ragbot.llm.base as base
from ragbot.config import settings as real_settings
from ragbot.data.catalog import Catalog, Table, View
from ragbot.llm.base import ChatModel, ChatReply
from ragbot.auth.models import Scope


class Settings:
    def __init__(self, **over):
        self.over = over

    def __getitem__(self, k):
        return self.over[k] if k in self.over else real_settings()[k]

    def get(self, k, default=None):
        return self.over[k] if k in self.over else real_settings().get(k, default)


class ScriptedSQL(ChatModel):
    name = "fake"

    def __init__(self, replies):
        self.replies, self.systems = list(replies), []

    def _chat(self, messages, system, max_tokens, temperature):
        self.systems.append(system)
        return ChatReply(text=self.replies.pop(0), model="fake", stop_reason="stop")


def _cat():
    supplier = Table("dbo.SupplierData", "table", 1022, "", ["SupplierID"],
                     [{"name": "SupplierID", "type": "int"}, {"name": "SupplierName", "type": "varchar(100)"},
                      {"name": "Email", "type": "varchar(100)", "sensitive": True}])
    other = Table("dbo.Unrelated", "table", 5, "", [], [{"name": "X", "type": "int"}])
    view = View("rag.vw_ExportOrder", "one row per order", "Orders.", ["ExportOrderID"], {"ExportOrderID": "id"})
    return {"Demo": Catalog("Demo", "tsql", "SQLSERVER_CONN_DEMO", ["Use TOP (n)."], [view],
                            tables=[supplier, other])}


@pytest.fixture
def wired(monkeypatch):
    si._CACHE.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    # generate_and_run logs which tables it was shown; tests must not write the real logs/schema_select.csv
    monkeypatch.setattr("ragbot.data.schema_usage.log_selection", lambda *a, **k: None)
    monkeypatch.setattr(si, "json_sha", lambda cat: "x")

    def no_embedder(q):
        raise RuntimeError("no model in unit tests")    # selection falls back to keyword-only
    import ragbot.retrieve.retriever as rt
    monkeypatch.setattr(rt, "_embed_query", no_embedder)
    monkeypatch.setattr(si, "vectors_path", lambda db: __import__("pathlib").Path("does-not-exist.npz"))
    ran = []
    monkeypatch.setattr(tools, "cached_run", lambda engine, sql, params, **kw: ran.append(sql) or (["N"], [[1022]], datetime.now()))

    def install(replies, **over):
        monkeypatch.setattr(tools, "settings", lambda: Settings(**over))
        chat = ScriptedSQL(replies)
        monkeypatch.setattr(tools, "get_chat", lambda: chat)
        return chat, ran
    yield install
    si._CACHE.clear()


def test_selected_raw_table_reaches_prompt_and_runs(wired):
    chat, ran = wired(["DATABASE: Demo\nSELECT COUNT(*) AS N FROM dbo.SupplierData"])
    r = tools.generate_and_run("How many suppliers are in the supplier data?", _cat(), scope=Scope.unrestricted())
    assert r.error is None and r.rows == [[1022]] and ran
    assert "dbo.SupplierData" in r.views and r.key_columns == ["SupplierID"]
    system = chat.systems[0]
    assert "=== Other tables in Demo" in system and "dbo.SupplierData (1,022 rows)" in system
    assert "Email" not in system                                     # sensitive column never shown
    assert system.index("=== Database: Demo") < system.index("=== Other tables in Demo")   # static part first


def test_sensitive_column_is_refused_even_on_an_offered_table(wired):
    chat, ran = wired(["DATABASE: Demo\nSELECT SupplierName, Email FROM dbo.SupplierData",
                       "DATABASE: Demo\nSELECT SupplierName, Email FROM dbo.SupplierData"])
    r = tools.generate_and_run("List suppliers with their email", _cat(), scope=Scope.unrestricted())
    assert r.error and "restricted column" in r.error and not ran


def test_schema_rag_off_shows_no_raw_tables(wired):
    chat, _ = wired(["DATABASE: Demo\nSELECT TOP (5) ExportOrderID FROM rag.vw_ExportOrder"], **{"data.schema_rag": False})
    tools.generate_and_run("How many suppliers are in the supplier data?", _cat(), scope=Scope.unrestricted())
    assert "=== Other tables in" not in chat.systems[0] and "(none selected for this question)" in chat.systems[0]
