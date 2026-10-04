"""Phase J: one connection string per server; a catalog names its database (connection_database)."""
import ragbot.data.connectors as con


def test_dsn_points_the_shared_connection_at_another_database(monkeypatch):
    monkeypatch.setenv("SQLSERVER_CONN_T", "Driver={ODBC Driver 18 for SQL Server};Server=h\\i;Database=Production;UID=u;PWD=p;Encrypt=yes")
    assert con.dsn("SQLSERVER_CONN_T") == "Driver={ODBC Driver 18 for SQL Server};Server=h\\i;Database=Production;UID=u;PWD=p;Encrypt=yes"
    assert con.dsn("SQLSERVER_CONN_T", "HR") == "Driver={ODBC Driver 18 for SQL Server};Server=h\\i;Database=HR;UID=u;PWD=p;Encrypt=yes"


def test_dsn_handles_initial_catalog_case_and_a_missing_database(monkeypatch):
    monkeypatch.setenv("C1", "Server=h; initial catalog = Prod ;UID=u")
    assert con.dsn("C1", "Inventory") == "Server=h; initial catalog =Inventory;UID=u"
    monkeypatch.setenv("C2", "Server=h;UID=u;")
    assert con.dsn("C2", "FM") == "Server=h;UID=u;Database=FM"
    monkeypatch.setenv("C3", "Server=h;DATABASE=x;UID=u")
    assert con.dsn("C3", "FM") == "Server=h;DATABASE=FM;UID=u"


def test_pools_are_per_database(monkeypatch):
    monkeypatch.setattr(con, "_POOLS", {})
    a = con._sqlserver_pool("E", "A")
    assert con._sqlserver_pool("E", "A") is a
    assert con._sqlserver_pool("E", "B") is not a and con._sqlserver_pool("E") is not a
    assert set(con._POOLS) == {"E|A", "E|B", "E|"}
