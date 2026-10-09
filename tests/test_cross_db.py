"""P3: same-server catalogs are siblings only when the setting is on; the prompt says so; a three-part table is
cited with its database; a restricted user never crosses databases; the VistaQ pair never joins the rest."""
from datetime import datetime

import pytest

import ragbot.data.connectors as cx
import ragbot.data.schema_index as si
import ragbot.data.tools as tools
import ragbot.llm.base as base
from ragbot.auth.models import Scope
from ragbot.data.catalog import Catalog, Table

from test_schema_rag import ScriptedSQL, Settings

ENV = {"SQLSERVER_CONN_PRODUCTION": "Driver={x};Server=RND\\MSSQL2019;Database=Production;UID=u;PWD=p",
       "SQLSERVER_CONN_BITOPISPLINT": "Driver={x};Server=rnd\\mssql2019;Database=BitopiSplint;UID=u;PWD=p",
       "SQLSERVER_CONN_VISTAQ": "Driver={x};Server=VISTAQ;Database=VistaQ_Default_RHL;UID=u;PWD=p"}


def _cats():
    col = lambda n: {"name": n, "type": "varchar"}  # noqa: E731
    inv = Catalog("Inventory", "tsql", "SQLSERVER_CONN_PRODUCTION", [], [], connection_database="Inventory",
                  keywords=["grn", "item"], tables=[
                      Table("dbo.GRNMaster", rows=10, primary_key=["GRNID"], columns=[col("GRNID"), col("CompanyID")])])
    sm = Catalog("SystemManager", "tsql", "SQLSERVER_CONN_PRODUCTION", [], [], connection_database="SystemManager",
                 keywords=["company"], tables=[
                     Table("dbo.Company", rows=12, primary_key=["CompanyID"], columns=[col("CompanyID"), col("CompanyName")])])
    bs = Catalog("BitopiSplint", "tsql", "SQLSERVER_CONN_BITOPISPLINT", [], [], keywords=["order"], tables=[
        Table("dbo.SupplierData", rows=5, primary_key=["SupplierID"], columns=[col("SupplierID")])])
    vq = Catalog("VistaQ_Default_RHL", "tsql", "SQLSERVER_CONN_VISTAQ", [], [], connection_database="VistaQ_Default_RHL",
                 keywords=["dhu"], tables=[Table("dbo.QCSummary", rows=5, columns=[col("Line")])])
    return {"Inventory": inv, "SystemManager": sm, "BitopiSplint": bs, "VistaQ_Default_RHL": vq}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(cx, "env", lambda name: ENV[name])


def test_siblings_need_the_setting_and_the_same_instance(env, monkeypatch):
    cats = _cats()
    monkeypatch.setattr(tools, "settings", lambda: Settings(**{"data.cross_database_names": False}))
    assert tools.siblings_of(cats["Inventory"], cats) == {}
    monkeypatch.setattr(tools, "settings", lambda: Settings(**{"data.cross_database_names": True}))
    sibs = tools.siblings_of(cats["Inventory"], cats)
    assert set(sibs) == {"SystemManager", "BitopiSplint"}            # the second login line names the same instance
    assert "VistaQ_Default_RHL" not in sibs and tools.siblings_of(cats["VistaQ_Default_RHL"], cats) == {}
    assert set(tools.siblings_of(cats["BitopiSplint"], cats)) == {"Inventory", "SystemManager"}   # Production = its line's Database


def test_three_part_tables_are_cited_with_their_database_and_keyed_through_the_sibling(env, monkeypatch):
    cats = _cats()
    monkeypatch.setattr(tools, "settings", lambda: Settings(**{"data.cross_database_names": True}))
    sibs = tools.siblings_of(cats["Inventory"], cats)
    sql = "SELECT TOP (20) g.GRNID, c.CompanyName FROM dbo.GRNMaster g JOIN SystemManager.dbo.Company c ON c.CompanyID = g.CompanyID"
    views = tools._views_in(sql, cats["Inventory"], sibs)
    assert views == ["dbo.GRNMaster", "SystemManager.dbo.Company"]
    assert tools._keys_for(views, cats["Inventory"], sibs) == ["GRNID", "CompanyID"]
    assert tools._views_in(sql, cats["Inventory"], None) == ["dbo.GRNMaster"]      # off: the foreign table is unknown


@pytest.fixture
def wired(monkeypatch, env):
    si._CACHE.clear()
    tools._SQL.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr("ragbot.data.schema_usage.log_selection", lambda *a, **k: None)
    monkeypatch.setattr(tools, "log_generation", lambda **kw: None)
    monkeypatch.setattr(tools, "catalog_stamp", lambda folder=None: ("stamp",))
    monkeypatch.setattr(si, "json_sha", lambda cat: "x")
    monkeypatch.setattr(si, "vectors_path", lambda db: __import__("pathlib").Path("does-not-exist.npz"))
    import ragbot.retrieve.retriever as rt
    monkeypatch.setattr(rt, "_embed_query", lambda q: (_ for _ in ()).throw(RuntimeError("no model")))
    ran = []
    monkeypatch.setattr(tools, "cached_run", lambda engine, sql, params, **kw: ran.append(sql) or
                        (["GRNID", "CompanyName"], [["GL-1", "MISAMI Garments Ltd."]], datetime.now()))

    def install(replies, **over):
        monkeypatch.setattr(tools, "settings", lambda: Settings(**over))
        chat = ScriptedSQL(replies)
        monkeypatch.setattr(tools, "get_chat", lambda: chat)
        return chat, ran
    yield install
    tools._SQL.clear()


JOIN = ("DATABASE: Inventory\nSELECT TOP (20) g.GRNID, c.CompanyName FROM dbo.GRNMaster g "
        "JOIN SystemManager.dbo.Company c ON c.CompanyID = g.CompanyID")


def test_with_the_setting_on_a_same_server_join_runs_and_the_prompt_says_so(wired):
    chat, ran = wired([JOIN], **{"data.cross_database_names": True})
    cats = {k: v for k, v in _cats().items() if k in ("Inventory", "SystemManager")}
    r = tools.generate_and_run("GRN ids with the company name", cats, scope=Scope.unrestricted())
    assert r.error is None and r.views == ["dbo.GRNMaster", "SystemManager.dbo.Company"] and ran
    assert "are on one server" in chat.systems[0] and "Inventory, SystemManager" in chat.systems[0]


def test_with_the_setting_off_the_same_statement_is_refused_and_the_prompt_forbids_it(wired):
    chat, ran = wired([JOIN, JOIN], **{"data.cross_database_names": False})
    cats = {k: v for k, v in _cats().items() if k in ("Inventory", "SystemManager")}
    r = tools.generate_and_run("GRN ids with the company name", cats, scope=Scope.unrestricted())
    assert r.error and "three-part name not allowed" in r.error and not ran
    assert "Never with a database prefix." in chat.systems[0]


def test_a_restricted_user_never_crosses_databases(wired):
    chat, ran = wired([JOIN, JOIN], **{"data.cross_database_names": True})
    cats = {k: v for k, v in _cats().items() if k in ("Inventory", "SystemManager")}
    r = tools.generate_and_run("GRN ids with the company name", cats, scope=Scope(factories=["TAL"]))
    assert r.error and not ran                         # raw tables are not offered to a restricted scope at all
