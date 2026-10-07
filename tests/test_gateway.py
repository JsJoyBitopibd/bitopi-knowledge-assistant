"""The gateway scripts (data/gateway.py): table names rewritten through the linked server and nothing else, the link
login limited to the views' tables, every per-database statement guarded, and no password in any output."""
from datetime import date

import pytest
import sqlglot

from ragbot.data import gateway as gw
from ragbot.data.catalog import Catalog, Table, View

DEFN = """SELECT
  eo.ExportOrderID,
  CASE fr.Factory WHEN '06' THEN 'TAL' ELSE fr.Factory END AS Factory,
  buyer.ContactName AS Buyer,
  s.InvoicedQty
FROM dbo.ExportOrder eo
LEFT JOIN [dbo].[FileRef] fr ON fr.FileRefID = eo.FileRefID
LEFT JOIN dbo.Contact_Master buyer ON buyer.ContactID = fr.Buyer
LEFT JOIN (
  SELECT ei.ExportOrderID, SUM(ei.ExportQty) AS InvoicedQty
  FROM dbo.EI_ExportOrder ei
  GROUP BY ei.ExportOrderID
) s ON s.ExportOrderID = eo.ExportOrderID
WHERE eo.ExportOrderCategory = 'dbo.ExportOrder is not a table here'"""


def _cat(views, name="BitopiSplint", tables=()):
    return Catalog(name, "tsql", "SQLSERVER_CONN_BITOPISPLINT", [], list(views), tables=list(tables))


def _view(name, definition=DEFN, scope="Factory"):
    return View(name, "", "orders", ["ExportOrderID"], {"ExportOrderID": "", "Factory": ""}, definition=definition,
                scope_column=scope)


def test_view_tables_lists_each_table_once():
    assert gw.view_tables(DEFN) == ["dbo.ExportOrder", "dbo.FileRef", "dbo.Contact_Master", "dbo.EI_ExportOrder"]
    with pytest.raises(ValueError, match="no schema"):
        gw.view_tables("SELECT a FROM ExportOrder")
    with pytest.raises(ValueError, match="cross-database"):
        gw.view_tables("SELECT a FROM Other.dbo.T")
    with pytest.raises(ValueError, match="alias.column"):
        gw.view_tables("SELECT dbo.T.a FROM dbo.T")


def test_qualify_rewrites_tables_only():
    out = gw.qualify_tables(DEFN, "BITOPI_LIVE", "BitopiSplint")
    assert "FROM [BITOPI_LIVE].[BitopiSplint].[dbo].[ExportOrder] eo" in out
    assert "JOIN [BITOPI_LIVE].[BitopiSplint].[dbo].[FileRef] fr" in out                  # bracketed form too
    assert "FROM [BITOPI_LIVE].[BitopiSplint].[dbo].[EI_ExportOrder] ei" in out          # inside a derived table
    assert "'dbo.ExportOrder is not a table here'" in out                                # strings untouched
    assert "eo.ExportOrderID" in out and "fr.Factory" in out and "s.InvoicedQty" in out  # columns untouched
    tables = [[p.name for p in t.parts] for t in sqlglot.parse_one(out, read="tsql").find_all(sqlglot.exp.Table)]
    assert all(len(p) == 4 and p[0] == "BITOPI_LIVE" for p in tables)
    # a name that only starts like a table is left alone
    assert "[dbo].[ExportOrderBack]" in gw.qualify_tables("SELECT 1 AS x FROM dbo.ExportOrderBack b", "L", "D")


def test_gateway_views_script():
    cat = _cat([_view("rag.vw_OrderShipment"), _view("rag.vw_Plain", "SELECT a FROM dbo.T", scope=None)])
    sql = gw.render_gateway_views("BITOPI_LIVE", "RAG_LIVE", {"BitopiSplint": cat}, "rag_reader")
    assert "IF DB_ID(N'RAG_LIVE') IS NULL CREATE DATABASE [RAG_LIVE];" in sql
    assert sql.count("CREATE OR ALTER VIEW rag.") == 2
    assert "SESSION_CONTEXT(N'rag_factories')" in sql.split("rag.[vw_OrderShipment]")[1].split("GO")[0]
    assert "SESSION_CONTEXT" not in sql.split("rag.[vw_Plain]")[1].split("GO")[0]
    assert "GRANT SELECT ON SCHEMA::[rag] TO [rag_reader];" in sql
    with pytest.raises(ValueError, match="two views named"):
        gw.render_gateway_views("L", "G", {"A": _cat([_view("rag.vw_X")], "A"), "B": _cat([_view("rag.vw_X")], "B")}, "r")


