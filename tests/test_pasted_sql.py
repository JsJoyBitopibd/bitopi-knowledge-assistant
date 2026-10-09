"""P1: a question that IS a SQL statement runs as written, under the guard, against the catalog that offers its
tables — it is not scored on its words ("ORDER BY" sent one to the order agent on 8 Oct 2026) nor rewritten."""
from datetime import datetime

import pytest

import ragbot.data.tools as tools
from ragbot.agent.rewrite import _needs_rewrite
from ragbot.agent.router import route
from ragbot.auth.models import Scope
from ragbot.data.catalog import Catalog, Table
from ragbot.data.db_router import _strip_sql, keyword_score
from ragbot.domain_agents import Agent
from ragbot.domain_agents.registry import keyword_hits

PASTED = ("select a.GRNID,ItemCode,dbo.fnGetBaseQty(UOM,grnqty) baseqty,grnqty ,UOM from GRNChild a  join GRNMaster b "
          "on a.GRNID=b.GRNID\nwhere ItemCode='1178108' and CompanyID='02'\norder by GRNDate")


def _cats():
    col = lambda n: {"name": n, "type": "varchar"}  # noqa: E731
    inv = Catalog("Inventory", "tsql", "X", [], [], keywords=["grn", "item"], tables=[
        Table("dbo.GRNChild", rows=10, columns=[col("GRNID"), col("ItemCode"), col("GRNQty"), col("UOM")]),
        Table("dbo.GRNMaster", rows=10, columns=[col("GRNID"), col("CompanyID"), col("GRNDate")])])
    orders = Catalog("BitopiSplint", "tsql", "X", [], [], keywords=["order", "orders"], tables=[
        Table("dbo.ExportOrder", rows=10, columns=[col("ExportOrderID")])])
    return {"BitopiSplint": orders, "Inventory": inv}


def test_sql_words_do_not_score_and_table_names_do():
    cats = _cats()
    assert "order" not in _strip_sql(PASTED).lower().split()
    assert keyword_score(PASTED, cats["BitopiSplint"]) == 0
    assert keyword_hits(PASTED, Agent(name="order", title="Order", keywords=("order", "orders"))) == 0


def test_a_statement_is_data_and_is_never_rewritten():
    assert route(PASTED) == "data"
    assert not _needs_rewrite("select this, that from dbo.X where it = 1")


def test_runs_as_written_against_the_catalog_that_offers_its_tables(monkeypatch):
    ran = []
    monkeypatch.setattr(tools, "cached_run", lambda engine, sql, params, **kw: ran.append((kw.get("database"), sql))
                        or (["GRNID"], [["GL-02-0611-065"]], datetime.now()))
    monkeypatch.setattr(tools, "log_generation", lambda **kw: None)
    r = tools.run_pasted(PASTED, _cats(), scope=Scope.unrestricted())
    assert r.error is None and r.database == "Inventory" and r.tool == "pasted"
    assert r.views == ["dbo.GRNChild", "dbo.GRNMaster"] and r.databases_considered == ["Inventory"]
    assert "from dbo.GRNChild a" in r.sql and "join dbo.GRNMaster b" in r.sql     # bare names qualified
    assert r.sql.upper().startswith("SELECT TOP (200)")                            # the guard's row cap


def test_a_statement_nobody_offers_is_an_error_not_a_guess(monkeypatch):
    monkeypatch.setattr(tools, "log_generation", lambda **kw: None)
    r = tools.run_pasted("select * from dbo.Salaries", _cats(), scope=Scope.unrestricted())
    assert r.error and r.rows == [] and r.databases_considered == []


def test_a_restricted_user_cannot_run_a_raw_table_statement(monkeypatch):
    monkeypatch.setattr(tools, "log_generation", lambda **kw: None)
    r = tools.run_pasted(PASTED, _cats(), scope=Scope(factories=["TAL"]))
    assert r.error and "schema not allowed" in r.error and r.rows == []


def test_answer_from_data_takes_the_pasted_path(monkeypatch):
    monkeypatch.setattr(tools, "load_catalogs", lambda folder=None: _cats())
    monkeypatch.setattr(tools, "run_pasted", lambda q, cats, user="", refresh=False, scope=None: "pasted")
    assert tools.answer_from_data(PASTED, scope=Scope.unrestricted()) == ["pasted"]
