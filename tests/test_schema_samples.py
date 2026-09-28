"""G2: sample column values only for the raw tables the SQL model actually needs, and keep earlier
samples when discovery runs again."""
from ragbot.data.catalog import Catalog, Table, View
from ragbot.data.discovery import assemble, carried_samples, sample_candidates
from ragbot.data.schema_usage import SELECT_HEADER, used_tables
from ragbot.logs import append_row

TABLES = [("dbo", "Status", "U", 50), ("dbo", "Big", "U", 5_000_000), ("dbo", "Other", "U", 20)]
COLUMNS = [("dbo", "Status", "Code", "varchar", 20, True, 1), ("dbo", "Big", "Kind", "varchar", 20, True, 1),
           ("dbo", "Other", "Flag", "varchar", 10, True, 1), ("dbo", "Status", "Email", "varchar", 50, True, 2)]


def test_only_the_listed_tables_are_sampled():
    assert sample_candidates(TABLES, COLUMNS, 200_000) == [("dbo", "Status", "Code"), ("dbo", "Other", "Flag")]
    assert sample_candidates(TABLES, COLUMNS, 200_000, only={"dbo.status"}) == [("dbo", "Status", "Code")]
    assert sample_candidates(TABLES, COLUMNS, 200_000, only=set()) == []


def test_earlier_samples_survive_a_new_run_and_fresh_ones_replace_them():
    old = assemble("DB", "X", TABLES, COLUMNS, [], [], [], {("dbo", "Status", "Code"): ["A", "B"],
                                                           ("dbo", "Other", "Flag"): ["Y"]})
    kept = carried_samples(old)
    assert kept == {("dbo", "Status", "Code"): ["A", "B"], ("dbo", "Other", "Flag"): ["Y"]}
    new = assemble("DB", "X", TABLES, COLUMNS, [], [], [], {**kept, ("dbo", "Status", "Code"): ["A", "B", "C"]})
    cols = {(t["name"], c["name"]): c.get("samples") for t in new["tables"] for c in t["columns"]}
    assert cols[("dbo.Status", "Code")] == ["A", "B", "C"] and cols[("dbo.Other", "Flag")] == ["Y"]
    assert carried_samples(None) == {}


def test_used_tables_come_from_schema_selection_and_generated_sql(tmp_path):
    cat = Catalog("DB", "tsql", "X", [], [View("rag.vw_Orders", "g", "d", [], {})],
                  tables=[Table("dbo.Status"), Table("dbo.Other"), Table("dbo.Never")])
    append_row("schema_select.csv", SELECT_HEADER, ["t", "u", "DB", "dbo.Status;dbo.Other", "q1"], folder=tmp_path)
    append_row("schema_select.csv", SELECT_HEADER, ["t", "u", "DB", "dbo.Status", "q2"], folder=tmp_path)
    append_row("schema_select.csv", SELECT_HEADER, ["t", "u", "OtherDB", "dbo.Never", "q3"], folder=tmp_path)
    header = ["ts", "user", "scope", "engine", "tool", "rows", "ms", "error", "params", "sql", "sql_exec"]
    append_row("sql.csv", header, ["t", "u", "", "sqlserver", "generated", 1, 5, "", {},
                                   "SELECT TOP (5) Flag FROM dbo.Other JOIN rag.vw_Orders o ON 1=1", ""], folder=tmp_path)
    append_row("sql.csv", header, ["t", "u", "", "sqlserver", "eo_by_id", 1, 5, "", {}, "SELECT * FROM dbo.Never", ""],
               folder=tmp_path)
    assert used_tables(cat, folder=tmp_path) == ["dbo.Other", "dbo.Status"]   # 2 uses each; views and fixed tools ignored
