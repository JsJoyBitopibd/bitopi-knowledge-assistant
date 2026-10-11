"""Phase Q (Q4): the production dashboard reads every factory once and keeps the asked one in Python.

`(@factory = '' OR Factory = @factory)` inside five subqueries gave SQL Server a far worse plan than the plain
all-factory statement: 11 Oct 2026, all factories 8.1 s, TAL 30 s timeout on byte-identical SQL. Reading once
and filtering here is faster, and the cached result is shared by every factory of the same scope.
"""
import pytest

import ragbot.data.tools as tools
from ragbot.auth.models import Scope
from ragbot.data.tools import _apply_filter, _params_in, load_fixed_tools

DASH = "production_dashboard_window"
BY_LINE = "production_by_line_window"


def _tool(name):
    return next(t for t in load_fixed_tools() if t["name"] == name)


@pytest.fixture
def ran(monkeypatch):
    """Records what reached the result cache and returns three factories' rows."""
    calls = []

    def fake(engine, sql_exec, params, *, conn_env, display_sql, tool="generated", user="", refresh=False,
             timeout=None, scope_key="", database=None, ttl=None):
        from datetime import datetime
        calls.append({"sql_exec": sql_exec, "params": dict(params), "scope_key": scope_key, "ttl": ttl,
                      "tool": tool})
        cols = ["Date", "Factory", "Unit", "Line", "Styles"]
        rows = [["2026-10-11", "BGL", "U1", "101", 1], ["2026-10-11", "RHL", "U1", "201", 2],
                ["2026-10-11", "TAL", "U1", "2102", 1]]
        return cols, rows, datetime(2026, 10, 11, 9, 50)
    monkeypatch.setattr(tools, "cached_run", fake)
    return calls


def test_neither_dashboard_tool_still_filters_the_factory_in_sql():
    for name in (DASH, BY_LINE):
        assert "@factory" not in _tool(name)["sql"], name


def test_only_referenced_parameters_are_bound_so_one_read_serves_every_factory():
    sql = "SELECT 1 FROM rag.vw_PlanDay WHERE PlanDate >= @from AND PlanDate < @to"
    params = {"from": "a", "to": "b", "factory": "TAL", "window": "today"}
    assert _params_in(sql, params, "tsql") == {"from": "a", "to": "b"}
    assert _params_in(sql.replace("@", ":"), params, "mysql") == {"from": "a", "to": "b"}


def test_one_cached_read_serves_every_factory_and_the_rows_are_narrowed(ran, monkeypatch):
    from ragbot.data.catalog import Catalog
    cat = Catalog(database="VistaQ_Default_RHL", dialect="tsql", connection_env="X", rules=[], views=[])
    monkeypatch.setattr(tools, "rewrite_virtual", lambda sql, c, scope: (sql, ["rag.vw_PlanDay"]))
    tool = _tool(BY_LINE)
    out = {}
    for factory in ("", "TAL", "RHL"):
        p = {"from": "2026-10-11", "to": "2026-10-12", "window": "today", "factory": factory}
        out[factory] = tools.run_fixed_tool(tool, p, {"VistaQ_Default_RHL": cat}, scope=Scope.unrestricted())
    keys = {(c["sql_exec"], tuple(sorted(c["params"].items())), c["scope_key"]) for c in ran}
    assert len(keys) == 1, "the three questions must share one cache key"
    assert all(c["ttl"] == 600 for c in ran)
    assert "factory" not in ran[0]["params"]
    assert [r[1] for r in out["TAL"].rows] == ["TAL"] and [r[1] for r in out["RHL"].rows] == ["RHL"]
    assert len(out[""].rows) == 3
    # the full parameters stay on the result: the templated lead, the [D#] card and the citation check read them
    assert out["TAL"].params["factory"] == "TAL" and out["TAL"].params["window"] == "today"


def test_the_pivoted_dashboard_keeps_only_the_asked_factorys_column():
    tool = _tool(DASH)
    cols = ["Date", "Metric", "BGL", "RHL", "TAL", "All"]
    rows = [["2026-10-11", "Sewing output", 1, 2, 3, 6]]
    c, r = _apply_filter(tool, {"factory": "TAL"}, cols, rows)
    assert c == ["Date", "Metric", "TAL"] and r == [["2026-10-11", "Sewing output", 3]]
    c, r = _apply_filter(tool, {"factory": ""}, cols, rows)
    assert c == cols and r == rows


def test_a_missing_filter_column_raises_instead_of_returning_every_factory():
    with pytest.raises(KeyError):
        _apply_filter(_tool(BY_LINE), {"factory": "TAL"}, ["Date", "Line"], [["2026-10-11", "2102"]])


def test_a_tool_without_a_filter_is_untouched():
    cols, rows = ["a"], [[1]]
    assert _apply_filter({"name": "x"}, {"factory": "TAL"}, cols, rows) == (cols, rows)
