"""C6: pre-computed aggregates — atomic local copy, read-only lookups, fixed-tool routing and fallback."""
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

import ragbot.data.aggregates as agg
import ragbot.data.tools as tools
from ragbot.data.guard import GuardError, assert_read_only
from ragbot.auth.models import Scope

COLS = ["ExportOrderID", "VersionNo", "PCD", "ShipDate", "UpdatedBy", "DateUpdated"]


def _batches(n_orders=3, versions=4):
    rows = [[f"TAL-25-1493-{o}", v, date(2026, 9, 1 + v), datetime(2026, 10, 1 + v, 0, 0), "planner", date(2026, 8, v + 1)]
            for o in range(n_orders) for v in range(1, versions + 1)]
    yield COLS, rows[:5]
    yield COLS, rows[5:]


def test_write_then_lookup_and_as_of(tmp_path):
    db = tmp_path / "agg.db"
    assert agg.available("pcd_history", db) is None
    n = agg.write("pcd_history", _batches(), ["ExportOrderID"], ["rag.vw_PCDChangeHistory"], db)
    assert n == 12 and isinstance(agg.available("pcd_history", db), datetime)
    cols, rows = agg.run_local("SELECT VersionNo, PCD FROM pcd_history WHERE ExportOrderID = @eo ORDER BY VersionNo",
                               {"eo": "TAL-25-1493-1"}, path=db)
    assert cols == ["VersionNo", "PCD"] and rows == [[1, "2026-09-02"], [2, "2026-09-03"], [3, "2026-09-04"], [4, "2026-09-05"]]


def test_refresh_replaces_the_copy(tmp_path):
    db = tmp_path / "agg.db"
    agg.write("pcd_history", _batches(3, 4), ["ExportOrderID"], [], db)
    agg.write("pcd_history", _batches(1, 2), ["ExportOrderID"], [], db)      # second refresh: fewer rows
    _, rows = agg.run_local("SELECT COUNT(*) FROM pcd_history", {}, path=db)
    assert rows == [[2]]


def test_failed_refresh_keeps_the_previous_copy(tmp_path):
    db = tmp_path / "agg.db"
    agg.write("pcd_history", _batches(), ["ExportOrderID"], [], db)
    before = agg.available("pcd_history", db)

    def broken():
        yield COLS, [["TAL-1", 1, date(2026, 1, 1), None, "x", None]]
        raise ConnectionError("link dropped mid-scan")

    with pytest.raises(ConnectionError):
        agg.write("pcd_history", broken(), ["ExportOrderID"], [], db)
    _, rows = agg.run_local("SELECT COUNT(*) FROM pcd_history", {}, path=db)
    assert rows == [[12]] and agg.available("pcd_history", db) == before


def test_local_copy_is_opened_read_only(tmp_path):
    db = tmp_path / "agg.db"
    agg.write("pcd_history", _batches(), [], [], db)
    with pytest.raises(Exception):
        agg.run_local("DELETE FROM pcd_history", {}, path=db)


@pytest.mark.parametrize("bad", ["DELETE FROM rag.vw_X", "SELECT 1; DROP TABLE x", "EXEC sp_who",
                                 "SELECT * INTO #t FROM rag.vw_X"])
def test_aggregate_sql_must_be_one_read_only_select(bad):
    with pytest.raises(GuardError):
        assert_read_only(bad)


def test_real_aggregate_config_is_read_only():
    for spec in agg.load_specs():
        assert_read_only(spec["sql"])


def _cat():
    from ragbot.data.catalog import Catalog, View
    v = View("rag.vw_PCDChangeHistory", "", "", ["ExportOrderID", "VersionNo"], {})
    return Catalog("BitopiSplint", "tsql", "SQLSERVER_CONN_BITOPISPLINT", [], [v])


TOOL = {"name": "pcd_history_by_eo", "database": "BitopiSplint", "aggregate": "pcd_history", "heavy": True,
        "local_sql": "SELECT VersionNo, PCD FROM pcd_history WHERE ExportOrderID = @eo ORDER BY VersionNo",
        "sql": "SELECT TOP (50) VersionNo, PCD FROM rag.vw_PCDChangeHistory WHERE ExportOrderID = @eo"}


def test_fixed_tool_answers_from_the_copy_and_refresh_goes_live(tmp_path, monkeypatch):
    db = tmp_path / "agg.db"
    agg.write("pcd_history", _batches(), ["ExportOrderID"], [], db)
    monkeypatch.setattr(agg, "db_path", lambda: db)
    import ragbot.data.connectors as con
    monkeypatch.setattr(con, "_log", lambda *a, **k: None)
    live = []
    monkeypatch.setattr(tools, "rewrite_virtual", lambda sql, cat, scope=None: (sql, ["rag.vw_PCDChangeHistory"]))
    monkeypatch.setattr(tools, "cached_run", lambda *a, **k: live.append(k) or (["VersionNo"], [[9]], datetime.now()))
    cats = {"BitopiSplint": _cat()}

    r = tools.run_fixed_tool(TOOL, {"eo": "TAL-25-1493-0"}, cats, scope=Scope.unrestricted())
    assert r.engine == "local" and len(r.rows) == 4 and r.as_of == agg.available("pcd_history", db) and not live
    assert r.views == ["rag.vw_PCDChangeHistory"]

    r = tools.run_fixed_tool(TOOL, {"eo": "TAL-25-1493-0"}, cats, refresh=True, scope=Scope.unrestricted())     # Refresh data: live
    assert r.engine == "sqlserver" and live and live[0]["timeout"] == 30


def test_no_copy_yet_falls_back_to_live(tmp_path, monkeypatch):
    monkeypatch.setattr(agg, "db_path", lambda: tmp_path / "missing.db")
    monkeypatch.setattr(tools, "rewrite_virtual", lambda sql, cat, scope=None: (sql, []))
    monkeypatch.setattr(tools, "cached_run", lambda *a, **k: (["VersionNo"], [[1]], datetime.now()))
    r = tools.run_fixed_tool(TOOL, {"eo": "X"}, {"BitopiSplint": _cat()}, scope=Scope.unrestricted())
    assert r.engine == "sqlserver" and r.rows == [[1]]


def test_lookups_are_case_insensitive_like_sql_server(tmp_path):
    """The source data has 'tal-22-523-81' and 'TAL-22-523-81' for the same order; SQL Server's default
    collation matches both, so the copy must too (found 2026-09-27: 9 rows live vs 7 from the copy)."""
    db = tmp_path / "agg.db"
    rows = [["tal-22-523-81", 1, "2023-04-11"], ["TAL-22-523-81", 2, "2023-05-01"], ["TAL-22-523-82", 1, "2023-06-01"]]
    agg.write("h", iter([(["ExportOrderID", "VersionNo", "PCD"], rows)]), ["ExportOrderID"], [], db)
    _, got = agg.run_local("SELECT VersionNo FROM h WHERE ExportOrderID = @eo ORDER BY VersionNo", {"eo": "TAL-22-523-81"}, path=db)
    assert got == [[1], [2]]
    _, typed = agg.run_local("SELECT typeof(VersionNo) FROM h LIMIT 1", {}, path=db)
    assert typed == [["integer"]]                   # no text affinity: numbers stay numbers
