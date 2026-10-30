"""Phase T4: the guard's paging rule (max_offset) and data/paging.py."""
import re

import pytest
import sqlglot

from ragbot.data import tools as T
from ragbot.data.guard import GuardError, guard
from ragbot.data.paging import page_sql

V = {"rag.vw_ExportOrder"}


# ---------------------------------------------------------------- the guard
@pytest.mark.parametrize("dialect,sql", [
    ("tsql", "SELECT ExportOrderID FROM rag.vw_ExportOrder ORDER BY PCD OFFSET 200 ROWS FETCH NEXT 200 ROWS ONLY"),
    ("tsql", "SELECT ExportOrderID FROM rag.vw_ExportOrder ORDER BY PCD OFFSET 5 ROWS"),
    ("mysql", "SELECT ExportOrderID FROM rag.vw_ExportOrder ORDER BY PCD LIMIT 200 OFFSET 400"),
    ("mysql", "SELECT ExportOrderID FROM rag.vw_ExportOrder ORDER BY PCD LIMIT 400, 200"),
])
def test_an_offset_is_refused_by_default(dialect, sql):
    """Generated, pasted and replayed statements never page themselves: only data/paging.py asks for an OFFSET."""
    with pytest.raises(GuardError, match="OFFSET is not allowed"):
        guard(sql, V, dialect)


def test_a_fetch_count_is_held_to_the_row_cap():
    """Before T4 sqlglot kept the FETCH count where the cap check did not look, so this passed uncapped."""
    big = "SELECT ExportOrderID FROM rag.vw_ExportOrder ORDER BY PCD OFFSET 0 ROWS FETCH NEXT 100000 ROWS ONLY"
    with pytest.raises(GuardError):
        guard(big, V, "tsql")
    with pytest.raises(GuardError, match="exceeds the maximum"):
        guard(big, V, "tsql", max_offset=2000)


def test_a_page_passes_with_order_literal_offset_and_count():
    ok = "SELECT ExportOrderID FROM rag.vw_ExportOrder ORDER BY PCD OFFSET 200 ROWS FETCH NEXT 200 ROWS ONLY"
    assert guard(ok, V, "tsql", max_offset=2000) == ok                      # no TOP added: FETCH is the cap
    assert guard("SELECT ExportOrderID FROM rag.vw_ExportOrder ORDER BY PCD LIMIT 200 OFFSET 400", V, "mysql",
                 max_offset=2000).endswith("LIMIT 200 OFFSET 400")


@pytest.mark.parametrize("sql,why", [
    ("SELECT ExportOrderID FROM rag.vw_ExportOrder ORDER BY PCD OFFSET 4000 ROWS FETCH NEXT 200 ROWS ONLY", "beyond"),
    ("SELECT ExportOrderID FROM rag.vw_ExportOrder OFFSET 200 ROWS FETCH NEXT 200 ROWS ONLY", "ORDER BY"),
    ("SELECT ExportOrderID FROM rag.vw_ExportOrder ORDER BY PCD OFFSET 200 ROWS", "row count"),
    ("SELECT ExportOrderID FROM rag.vw_ExportOrder ORDER BY PCD OFFSET 200 ROWS FETCH NEXT 10 PERCENT ROWS ONLY", "PERCENT"),
])
def test_a_bad_page_is_refused(sql, why):
    with pytest.raises(GuardError, match=why):
        guard(sql, V, "tsql", max_offset=2000)


def test_every_other_rule_still_applies_to_a_page():
    for bad in ("SELECT ExportOrderID FROM rag.vw_ExportOrder ORDER BY PCD OFFSET 200 ROWS FETCH NEXT 200 ROWS ONLY; "
                "DROP TABLE x",
                "SELECT Salary FROM rag.vw_ExportOrder ORDER BY PCD OFFSET 200 ROWS FETCH NEXT 200 ROWS ONLY",
                "SELECT a FROM dbo.Secret ORDER BY a OFFSET 200 ROWS FETCH NEXT 200 ROWS ONLY"):
        with pytest.raises(GuardError):
            guard(bad, V, "tsql", max_offset=2000)


# ---------------------------------------------------------------- page_sql
def test_page_sql_replaces_top_with_offset_and_fetch():
    out = page_sql("SELECT TOP (200) ExportOrderID, PCD FROM rag.vw_ExportOrder WHERE Factory = @factory "
                   "ORDER BY PCD, ExportOrderID", "tsql", 2, 200)
    assert "TOP" not in out and out.endswith("ORDER BY PCD, ExportOrderID OFFSET 400 ROWS FETCH FIRST 200 ROWS ONLY")
    assert "@factory" in out
    again = page_sql(out, "tsql", 3, 200)                                    # a page of a page: the window moves
    assert again.endswith("OFFSET 600 ROWS FETCH FIRST 200 ROWS ONLY") and again.count("OFFSET") == 1


def test_page_sql_pages_the_final_select_of_a_cte_and_mysql():
    out = page_sql("WITH x AS (SELECT a FROM rag.v) SELECT TOP (200) a FROM x ORDER BY a", "tsql", 1, 200)
    assert out.startswith("WITH x AS") and out.endswith("ORDER BY a OFFSET 200 ROWS FETCH FIRST 200 ROWS ONLY")
    assert page_sql("SELECT a, b FROM rag.v LIMIT 200", "mysql", 1, 200, ["a"]).endswith("ORDER BY `a` LIMIT 200 OFFSET 200")


def test_page_sql_needs_a_fixed_order():
    assert "ORDER BY [ExportOrderID]" in page_sql("SELECT TOP (200) ExportOrderID, PCD FROM rag.vw_ExportOrder",
                                                   "tsql", 1, 200, ["ExportOrderID"])
    with pytest.raises(GuardError, match="no fixed order"):
        page_sql("SELECT TOP (200) PCD FROM rag.vw_ExportOrder", "tsql", 1, 200, ["ExportOrderID"])


def test_a_generated_page_passes_the_guard():
    paged = page_sql("SELECT TOP (200) ExportOrderID, PCD FROM rag.vw_ExportOrder WHERE PCD >= @today "
                     "ORDER BY PCD", "tsql", 1, 200)
    assert guard(paged, V, "tsql", max_offset=2000) == paged
    with pytest.raises(GuardError):
        guard(paged, V, "tsql")                                               # and only when paging asks


def test_every_real_tool_pages_with_its_parameters_intact():
    paged = 0
    for t in T.load_fixed_tools():
        sql = T.tool_sql(t).strip()
        try:
            out = page_sql(sql, "tsql", 1, 200, ["ExportOrderID"])
        except GuardError:
            continue                                                          # no fixed order: never paged
        assert set(re.findall(r"@\w+", sql)) == set(re.findall(r"@\w+", out)), t["name"]
        assert "OFFSET 200 ROWS FETCH FIRST 200 ROWS ONLY" in out, t["name"]
        sqlglot.parse_one(out, read="tsql")
        paged += 1
    assert paged >= 40
