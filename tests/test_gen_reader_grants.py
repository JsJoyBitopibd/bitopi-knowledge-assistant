"""F2: the rag_reader grant script — instance-wide read, DENY on what discovery marked sensitive, no password."""
from ragbot.data.catalog import Catalog, Table
from ragbot.data.grants import (PASSWORD_VAR, SERVER_PERMISSIONS, deny_statements, qualified, render_grants,
                                render_rollback)


def _cat(tables, name="Production", dialect="tsql"):
    return Catalog(name, dialect, f"SQLSERVER_CONN_{name.upper()}", [], [], tables=tables)


SALARY = Table("HR.Salary", sensitive=True, columns=[{"name": "Amount", "sensitive": False}])
CONTACT = Table("dbo.Contact_Master", columns=[{"name": "ContactName", "sensitive": False},
                                              {"name": "Phone", "sensitive": True},
                                              {"name": "Email", "sensitive": True}])
ORDERS = Table("dbo.ExportOrder", columns=[{"name": "ExportOrderID", "sensitive": False}])
EMP_VIEW = Table("dbo.vwEmployees", kind="view", columns=[{"name": "DOB", "sensitive": True}])


def test_deny_whole_object_or_only_its_sensitive_columns():
    assert deny_statements(SALARY, "rag_reader") == ["DENY SELECT ON OBJECT::[HR].[Salary] TO [rag_reader];"]
    assert deny_statements(CONTACT, "rag_reader") == [
        "DENY SELECT ON OBJECT::[dbo].[Contact_Master] ([Phone], [Email]) TO [rag_reader];"]
    assert deny_statements(ORDERS, "rag_reader") == []
    assert deny_statements(EMP_VIEW, "rag_reader") == [
        "DENY SELECT ON OBJECT::[dbo].[vwEmployees] ([DOB]) TO [rag_reader];"]


def test_identifiers_are_bracket_quoted():
    assert qualified("dbo.Odd]Name") == "[dbo].[Odd]]Name]"
    assert qualified("NoSchema") == "[dbo].[NoSchema]"


def test_grants_script_shape():
    sql = render_grants("rag_reader", {"Production": _cat([SALARY, CONTACT, ORDERS]),
                                       "BitopiSplint": _cat([], "BitopiSplint")})
    # one login, created only if missing, password only as the sqlcmd variable
    assert sql.count("CREATE LOGIN [rag_reader]") == 1
    assert "IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'rag_reader')" in sql
    assert f"PASSWORD = N'{PASSWORD_VAR}'" in sql
    # the three read-only server permissions and nothing that writes or administers
    grants = [l.strip() for l in sql.splitlines() if l.strip().startswith("GRANT ")]
    assert len(grants) == len(SERVER_PERMISSIONS)
    for perm, _ in SERVER_PERMISSIONS:
        assert any(g.startswith(f"GRANT {perm}") and "TO [rag_reader]" in g for g in grants), perm
    assert not any(w in g for g in grants for w in ("CONTROL", "ALTER", "INSERT", "UPDATE", "DELETE", "IMPERSONATE"))
    # one user per T-SQL database, DENYs sorted by object name, a database without discovery gets a warning
    assert sql.count("CREATE USER [rag_reader] FOR LOGIN [rag_reader]") == 2
    prod = sql.split("USE [Production];")[1].split("\nGO\n")[0]
    assert prod.count("DENY SELECT") == 2
    assert prod.index("[dbo].[Contact_Master]") < prod.index("[HR].[Salary]")
    assert "2 sensitive columns" in prod and "1 sensitive objects" in prod
    bsp = sql.split("USE [BitopiSplint];")[1].split("\nGO\n")[0]
    assert "DENY SELECT" not in bsp and "discover_schema.py --json BitopiSplint" in bsp


def test_mysql_catalogs_are_skipped():
    sql = render_grants("rag_reader", {"Stock": _cat([SALARY], "Stock", dialect="mysql")})
    assert "USE [Stock]" not in sql and "DENY SELECT" not in sql
    assert "CREATE LOGIN [rag_reader]" in sql        # the server part is still written


def test_rollback_drops_users_before_the_login_and_revokes_every_permission():
    sql = render_rollback("rag_reader", {"Production": _cat([SALARY]), "BitopiSplint": _cat([], "BitopiSplint")})
    assert sql.count("DROP USER [rag_reader]") == 2
    assert sql.rindex("DROP USER [rag_reader]") < sql.index("DROP LOGIN [rag_reader]")
    for perm, _ in SERVER_PERMISSIONS:
        assert f"REVOKE {perm} FROM [rag_reader];" in sql
    assert PASSWORD_VAR not in sql
