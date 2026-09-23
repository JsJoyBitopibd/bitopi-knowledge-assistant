import pytest
from ragbot.data.guard import GuardError, guard

VIEWS = {"rag.vw_exportorderpcd"}


def test_select_passes_and_gets_top():
    s = guard("SELECT EONo FROM rag.vw_ExportOrderPCD WHERE Factory = 'TAL'", VIEWS, "tsql")
    assert s.upper().startswith("SELECT TOP (200)")


def test_distinct_keeps_distinct_after_top():
    s = guard("SELECT DISTINCT Factory FROM rag.vw_ExportOrderPCD", VIEWS, "tsql")
    assert s.upper().startswith("SELECT DISTINCT TOP (200)")


def test_case_insensitive_schema_and_name():
    s = guard("select * from RAG.VW_EXPORTORDERPCD", VIEWS, "tsql")
    assert s.upper().startswith("SELECT TOP (200)")


def test_model_cte_with_top_passes():
    s = guard("WITH x AS (SELECT EONo FROM rag.vw_ExportOrderPCD) SELECT TOP (10) * FROM x", VIEWS, "tsql")
    assert "TOP (10)" in s.upper()


def test_mysql_limit():
    s = guard("SELECT LotNo FROM rag.vw_fabric_stock", {"rag.vw_fabric_stock"}, "mysql")
    assert s.upper().endswith("LIMIT 200")


def test_top_already_present_not_duplicated():
    s = guard("SELECT TOP (5) EONo FROM rag.vw_ExportOrderPCD", VIEWS, "tsql")
    assert s.upper().count("TOP") == 1


@pytest.mark.parametrize("bad", [
    # DML / DDL
    "UPDATE rag.vw_ExportOrderPCD SET PCD = '2026-01-01'",
    "INSERT INTO rag.vw_ExportOrderPCD (EONo) VALUES ('x')",
    "DELETE FROM rag.vw_ExportOrderPCD",
    "MERGE rag.vw_ExportOrderPCD USING x ON 1=1",
    "DROP TABLE rag.vw_ExportOrderPCD",
    "ALTER TABLE rag.vw_ExportOrderPCD ADD x INT",
    "CREATE VIEW x AS SELECT 1",
    "TRUNCATE TABLE rag.vw_ExportOrderPCD",
    # statement stacking / comments
    "SELECT * FROM rag.vw_ExportOrderPCD; DROP TABLE x",
    "SELECT * FROM rag.vw_ExportOrderPCD WHERE 1=1; SELECT 1",
    "SELECT * FROM rag.vw_ExportOrderPCD -- comment",
    "SELECT * FROM rag.vw_ExportOrderPCD /* comment */",
    "SELECT * FROM rag.vw_ExportOrderPCD WHERE n = 'x' OR 1=1 -- ",
    # dangerous procs / system access
    "EXEC xp_cmdshell 'dir'",
    "EXECUTE sp_executesql N'SELECT 1'",
    "SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'y')",
    "SELECT * FROM OPENQUERY(srv, 'SELECT 1')",
    "SELECT * FROM sys.tables",
    "SELECT * FROM information_schema.tables",
    "SELECT name FROM rag.vw_ExportOrderPCD UNION SELECT name FROM sys.objects",
    "BACKUP DATABASE x TO DISK='y'",
    "SELECT 1; WAITFOR DELAY '0:0:5'",
    "SELECT 1 INTO OUTFILE '/tmp/x' FROM rag.vw_ExportOrderPCD",
    "SELECT * INTO #t FROM rag.vw_ExportOrderPCD",
    "DECLARE @x INT",
    # scoping violations
    "SELECT * FROM dbo.ExportOrder",                                  # schema swap
    "SELECT * FROM vw_ExportOrderPCD",                                # bare name, not a CTE
    "SELECT * FROM rag.vw_Nope",                                      # not in catalog
    "SELECT * FROM [Production.PPM].rag.vw_ExportOrderPCD",           # three-part name
    "SELECT * FROM rag.vw_ExportOrderPCD a, dbo.Users b",             # comma join to a disallowed table
    "SELECT * FROM STRING_SPLIT('a,b', ',')",                          # disguised function-as-table
    # row cap abuse
    "SELECT TOP 100 PERCENT * FROM rag.vw_ExportOrderPCD",
    "SELECT TOP 5000 * FROM rag.vw_ExportOrderPCD",
    # illegal ORDER BY without a cap, and a UNION with no cap on the final select
    "SELECT * FROM rag.vw_ExportOrderPCD ORDER BY EONo",
    "SELECT EONo FROM rag.vw_ExportOrderPCD UNION SELECT EONo FROM rag.vw_ExportOrderPCD",
])
def test_denied(bad):
    with pytest.raises(GuardError):
        guard(bad, VIEWS, "tsql")
