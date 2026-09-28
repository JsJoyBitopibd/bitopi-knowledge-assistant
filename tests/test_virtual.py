"""Tests for the virtual-views rewrite (src/ragbot/data/virtual.py): expanding `rag.vw_X` references
into CTEs from the catalog's `definition:` fields, with no DDL ever run on the server."""
import re

import sqlglot
import pytest

from ragbot.data.catalog import Catalog, View
from ragbot.data.guard import GuardError, guard
from ragbot.data.tools import load_fixed_tools, match_fixed_tool
from ragbot.data.virtual import cte_name, rewrite_virtual, validate_definition
from ragbot.auth.models import Scope


def make_catalog(**views_with_defs: str) -> Catalog:
    views = [View(name=f"rag.{n}", grain="one row", description="d", key_columns=["Id"],
                  columns={"Id": "id"}, definition=defn)
             for n, defn in views_with_defs.items()]
    return Catalog(database="TestDB", dialect="tsql", connection_env="TEST_CONN", rules=[], views=views)


DEF_A = "SELECT Id, Val FROM dbo.RealTableA"
DEF_B = "SELECT Id, Other FROM dbo.RealTableB"


# --------------------------------------------------------------------------- validate_definition


def test_validate_definition_accepts_plain_select():
    validate_definition(DEF_A, "tsql")


@pytest.mark.parametrize("bad_sql", [
    "UPDATE dbo.RealTableA SET Val = 1",
    "SELECT Id FROM dbo.RealTableA; DROP TABLE x",
    "SELECT Id FROM dbo.RealTableA WHERE Id = @x",
    "SELECT Id FROM dbo.RealTableA WHERE Id = ?",
    "WITH x AS (SELECT 1 a) SELECT a FROM x",
    "SELECT TOP (10) Id FROM dbo.RealTableA",
    "SELECT Id FROM dbo.RealTableA ORDER BY Id",
    "SELECT Id FROM rag.vw_Other",
    "SELECT Id FROM #TempTable",
])
def test_validate_definition_rejects(bad_sql):
    with pytest.raises(GuardError):
        validate_definition(bad_sql, "tsql")


# --------------------------------------------------------------------------- rewrite_virtual


def test_single_view_rewrite():
    cat = make_catalog(vw_A=DEF_A)
    safe = guard("SELECT Id, Val FROM rag.vw_A WHERE Id = 1", cat.view_names, cat.dialect, 200)
    sql_exec, views = rewrite_virtual(safe, cat, scope=Scope.unrestricted())
    assert views == ["rag.vw_A"]
    assert "WITH rag_vw_A AS" in sql_exec
    assert "FROM rag_vw_A" in sql_exec
    assert sqlglot.parse_one(sql_exec, read="tsql")  # must still parse


def test_two_views_with_model_cte_ordering():
    cat = make_catalog(vw_A=DEF_A, vw_B=DEF_B)
    guarded_sql = ("WITH recent AS (SELECT Id FROM rag.vw_A)\n"
                   "SELECT TOP (10) r.Id, b.Other FROM recent r JOIN rag.vw_B b ON b.Id = r.Id")
    safe = guard(guarded_sql, cat.view_names, cat.dialect, 200)
    sql_exec, views = rewrite_virtual(safe, cat, scope=Scope.unrestricted())
    assert set(views) == {"rag.vw_A", "rag.vw_B"}
    # our CTEs come first so the model's own CTE (which references vw_A) can see the rewritten name
    assert sql_exec.index("rag_vw_A AS") < sql_exec.index("recent AS")
    assert "FROM rag_vw_A" in sql_exec  # the model's CTE body got rewritten too
    assert sqlglot.parse_one(sql_exec, read="tsql")


def test_view_without_definition_left_qualified():
    cat = make_catalog(vw_A=DEF_A)
    cat.views.append(View(name="rag.vw_Real", grain="g", description="d", key_columns=[], columns={},
                          definition=None))
    safe = guard("SELECT Id FROM rag.vw_Real", cat.view_names, cat.dialect, 200)
    sql_exec, views = rewrite_virtual(safe, cat, scope=Scope.unrestricted())
    assert views == ["rag.vw_Real"]
    assert "rag.vw_Real" in sql_exec  # untouched: no matching CTE was injected
    assert "WITH" not in sql_exec.upper()


