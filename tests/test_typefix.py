"""Phase Q (Q2): a bare number compared to a text column is quoted before the statement runs.

The 11 Oct failure in one line: `WHERE Line = 2102` against a column holding workspace names made SQL Server
cast every name and fail on 'Remi Holdings Ltd.' (22018/245).
"""
import pytest

from ragbot.data import typefix
from ragbot.data.catalog import Catalog, Table, View, kind_of_sql_type

VIEW = View(name="rag.vw_PlanDay", grain="", description="", key_columns=[],
            columns={"Factory": "", "Line": "", "Style": "", "PlanDate": "", "DayTarget": "", "PlanHours": ""},
            column_kinds={"Factory": "text", "Line": "text", "Style": "text", "PlanDate": "date",
                          "DayTarget": "int", "PlanHours": "decimal"})
QUALITY = View(name="rag.vw_PlanQualityDay", grain="", description="", key_columns=[],
               columns={"Factory": "", "Line": "", "Checked": ""},
               column_kinds={"Factory": "text", "Line": "text", "Checked": "int"})
WORKSPACE = Table(name="dbo.Workspace", rows=179,
                  columns=[{"name": "Id", "type": "int"}, {"name": "WorkspaceName", "type": "nvarchar(255)"},
                           {"name": "SortOrder", "type": "int"}])
CAT = Catalog(database="VistaQ_Default_RHL", dialect="tsql", connection_env="X", rules=[],
              views=[VIEW, QUALITY], tables=[WORKSPACE])


def _fix(sql, mode="known"):
    return typefix.fix(sql, CAT, mode=mode)


def test_sql_types_map_to_kinds():
    assert kind_of_sql_type("nvarchar(255)") == "text" and kind_of_sql_type("varchar(max)") == "text"
    assert kind_of_sql_type("int") == "int" and kind_of_sql_type("decimal(12,2)") == "decimal"
    assert kind_of_sql_type("datetime2") == "datetime" and kind_of_sql_type("date") == "date"
    assert kind_of_sql_type("bit") == "bool" and kind_of_sql_type("geography") is None


def test_the_failing_statement_of_11_october_is_repaired():
    sql, notes = _fix("SELECT TOP (1) Style FROM rag.vw_PlanDay WHERE Factory = 'TAL' AND Line = 2102 "
                      "AND PlanDate >= '2026-10-11' AND PlanDate < '2026-10-12' AND PlanHours > 0")
    assert "Line = '2102'" in sql and notes
    assert "PlanHours > 0" in sql and "'2026-10-11'" in sql        # numeric and date comparisons untouched


@pytest.mark.parametrize("before,after", [
    ("SELECT Style FROM rag.vw_PlanDay p WHERE p.Line = 2102", "p.Line = '2102'"),
    ("SELECT Style FROM rag.vw_PlanDay WHERE [Line] = 2102", "[Line] = '2102'"),
    ("SELECT Style FROM rag.vw_PlanDay WHERE Line<>2102", "Line <> '2102'"),
])
def test_the_forms_a_model_writes(before, after):
    sql, notes = _fix(before)
    assert after in sql and notes


def test_in_lists_and_between_are_quoted():
    sql, _ = _fix("SELECT Style FROM rag.vw_PlanDay WHERE Line IN (2102, 2103)")
    assert "'2102'" in sql and "'2103'" in sql
    sql, _ = _fix("SELECT Style FROM rag.vw_PlanDay WHERE Line BETWEEN 2100 AND 2110")
    assert "'2100'" in sql and "'2110'" in sql


def test_a_raw_tables_kind_comes_from_the_discovered_type():
    sql, notes = _fix("SELECT Id FROM dbo.Workspace WHERE WorkspaceName = 2102")
    assert "WorkspaceName = '2102'" in sql and notes
    assert _fix("SELECT Id FROM dbo.Workspace WHERE SortOrder = 3")[1] == []


def test_nothing_else_is_touched():
    for sql in ["SELECT Style FROM rag.vw_PlanDay WHERE DayTarget = 2102",          # an int column
                "SELECT Style FROM rag.vw_PlanDay WHERE PlanHours > 0",             # a decimal column
                "SELECT TOP (2102) Style FROM rag.vw_PlanDay",                      # a row cap
                "SELECT Style FROM rag.vw_PlanDay WHERE Line = '2102'",             # already quoted
                "SELECT Style FROM rag.vw_PlanDay WHERE Style = 'Line = 2102'",     # inside a string literal
                "SELECT Style FROM rag.vw_PlanDay WHERE Line = 2102.5",             # not an integer
                "SELECT n FROM rag.vw_Unknown WHERE Line = 2102"]:                  # an object we do not know
        assert _fix(sql) == (sql, []), sql


def test_an_unknown_kind_is_quoted_only_in_aggressive_mode():
    sql = "SELECT n FROM dbo.Workspace WHERE Id = 5 AND WorkspaceName = 7"
    assert "WorkspaceName = '7'" in _fix(sql)[0]                  # known text column, either mode
    assert "Id = 5" in _fix(sql)[0]                               # known int column, left alone
    hard, notes = _fix("SELECT a FROM dbo.Workspace WHERE Mystery = 7", mode="aggressive")
    assert "Mystery = '7'" in hard and notes
    assert _fix("SELECT a FROM dbo.Workspace WHERE Mystery = 7") == (
        "SELECT a FROM dbo.Workspace WHERE Mystery = 7", [])


def test_two_sources_must_agree_before_an_unqualified_column_is_touched():
    both = "SELECT a FROM rag.vw_PlanDay JOIN rag.vw_PlanQualityDay ON 1 = 1 WHERE Line = 2102"
    assert "Line = '2102'" in _fix(both)[0]                       # text in both views
    mixed = "SELECT a FROM rag.vw_PlanDay JOIN rag.vw_PlanQualityDay ON 1 = 1 WHERE Checked = 5"
    assert _fix(mixed)[1] == []                                   # int in one, unknown in the other


def test_a_cte_or_derived_table_is_left_alone():
    sql = "WITH x AS (SELECT Line, Style FROM rag.vw_PlanDay) SELECT Style FROM x WHERE Line = 2102"
    assert _fix(sql) == (sql, [])


def test_the_fix_is_idempotent_and_still_parses():
    once, _ = _fix("SELECT Style FROM rag.vw_PlanDay WHERE Line = 2102")
    twice, notes = _fix(once)
    assert twice == once and notes == []


def test_conversion_errors_are_recognised():
    assert typefix.is_conversion_error(
        "DataError: ('22018', \"[22018] ... Conversion failed when converting the nvarchar value "
        "'Remi Holdings Ltd.' to data type int. (245) (SQLExecDirectW)\")")
    assert typefix.is_conversion_error("8114 Error converting data type varchar to numeric")
    assert not typefix.is_conversion_error("('HYT00', 'Query timeout expired (0)')")
    assert not typefix.is_conversion_error("")


def test_the_kinds_line_names_what_the_statement_read():
    line = typefix.kinds_line("SELECT Style FROM rag.vw_PlanDay WHERE Line = 2102", CAT)
    assert line.startswith("rag.vw_PlanDay:") and "Line (text)" in line and "DayTarget (int)" in line
