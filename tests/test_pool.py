"""B6: SQL Server connection pool — reuse, rollback on every release (the write barrier), broken
connections never reused, one retry on a dead pooled session, fetchmany row cap, heavy timeout tier.
Offline: a fake `pyodbc` module stands in for the driver."""
import sys
import types

import pytest

import ragbot.data.connectors as con
from ragbot.auth.models import Scope


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.description = conn, None

    def execute(self, sql, args=()):
        self.conn.executed.append(sql)
        if self.conn.fail_with is not None:
            e, self.conn.fail_with = self.conn.fail_with, None
            raise e
        self.description = [("n",)] if sql.lstrip().upper().startswith("SELECT") else None

    def fetchmany(self, n):
        self.conn.fetch_sizes.append(n)
        return [(i,) for i in range(min(n, 500))]   # the "table" has 500 rows

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.executed, self.fetch_sizes = [], []
        self.rollbacks = self.closes = 0
        self.fail_with = None
        self.timeout = None

    def cursor(self):
        return FakeCursor(self)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


@pytest.fixture
def driver(monkeypatch):
    conns = []

    def connect(cs, timeout=None, autocommit=True, readonly=False):
        assert autocommit is False and readonly is True       # read-only discipline on every connection
        c = FakeConn()
        conns.append(c)
        return c

    monkeypatch.setitem(sys.modules, "pyodbc", types.SimpleNamespace(connect=connect))
    monkeypatch.setenv("SQLSERVER_CONN_TEST", "Driver=fake")
    monkeypatch.setattr(con, "_log", lambda *a, **kw: None)
    con._POOLS.clear()
    yield conns
    con._POOLS.clear()


def _run(**kw):
    return con.run_sqlserver("SELECT n FROM rag.vw_T WHERE id = @id", {"id": 1}, conn_env="SQLSERVER_CONN_TEST", **kw)


def test_connection_is_reused_and_rolled_back_every_time(driver):
    _run()
    _run()
    assert len(driver) == 1                                   # one connect for two queries
    c = driver[0]
    assert c.rollbacks == 2 and c.closes == 0                 # rollback after each statement, kept open
    assert sum("NOCOUNT" in s for s in c.executed) == 1       # session settings once per connection


def test_failed_statement_closes_the_connection(driver):
    _run()
    driver[0].fail_with = Exception("42S02", "Invalid object name")
    with pytest.raises(Exception):
        _run()
    assert driver[0].rollbacks == 2 and driver[0].closes == 1
    _run()
    assert len(driver) == 2                                   # the broken connection was not reused


def test_dead_pooled_session_is_retried_once_on_a_fresh_connection(driver):
    _run()
    driver[0].fail_with = Exception("08S01", "Communication link failure")
    cols, rows = _run()
    assert cols == ["n"] and rows and len(driver) == 2 and driver[0].closes == 1


def test_timeout_is_not_retried(driver):
    _run()
    driver[0].fail_with = Exception("HYT00", "Query timeout expired")
    with pytest.raises(Exception):
        _run()
    assert len(driver) == 1


def test_rows_are_capped_with_fetchmany(driver):
    _, rows = _run()
    assert driver[0].fetch_sizes == [200] and len(rows) == 200   # data.max_rows from settings


def test_heavy_fixed_tool_gets_longer_timeout(monkeypatch):
    import ragbot.data.tools as tools
    seen = {}

    def fake_cached_run(engine, sql_exec, params, **kw):
        seen["timeout"] = kw.get("timeout")
        from datetime import datetime
        return ["n"], [[1]], datetime.now()

    monkeypatch.setattr(tools, "cached_run", fake_cached_run)
    monkeypatch.setattr(tools, "rewrite_virtual", lambda sql, cat, scope=None: (sql, []))
    cat = types.SimpleNamespace(engine="sqlserver", connection_env="SQLSERVER_CONN_TEST", database="DB",
                                view=lambda n: None)
    base = {"database": "DB", "sql": "SELECT 1", "name": "t"}
    tools.run_fixed_tool({**base, "heavy": True}, {}, {"DB": cat}, scope=Scope.unrestricted())
    assert seen["timeout"] == 30
    tools.run_fixed_tool(base, {}, {"DB": cat}, scope=Scope.unrestricted())
    assert seen["timeout"] is None                             # default tier
