"""C6: pre-computed aggregates — atomic local copy, read-only lookups, fixed-tool routing and fallback."""
from datetime import date, datetime, timedelta
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


# ---------------------------------------------------------------- the refresh schedule (refresh_aggregates.py --at)
# `--every 24` refreshed on start and then every 24 hours, so a container recreated at 14:58 put the
# production read at ~15:13 every day. `--at 02:00` pins it to the clock.
def test_next_run_before_the_hour_is_today():
    assert agg.next_run("02:00", datetime(2026, 10, 15, 1, 30)) == datetime(2026, 10, 15, 2, 0)
    assert agg.next_run("02:00", datetime(2026, 10, 15, 1, 59, 59, 999999)) == datetime(2026, 10, 15, 2, 0)


def test_next_run_after_the_hour_is_tomorrow():
    assert agg.next_run("02:00", datetime(2026, 10, 15, 14, 58)) == datetime(2026, 10, 16, 2, 0)
    assert agg.next_run("02:00", datetime(2026, 10, 15, 2, 0, 0, 1)) == datetime(2026, 10, 16, 2, 0)


def test_next_run_at_the_hour_is_strictly_after():
    """A refresh that starts at 02:00:00 and asks again when it ends must wait a day, not run twice."""
    assert agg.next_run("02:00", datetime(2026, 10, 15, 2, 0)) == datetime(2026, 10, 16, 2, 0)


def test_next_run_across_midnight_and_month_and_year_ends():
    assert agg.next_run("00:15", datetime(2026, 10, 15, 23, 50)) == datetime(2026, 10, 16, 0, 15)
    assert agg.next_run("23:59", datetime(2026, 10, 15, 0, 0)) == datetime(2026, 10, 15, 23, 59)
    assert agg.next_run("02:00", datetime(2026, 10, 31, 23, 0)) == datetime(2026, 11, 1, 2, 0)
    assert agg.next_run("02:00", datetime(2026, 12, 31, 3, 0)) == datetime(2027, 1, 1, 2, 0)
    assert agg.next_run("2:00", datetime(2026, 10, 15, 1, 0)) == agg.next_run(" 02:00 ", datetime(2026, 10, 15, 1, 0))


@pytest.mark.parametrize("bad", ["24:00", "02:60", "2am", "0200", "", "02:00:00", "-1:00", "ab:cd", "02:5",
                                 "002:00", None])
def test_next_run_rejects_what_is_not_hh_mm(bad):
    with pytest.raises(ValueError):
        agg.next_run(bad, datetime(2026, 10, 15, 12, 0))


def test_seconds_until_the_next_run():
    assert agg.seconds_until("02:00", datetime(2026, 10, 15, 1, 30)) == 1800.0
    assert agg.seconds_until("02:00", datetime(2026, 10, 15, 2, 0)) == 86400.0
    assert agg.seconds_until("02:00", datetime(2026, 10, 15, 14, 58)) == 11 * 3600 + 2 * 60
    with pytest.raises(ValueError):
        agg.seconds_until("25:00", datetime(2026, 10, 15, 12, 0))


def test_sleep_until_reads_the_wall_clock_in_chunks_of_at_most_300_s():
    clock, slept = [datetime(2026, 10, 15, 14, 58)], []

    def sleep(s):
        slept.append(s)
        clock[0] += timedelta(seconds=s)

    agg.sleep_until(datetime(2026, 10, 16, 2, 0), clock=lambda: clock[0], sleep=sleep)
    assert max(slept) <= 300 and sum(slept) == 11 * 3600 + 2 * 60
    assert clock[0] == datetime(2026, 10, 16, 2, 0)


def test_sleep_until_ends_on_time_when_the_clock_jumps():
    """The host suspended for 3 h during one chunk: the wait still ends at 02:00 by the wall clock."""
    clock, slept = [datetime(2026, 10, 15, 22, 0)], []

    def sleep(s):
        slept.append(s)
        clock[0] += timedelta(seconds=s) + (timedelta(hours=3) if len(slept) == 1 else timedelta())

    agg.sleep_until(datetime(2026, 10, 16, 2, 0), clock=lambda: clock[0], sleep=sleep)
    assert clock[0] == datetime(2026, 10, 16, 2, 0) and sum(slept) == 3600


def test_sleep_until_a_time_already_passed_does_not_sleep():
    agg.sleep_until(datetime(2026, 10, 15, 2, 0), clock=lambda: datetime(2026, 10, 15, 2, 0),
                    sleep=lambda s: pytest.fail("slept"))


def test_missing_lists_the_copies_never_built(tmp_path):
    """At start `--at` builds only these, so a recreate does not re-read every copy from production."""
    db = tmp_path / "agg.db"
    specs = [{"name": "pcd_history"}, {"name": "company_master"}, {"name": "values_stores"}]
    assert agg.missing(specs, db) == ["pcd_history", "company_master", "values_stores"]   # no file yet
    agg.write("company_master", _batches(), [], [], db)
    assert agg.missing(specs, db) == ["pcd_history", "values_stores"]                     # spec order kept
    agg.write("pcd_history", _batches(), [], [], db)
    agg.write("values_stores", _batches(), [], [], db)
    assert agg.missing(specs, db) == [] and agg.missing([], db) == []


def test_a_failed_first_build_is_still_missing(tmp_path):
    db = tmp_path / "agg.db"

    def broken():
        yield COLS, [["TAL-1", 1, date(2026, 1, 1), None, "x", None]]
        raise ConnectionError("link dropped mid-scan")

    with pytest.raises(ConnectionError):
        agg.write("pcd_history", broken(), [], [], db)
    assert agg.missing([{"name": "pcd_history"}], db) == ["pcd_history"]


def test_missing_on_a_file_without_the_meta_table(tmp_path):
    import sqlite3
    db = tmp_path / "agg.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE other (a)")
    con.commit()
    con.close()
    assert agg.missing([{"name": "pcd_history"}], db) == ["pcd_history"]
