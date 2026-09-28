"""F4: the server-side row-scope filter the DBA's rag views carry (PRD FR-4.4)."""
import sqlglot
from sqlglot import exp

from ragbot.data.catalog import load_catalogs
from ragbot.data.scope_sql import SESSION_KEY, session_filtered

DEF = "SELECT eo.ExportOrderID, fr.Factory AS Factory FROM dbo.ExportOrder eo LEFT JOIN dbo.FileRef fr ON fr.FileRefID = eo.FileRefID"


def test_filtered_view_is_one_select_over_the_definition():
    sql = session_filtered(DEF, "Factory")
    tree = sqlglot.parse_one(sql, read="tsql")
    assert isinstance(tree, exp.Select)
    assert {t.name for t in tree.find_all(exp.Table)} == {"ExportOrder", "FileRef"}
    assert f"SESSION_CONTEXT(N'{SESSION_KEY}')" in sql and "v.[Factory]" in sql


def test_filter_allows_star_or_a_listed_factory_and_nothing_else():
    sql = session_filtered(DEF + ";", "Factory")
    where = sql.split("WHERE", 1)[1]
    assert "= N'*'" in where                                    # all factories
    assert "CHARINDEX(N',' + v.[Factory] + N','" in where        # exact code match inside ',TAL,RHL,'
    assert "OPENJSON" not in sql and "STRING_SPLIT" not in sql  # both need compatibility level 130+
    assert ";" not in sql                                        # a trailing ; would break CREATE VIEW ... AS


def test_every_real_catalog_view_declares_a_scope_column_it_returns():
    for cat in load_catalogs().values():
        for v in cat.views:
            assert v.scope_column, f"{v.name} has no scope_column"
            assert v.scope_column in v.columns
