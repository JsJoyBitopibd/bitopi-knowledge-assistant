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
        self.replies, self.systems, self.messages_seen = list(replies), [], []

    def _chat(self, messages, system, max_tokens, temperature):
        self.systems.append(system)
        self.messages_seen += list(messages)
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
    tools._SQL.clear()        # the question->SQL cache (J2) would replay an earlier test's statement
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


def test_constant_select_is_not_a_source(wired):
    """Scope leak suite s33 (2026-10-06): asked about a table it may not read, the model wrote a SELECT of
    the not-found sentence. That row must not become a cited database answer, and must not be cached."""
    chat, ran = wired(["DATABASE: Demo\nSELECT TOP (200) 'System doesn''t have the data.'"])
    r = tools.generate_and_run("How many suppliers are in dbo.SupplierData?", _cat(), scope=Scope.unrestricted())
    assert r.rows == [] and r.error and "reads no view or table" in r.error
    assert ran == []                       # nothing executed, so nothing reached the question->SQL cache


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


# ---- Phase Q: typed literals on the generated path -----------------------------------------------------------
def _typed_cat():
    """A view that declares Line as text, and a raw table whose kind nothing records."""
    from ragbot.data.catalog import Catalog, Table, View
    plan = View("rag.vw_PlanDay", "one row per plan and day", "The plan.", ["Line"],
                {"Factory": "", "Line": "", "Style": "", "PlanDate": "", "DayTarget": ""},
                column_kinds={"Factory": "text", "Line": "text", "Style": "text", "PlanDate": "date",
                              "DayTarget": "int"})
    legacy = Table("dbo.Legacy", "table", 10, "", [], [{"name": "Note", "type": "sql_variant"}])
    return {"Demo": Catalog("Demo", "tsql", "SQLSERVER_CONN_DEMO", ["Use TOP (n)."], [plan], tables=[legacy])}


def test_a_declared_text_column_is_quoted_before_the_statement_ever_runs(wired):
    """The 11 Oct failure, prevented: the model writes Line = 2102 and the server never sees it."""
    chat, ran = wired(["DATABASE: Demo\nSELECT TOP (10) Buyer, Style FROM rag.vw_PlanDay "
                       "WHERE Factory = 'TAL' AND Line = 2102"])
    r = tools.generate_and_run("What style is running on TAL line 2102?", _typed_cat(), scope=Scope.unrestricted())
    assert r.error is None and len(ran) == 1 and len(chat.replies) == 0      # one statement, one model call
    assert "Line = '2102'" in ran[0] and "Line = '2102'" in r.sql


def test_a_conversion_error_is_repaired_once_with_no_model_call(wired, monkeypatch):
    """When no kind is recorded, the server's 22018 is answered by quoting every unknown literal and running
    again — still inside the first attempt, so the model is not asked a second time."""
    chat, ran = wired(["DATABASE: Demo\nSELECT TOP (10) Note FROM dbo.Legacy WHERE Note = 2102"])
    calls = {"n": 0}

    def flaky(engine, sql, params, **kw):
        calls["n"] += 1
        ran.append(sql)
        if "Note = 2102" in sql:
            raise Exception("DataError: ('22018', \"Conversion failed when converting the nvarchar value "
                            "'Remi Holdings Ltd.' to data type int. (245)\")")
        return ["Note"], [["ok"]], datetime.now()

    monkeypatch.setattr(tools, "cached_run", flaky)
    r = tools.generate_and_run("note 2102", _typed_cat(), scope=Scope.unrestricted())
    assert r.error is None and r.rows == [["ok"]]
    assert calls["n"] == 2 and len(chat.replies) == 0            # two statements, still one model call
    assert "Note = '2102'" in r.sql


def test_a_failure_that_is_not_a_conversion_still_reaches_the_model_with_the_column_kinds(wired, monkeypatch):
    chat, ran = wired(["DATABASE: Demo\nSELECT TOP (10) Style FROM rag.vw_PlanDay WHERE Line = '2102'",
                       "DATABASE: Demo\nSELECT TOP (10) Style FROM rag.vw_PlanDay WHERE Line = '2102'"])
    monkeypatch.setattr(tools, "cached_run",
                        lambda *a, **k: (_ for _ in ()).throw(Exception("('HYT00', 'Query timeout expired (0)')")))
    r = tools.generate_and_run("what style is on line 2102", _typed_cat(), scope=Scope.unrestricted())
    assert r.error and "HYT00" in r.error
    asked = [m["content"] for m in chat.messages_seen if m["role"] == "user"]
    assert any("Column kinds of what the query read" in c and "Line (text)" in c for c in asked)


def test_the_database_line_is_read_though_the_model_copies_the_dialect_label():
    """Golden case 100 (2026-10-13). The prompt lists each database with its dialect, so the model wrote
    "DATABASE: Hrms5Misami (tsql)". The name was matched exactly, found nothing, and the statement fell
    through to the keyword-best database — BitopiSplint — whose guard then refused the HR view the model
    had correctly chosen, and the question answered not-found."""
    cats = {"BitopiSplint": None, "Inventory": None, "Hrms5Misami": None, "HR": None}

    def pick(line):
        return tools._split_database_line(f"{line}\nSELECT 1 FROM dbo.T", cats, "BitopiSplint")[0]

    assert pick("DATABASE: Hrms5Misami (tsql)") == "Hrms5Misami"       # the reply that failed
    assert pick("DATABASE: Hrms5Misami") == "Hrms5Misami"              # and the plain form still works
    assert pick("DATABASE: [Hrms5Misami]") == "Hrms5Misami"
    assert pick("DATABASE: hrms5misami (t-sql, read only)") == "Hrms5Misami"
    assert pick("DATABASE: HR (tsql)") == "HR"                         # a label never matches a sibling
    assert pick("DATABASE: Payroll2") == "BitopiSplint"                # nothing offered by that name
    db, sql = tools._split_database_line("SELECT 1 FROM dbo.T", cats, "BitopiSplint")
    assert db == "BitopiSplint" and sql == "SELECT 1 FROM dbo.T"       # no line at all: SQL kept whole
    assert tools._split_database_line("DATABASE: HR\nSELECT 1", cats, "BitopiSplint")[1] == "SELECT 1"