def test_bracket_quoted_reference():
    cat = make_catalog(vw_A=DEF_A)
    safe = guard("SELECT * FROM [rag].[vw_A]", cat.view_names, cat.dialect, 200)
    sql_exec, views = rewrite_virtual(safe, cat, scope=Scope.unrestricted())
    assert views == ["rag.vw_A"]
    assert "rag_vw_A" in sql_exec


def test_reserved_cte_name_collision():
    cat = make_catalog(vw_A=DEF_A)
    guarded_sql = "WITH rag_vw_A AS (SELECT 1 x) SELECT TOP (5) * FROM rag.vw_A, rag_vw_A"
    safe = guard(guarded_sql, cat.view_names, cat.dialect, 200)
    with pytest.raises(GuardError, match="reserved"):
        rewrite_virtual(safe, cat, scope=Scope.unrestricted())


def test_literal_containing_rag_dot_is_refused():
    cat = make_catalog(vw_A=DEF_A)
    safe = guard("SELECT Id FROM rag.vw_A WHERE Val = 'rag.vw_A'", cat.view_names, cat.dialect, 200)
    with pytest.raises(GuardError):
        rewrite_virtual(safe, cat, scope=Scope.unrestricted())


def test_cte_name_helper():
    assert cte_name("rag.vw_ExportOrder") == "rag_vw_ExportOrder"


# --------------------------------------------------------------------------- real catalogs


def test_real_catalogs_load_and_validate():
    from ragbot.data.catalog import load_catalogs
    cats = load_catalogs()
    assert "BitopiSplint" in cats and "Production" in cats
    for cat in cats.values():
        for v in cat.views:
            if v.definition:
                validate_definition(v.definition, cat.dialect)


def test_real_fixed_tool_patterns_compile():
    """A regex that fails to compile breaks match_fixed_tool() for EVERY question, not just this
    tool's — re.search() is called unconditionally down the whole tool list. Python 3.11 rejects an
    inline (?i) flag anywhere but the very start of the pattern (a bug this once caught)."""
    tools = load_fixed_tools()
    assert tools, "no fixed tools loaded"
    for tool in tools:
        re.compile(tool["match"])
        for r in tool.get("requires", []):
            re.compile(r)


def test_real_fixed_tools_do_not_crash_the_matcher():
    """Exercise match_fixed_tool with a representative question per known pattern group — this
    would have caught the (?i)-placement regression even without inspecting the regex source."""
    tools = load_fixed_tools()
    probes = [
        "What is the PCD for TAL-17-382-1?",
        "PCD for PO 12345?",
        "Show orders under FR-17-29",
        "Which TAL orders have PCD next week?",
        "RHL orders shipping this week",
        "How many BGL orders are shipping next week?",
        "PCD history for TAL-17-382-1",
        "PPM meetings for TAL today",
        "How many PPM meetings for RHL this week?",
        "What is the weather today?",  # should match nothing, and must not raise
    ]
    for q in probes:
        match_fixed_tool(q, tools)  # only asserts it does not raise


def test_real_fixed_tools_round_trip():
    """Every fixed tool's SQL must pass the guard and rewrite against its declared catalog."""
    import yaml
    from pathlib import Path
    from ragbot.data.catalog import load_catalogs

    cats = load_catalogs()
    tools = yaml.safe_load(Path("config/fixed_tools.yaml").read_text(encoding="utf-8"))
    for tool in tools:
        cat = cats[tool["database"]]
        safe = guard(tool["sql"], cat.view_names, cat.dialect, 200)
        sql_exec, views = rewrite_virtual(safe, cat, scope=Scope.unrestricted())
        assert views, f"{tool['name']}: no views detected"
        assert sqlglot.parse_one(sql_exec, read="tsql" if cat.dialect == "tsql" else "mysql")
