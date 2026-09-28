"""A bare name is a CTE reference only where SQL binds it to one. SQL Server and MySQL resolve a name
inside a CTE to CTEs declared *before* it; a later CTE's name (or the CTE's own) falls through to a real
table in the login's default schema. The guard used to accept any bare name that matched a CTE anywhere
in the statement, so `WITH a AS (SELECT ... FROM ExportOrder), ExportOrder AS (...) SELECT ... FROM a`
read dbo.ExportOrder past the view allow-list, the raw-table rules and the user's scope (found in the
F1 review, 2026-09-28)."""
import pytest

from ragbot.auth.models import Scope
from ragbot.data import tools
from ragbot.data.catalog import Catalog, View, load_catalogs
from ragbot.data.guard import GuardError, guard
from ragbot.data.virtual import assert_scoped, rewrite_virtual

VIEWS = {"rag.vw_exportorder"}
ATTACK = ("WITH a AS (SELECT TOP (5) ExportOrderID, OrderQty FROM ExportOrder), "
          "ExportOrder AS (SELECT 1 AS ExportOrderID, 1 AS OrderQty) SELECT TOP (5) ExportOrderID, OrderQty FROM a")


@pytest.mark.parametrize("sql", [
    ATTACK,
    "WITH a AS (SELECT TOP (5) Id FROM a) SELECT TOP (5) Id FROM a",                          # its own name
    "WITH a AS (SELECT TOP (5) Id FROM b), b AS (SELECT Id FROM rag.vw_ExportOrder) SELECT TOP (5) Id FROM a",
])
def test_a_name_used_before_its_cte_is_defined_is_refused(sql):
    with pytest.raises(GuardError, match="before the CTE"):
        guard(sql, VIEWS, "tsql", 200)


def test_a_with_inside_a_subquery_is_refused():
    with pytest.raises(GuardError, match="only one WITH"):
        guard("SELECT TOP (5) x FROM (WITH a AS (SELECT 1 AS x) SELECT x FROM a) s", VIEWS, "tsql", 200)


@pytest.mark.parametrize("sql", [
    "WITH a AS (SELECT ExportOrderID FROM rag.vw_ExportOrder), b AS (SELECT ExportOrderID FROM a) "
    "SELECT TOP (5) ExportOrderID FROM b",
    "WITH a AS (SELECT ExportOrderID FROM rag.vw_ExportOrder) SELECT TOP (5) ExportOrderID FROM a "
    "WHERE ExportOrderID IN (SELECT ExportOrderID FROM a)",
])
def test_ctes_used_after_they_are_defined_still_pass(sql):
    guard(sql, VIEWS, "tsql", 200)


def test_the_attack_is_refused_for_every_user():
    cat = load_catalogs()["BitopiSplint"]
    for scope in (Scope(factories=["TAL"]), Scope.unrestricted()):
        with pytest.raises(GuardError):
            tools._guard_for(ATTACK, cat, 200, scope)


def test_the_self_check_refuses_a_bare_base_table_name_even_if_the_guard_missed_it():
    v = View(name="rag.vw_Orders", grain="g", description="d", key_columns=["Id"], columns={"Id": "", "Factory": ""},
             definition="SELECT o.Id, o.Factory FROM dbo.Orders o", scope_column="Factory")
    cat = Catalog(database="DB", dialect="tsql", connection_env="X", rules=[], views=[v])
    scope = Scope(factories=["TAL"])
    ok, _ = rewrite_virtual(guard("SELECT TOP (5) Id FROM rag.vw_Orders", cat.view_names, "tsql", 200), cat, scope=scope)
    assert_scoped(ok, cat, scope, [v])                              # the normal statement passes
    tampered = ok.replace("SELECT TOP (5) Id FROM rag_vw_Orders", "SELECT TOP (5) Id FROM Orders")
    with pytest.raises(GuardError, match="not a CTE"):
        assert_scoped(tampered, cat, scope, [v])
    outside = ok + " UNION ALL SELECT Id FROM dbo.Orders"
    with pytest.raises(GuardError, match="outside"):
        assert_scoped(outside, cat, scope, [v])
