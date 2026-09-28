"""F1: the user's scope is enforced where the data is read — both document search paths and every SQL
statement — never by the prompt (PRD FR-4.3, 4.4, 4.5)."""
import sqlite3

import pytest
import sqlglot

from ragbot.agent import orchestrator as orch
from ragbot.auth.filters import scope_where
from ragbot.auth.models import Scope
from ragbot.data import aggregates, tools
from ragbot.data.catalog import Catalog, View, load_catalogs
from ragbot.data.guard import GuardError, guard
from ragbot.data.virtual import assert_scoped, rewrite_virtual
from ragbot.models import Chunk, QueryResult
from ragbot.retrieve import retriever as rt
from ragbot.retrieve.keyword import FtsKeywordIndex
from ragbot.store import Registry

TAL = Scope(factories=["TAL"])
TAL_RHL = Scope(factories=["RHL", "TAL"])
ALL = Scope.unrestricted()


# ---------------------------------------------------------------- documents: keyword index
def _chunk(cid, factory="ALL", conf="internal", text="the PCD approval form IT-03 is signed by the planning head"):
    return Chunk(id=cid, text=f"Doc › 1\n{text}", source=cid.split("#")[0] + ".pdf", title=cid.split("#")[0], page=1,
                 category="SOP", factory=factory, confidentiality=conf)


@pytest.fixture
def fts(tmp_path):
    reg = Registry(tmp_path / "registry.db")
    for c in (_chunk("tal#p1#c1", "TAL"), _chunk("rhl#p1#c1", "RHL"), _chunk("sop#p1#c1", "ALL"),
              _chunk("hr#p1#c1", "ALL", "restricted"), _chunk("rhlcode#p1#c1", "RHL", text="form RHL-SOP-552 only")):
        reg.replace_chunks(c.source, [c])
    return FtsKeywordIndex(tmp_path / "registry.db")


def test_keyword_search_never_returns_out_of_scope_chunks(fts):
    got = {cid for cid, _ in fts.search("PCD approval form", 20, {"superseded": False, **scope_where(TAL)})}
    assert got == {"tal#p1#c1", "sop#p1#c1"}                       # not RHL, not restricted
    assert fts.search("RHL-SOP-552", 20, scope_where(TAL)) == []   # an exact code only in an RHL chunk
    assert {c for c, _ in fts.search("PCD approval form", 20, scope_where(ALL))} >= {"rhl#p1#c1", "hr#p1#c1"}


def test_keyword_index_refuses_filters_it_cannot_apply(fts):
    with pytest.raises(ValueError, match="cannot filter"):
        fts.search("PCD", 20, {"owner": ["x"]})
    assert fts.search("PCD", 20, {"factory": []}) == []


# ---------------------------------------------------------------- documents: retriever
class SpyStore:
    def __init__(self, chunks):
        self.chunks, self.where = chunks, None

    def query(self, vec, k, where=None):
        self.where = where
        return self.chunks

    def get(self, ids):
        return [c for c in self.chunks if c.id in ids]


class SpyKw:
    def __init__(self, ids):
        self.ids, self.where = ids, None

    def search(self, q, k, where=None):
        self.where = where
        return [(i, 1.0) for i in self.ids]


@pytest.fixture
def spies(monkeypatch):
    leaked = _chunk("rhl#p1#c1", "RHL")                             # a buggy index returns it anyway
    store, kw = SpyStore([_chunk("tal#p1#c1", "TAL"), leaked]), SpyKw(["tal#p1#c1", "rhl#p1#c1"])
    monkeypatch.setattr(rt, "get_store", lambda: store)
    monkeypatch.setattr(rt, "get_keyword_index", lambda: kw)
    monkeypatch.setattr(rt, "_embed_query", lambda q: (0.1, 0.2))
    monkeypatch.setattr(rt, "get_reranker", lambda: None)
    return store, kw


def test_scope_reaches_both_search_paths_and_the_final_check(spies):
    store, kw = spies
    got = rt.retrieve("which form approves a PCD change?", scope=TAL)
    assert [c.id for c in got] == ["tal#p1#c1"]                    # the leaked RHL chunk is dropped by _passes
    assert kw.where["factory"] == ["ALL", "TAL"] and kw.where["confidentiality"] == ["public", "internal"]
    clauses = store.where["$and"]
    assert {"factory": {"$in": ["ALL", "TAL"]}} in clauses


def test_a_caller_cannot_widen_the_scope(spies):
    with pytest.raises(ValueError, match="set by the scope"):
        rt.retrieve("x", where={"factory": ["RHL"]}, scope=TAL)
    with pytest.raises(TypeError):
        rt.retrieve("x")                                             # no scope: fails, never searches everything