def test_link_login_is_narrow_and_guarded():
    contact = Table("dbo.Contact_Master", columns=[{"name": "ContactName"}, {"name": "Phone", "sensitive": True}])
    cat = _cat([_view("rag.vw_OrderShipment")], tables=[contact])
    sql = gw.render_link_login("rag_link", {"BitopiSplint": cat}, "the live server")
    assert "CREATE LOGIN [rag_link] WITH PASSWORD = N'$(RAG_LINK_PASSWORD)'" in sql
    assert "SELECT ALL USER SECURABLES" not in sql and "CONNECT ANY DATABASE" not in sql
    assert sql.count("GRANT SELECT ON OBJECT::") == 4                       # exactly the view's four tables
    assert "IF OBJECT_ID(N''[dbo].[FileRef]'') IS NOT NULL GRANT SELECT ON OBJECT::[dbo].[FileRef] TO [rag_link];" in sql
    assert "DENY SELECT ON OBJECT::[dbo].[Contact_Master] ([Phone]) TO [rag_link];" in sql
    assert "IF DB_ID(N'BitopiSplint') IS NULL" in sql and "USE [BitopiSplint]" not in sql


def test_linked_server_maps_only_the_reader_and_dba():
    sql = gw.render_linked_server("BITOPI_LIVE", "10.0.0.1", "rag_link", "rag_reader")
    assert "@datasrc = N'10.0.0.1'" in sql and "'rpc out', 'false'" in sql and "'data access', 'true'" in sql
    assert "sp_droplinkedsrvlogin @rmtsrvname = N'BITOPI_LIVE', @locallogin = NULL" in sql
    assert sql.count("sp_addlinkedsrvlogin") == 2 and "@locallogin = N'rag_reader'" in sql
    assert "@rmtpassword = N'$(RAG_LINK_PASSWORD)'" in sql


def test_speed_check_runs_tools_like_the_app():
    cat = _cat([_view("rag.vw_OrderShipment")])
    tool = {"name": "late_by_factory", "database": "BitopiSplint", "heavy": True,
            "sql": "SELECT TOP (200) ExportOrderID FROM rag.vw_OrderShipment WHERE Factory = @factory "
                   "AND ShipDate >= @from AND Buyer <> 'O''Neil'"}
    raw = {"name": "raw_read", "database": "BitopiSplint", "sql": "SELECT TOP (5) a FROM dbo.T"}
    todo = [(tool, {"factory": "TAL", "from": date(2026, 9, 7), "window": "last 30 days"}), (raw, {})]
    gate = gw.render_speed_check(todo, {"BitopiSplint": cat}, via_gateway=True, gateway_db="RAG_LIVE",
                                 server_label="GATEWAY", limits=(10, 30))
    assert "USE [RAG_LIVE];" in gate and "@value = N'*'" in gate
    assert "N'@factory nvarchar(4000), @from date'" in gate and "@factory = N'TAL', @from = '2026-09-07'" in gate
    assert "Buyer <> ''O''''Neil''" in gate                                  # quotes doubled inside sp_executesql
    assert "-- skipped raw_read: reads dbo.T directly" in gate
    assert "late_by_factory (heavy)" in gate
    direct = gw.render_speed_check(todo, {"BitopiSplint": cat}, via_gateway=False, gateway_db="RAG_LIVE",
                                   server_label="LIVE", limits=(10, 30))
    assert "USE [BitopiSplint];" in direct and "WITH rag_vw_OrderShipment AS" in direct
    assert "raw_read" in direct and "-- skipped" not in direct
    with pytest.raises(ValueError, match="no value for @from"):
        gw.timed_exec("x", "SELECT 1 WHERE d >= @from", {})


def test_no_secret_in_any_script():
    cat = _cat([_view("rag.vw_OrderShipment")])
    texts = [gw.render_link_login("rag_link", {"B": cat}, "live"), gw.render_linked_server("L", "h", "rag_link", "r"),
             gw.render_gateway_views("L", "G", {"B": cat}, "r"), gw.render_rollback_live("rag_link", {"B": cat}),
             gw.render_rollback_gateway("L", "G")]
    for t in texts:
        assert "PWD=" not in t and "Password=" not in t
        assert "PASSWORD = N'" not in t or "$(RAG_LINK_PASSWORD)" in t