# ---------------------------------------------------------------- data: the view filter
def _cat(scope_col="Factory", definition="SELECT o.Id, o.Qty, f.Factory FROM dbo.Orders o JOIN dbo.Files f ON f.Id = o.FileId"):
    v = View(name="rag.vw_Orders", grain="g", description="d", key_columns=["Id"],
             columns={"Id": "id", "Qty": "qty", "Factory": "f"}, definition=definition, scope_column=scope_col)
    return Catalog(database="DB", dialect="tsql", connection_env="X", rules=[], views=[v])


def _rewrite(sql, cat, scope):
    return rewrite_virtual(guard(sql, cat.view_names, "tsql", 200), cat, scope=scope)


def test_every_view_is_filtered_inside_itself():
    sql, _ = _rewrite("SELECT COUNT(*) AS n FROM rag.vw_Orders", _cat(), TAL_RHL)
    assert "WHERE v.[Factory] IN ('RHL', 'TAL')" in sql
    cte = sqlglot.parse_one(sql, read="tsql").find(sqlglot.exp.CTE)
    assert cte.this.args["where"] is not None                       # inside the view, so COUNT(*) counts only TAL/RHL


def test_unrestricted_scope_leaves_the_statement_as_before():
    sql, _ = _rewrite("SELECT Id FROM rag.vw_Orders", _cat(), ALL)
    assert "v.[Factory]" not in sql


def test_a_view_without_a_scope_column_is_refused_for_a_restricted_user():
    with pytest.raises(GuardError, match="not available"):
        _rewrite("SELECT Id FROM rag.vw_Orders", _cat(scope_col=None), TAL)
    _rewrite("SELECT Id FROM rag.vw_Orders", _cat(scope_col=None), ALL)   # everyone else: fine


def test_a_real_view_is_wrapped_too_and_no_factories_means_no_data():
    real = _cat(definition=None)
    sql, _ = _rewrite("SELECT Id FROM rag.vw_Orders", real, TAL)
    assert "SELECT * FROM rag.vw_Orders" in sql and "IN ('TAL')" in sql
    with pytest.raises(GuardError, match="no factory"):
        _rewrite("SELECT Id FROM rag.vw_Orders", _cat(), Scope(factories=[]))


def test_the_self_check_rejects_a_statement_that_is_not_filtered():
    cat = _cat()
    v = cat.views[0]
    unfiltered = "WITH rag_vw_Orders AS (SELECT o.Id, f.Factory FROM dbo.Orders o JOIN dbo.Files f ON f.Id = o.FileId)\nSELECT TOP (200) Id FROM rag_vw_Orders"
    with pytest.raises(GuardError, match="scope check"):
        assert_scoped(unfiltered, cat, TAL, [v])
    wrong = unfiltered.replace("SELECT o.Id", "SELECT * FROM (SELECT o.Id").replace(
        "o.FileId)", "o.FileId) AS v WHERE v.[Factory] IN ('TAL', 'RHL'))")
    with pytest.raises(GuardError, match="scope check"):
        assert_scoped(wrong, cat, TAL, [v])                         # a wider list than the scope's
    outside = _rewrite("SELECT Id FROM rag.vw_Orders", cat, TAL)[0] + " UNION ALL SELECT Id FROM rag.vw_Orders"
    with pytest.raises(GuardError, match="outside"):
        assert_scoped(outside, cat, TAL, [v])


def test_every_real_fixed_tool_runs_filtered_for_a_factory_user():
    import yaml
    from pathlib import Path
    cats = load_catalogs()
    for tool in yaml.safe_load(Path("config/fixed_tools.yaml").read_text(encoding="utf-8")):
        cat = cats[tool["database"]]
        sql, views = rewrite_virtual(guard(tool["sql"], cat.view_names, cat.dialect, 200), cat, scope=TAL)
        assert "IN ('TAL')" in sql, tool["name"]
        assert sqlglot.parse_one(sql, read="tsql")


# ---------------------------------------------------------------- data: fixed tools, raw tables, aggregates
def test_a_fixed_tool_for_another_factory_is_denied_without_a_query(monkeypatch):
    monkeypatch.setattr(tools, "cached_run", lambda *a, **k: pytest.fail("must not query"))
    tool = {"name": "orders_by_factory", "database": "DB", "sql": "SELECT TOP (10) Id FROM rag.vw_Orders WHERE Factory = @factory"}
    r = tools.run_fixed_tool(tool, {"factory": "RHL"}, {"DB": _cat()}, scope=TAL)
    assert r.denied and r.error and not r.rows


def test_a_fixed_tool_runs_with_the_filter_and_a_scoped_cache_key(monkeypatch):
    seen = {}

    def fake_cached_run(engine, sql_exec, params, **kw):
        seen.update(sql=sql_exec, scope_key=kw.get("scope_key"))
        return ["Id"], [[1]], None

    monkeypatch.setattr(tools, "cached_run", fake_cached_run)
    tool = {"name": "orders_by_factory", "database": "DB", "sql": "SELECT TOP (10) Id FROM rag.vw_Orders WHERE Factory = @factory"}
    tools.run_fixed_tool(tool, {"factory": "TAL"}, {"DB": _cat()}, scope=TAL)
    assert "IN ('TAL')" in seen["sql"] and seen["scope_key"] == TAL.key()


def test_raw_tables_are_not_offered_to_a_factory_user():
    t = sqlglot  # noqa: F841
    from ragbot.data.catalog import Table
    cat = _cat()
    cat.tables = [Table("dbo.SupplierData", rows=1022, columns=[{"name": "SupplierName", "type": "varchar"}])]
    tools._guard_for("SELECT TOP (5) SupplierName FROM dbo.SupplierData", cat, 200, ALL)
    with pytest.raises(GuardError):
        tools._guard_for("SELECT TOP (5) SupplierName FROM dbo.SupplierData", cat, 200, TAL)


def test_the_local_copy_is_read_through_the_scope_filter(tmp_path, monkeypatch):
    db = tmp_path / "aggregates.db"
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE pcd_history ("ExportOrderID" COLLATE NOCASE, "VersionNo", "Factory" COLLATE NOCASE)')
    con.executemany("INSERT INTO pcd_history VALUES (?,?,?)", [("TAL-1", 1, "TAL"), ("RHL-1", 1, "RHL")])
    con.commit(); con.close()
    sql = "SELECT ExportOrderID FROM pcd_history ORDER BY ExportOrderID LIMIT 50"
    scoped = aggregates.scoped_local_sql(sql, "pcd_history", "Factory", ["TAL"])
    assert aggregates.run_local(scoped, {}, 50, path=db)[1] == [["TAL-1"]]
    assert aggregates.has_column("pcd_history", "factory", path=db)
    assert aggregates.scoped_local_sql("SELECT * FROM pcd_history a JOIN pcd_history b ON 1=1", "pcd_history",
                                       "Factory", ["TAL"]) is None
    assert aggregates.scoped_local_sql(sql, "pcd_history", "Factory", []) is None


def test_a_copy_without_the_factory_column_sends_a_factory_user_live(monkeypatch):
    monkeypatch.setattr(aggregates, "available", lambda name: __import__("datetime").datetime.now())
    monkeypatch.setattr(aggregates, "load_specs", lambda: [{"name": "pcd_history", "scope_column": "Factory"}])
    monkeypatch.setattr(aggregates, "has_column", lambda name, col: False)
    tool = {"name": "t", "aggregate": "pcd_history", "local_sql": "SELECT 1 FROM pcd_history", "sql": "SELECT Id FROM rag.vw_Orders"}
    assert tools._from_aggregate(tool, {}, _cat(), "", TAL) is None


# ---------------------------------------------------------------- caches and the answer
def test_cache_keys_differ_by_scope(monkeypatch):
    monkeypatch.setattr("ragbot.index_version.index_version", lambda: 1)
    assert orch._answer_key("Which form?", None, TAL) != orch._answer_key("Which form?", None, ALL)
    assert orch._answer_key("Which form?", None, TAL) == orch._answer_key("which form", None, Scope(factories=["tal"]))

    from ragbot.data import cache
    runs = []
    monkeypatch.setattr(cache, "run", lambda *a, **k: runs.append(1) or (["n"], [[1]]))
    monkeypatch.setattr(cache, "_log_sql", lambda *a, **k: None)
    cache.clear()
    for key in ("a", "b", "a"):
        cache.cached_run("sqlserver", "SELECT 1", {}, conn_env="X", display_sql="", scope_key=key)
    assert len(runs) == 2                                           # the third call hits scope "a"'s entry only


def test_a_denied_question_says_not_found_and_why(monkeypatch):
    import ragbot.index_version as iv
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "_route", lambda q, user: "data")
    monkeypatch.setattr(orch, "retrieve", lambda q, where=None, scope=None: [])
    monkeypatch.setattr(tools, "needs_clarification", lambda q: None)
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [QueryResult(
        database="DB", engine="sqlserver", views=[], sql="", columns=[], rows=[], denied=True, error="outside")])
    monkeypatch.setattr(orch, "_log", lambda a, user: a)
    a = orch.answer("How many RHL orders ship next week?", scope=TAL)
    assert a.not_found and a.text.startswith("System doesn't have the data.") and "access" in a.text
