"""C4: the guard with raw discovered tables allowed — allow-list, sensitive names, SELECT *, big-table
filter, new deny tokens. The pre-C4 behaviour (rag.* only) is covered by tests/test_guard.py."""
import pytest

from ragbot.data.guard import GuardError, guard

VIEWS = {"rag.vw_ExportOrder"}
RAW = frozenset({"dbo.buyer", "dbo.supplier", "dbo.exportorder", "ppm.ppmmeetings", "dbo.exportorderback"})
BIG = {"dbo.exportorderback": 4_235_069}


def g(sql):
    return guard(sql, VIEWS, "tsql", 200, allowed_tables=RAW, big_tables=BIG)


@pytest.mark.parametrize("ok", [
    "SELECT BuyerID, BuyerName FROM dbo.Buyer",
    "SELECT COUNT(*) AS Suppliers FROM dbo.Supplier",
    "SELECT TOP (20) e.ExportOrderID, b.BuyerName FROM dbo.ExportOrder e JOIN dbo.Buyer b ON b.BuyerID = e.BuyerID",
    "SELECT TOP (10) v.ExportOrderID FROM rag.vw_ExportOrder v JOIN dbo.Buyer b ON b.BuyerName = v.Buyer",
    "SELECT TOP (50) VersionNo, PCD FROM dbo.ExportOrderBack WHERE ExportOrderID = 'TAL-25-1493-215'",
    "SELECT * FROM rag.vw_ExportOrder",                               # star still fine on curated views
    "WITH x AS (SELECT BuyerID FROM dbo.Buyer) SELECT TOP (5) BuyerID FROM x",
])
def test_allowed(ok):
    assert "SELECT" in g(ok).upper()


@pytest.mark.parametrize("bad", [
    # not in the allow-list / scoping
    "SELECT BuyerID FROM dbo.Users",
    "SELECT BuyerID FROM hr.Buyer",
    "SELECT x FROM [Other].dbo.Buyer",
    "SELECT BuyerID FROM Buyer",                                       # bare raw name
    # SELECT * on raw tables
    "SELECT * FROM dbo.Buyer",
    "SELECT b.* FROM dbo.Buyer b",
    "SELECT TOP (5) * FROM rag.vw_ExportOrder v JOIN dbo.Buyer b ON b.BuyerName = v.Buyer",
    # big table without a filter
    "SELECT TOP (10) VersionNo FROM dbo.ExportOrderBack",
    "SELECT COUNT(*) FROM dbo.ExportOrderBack",
    # sensitive names, even on allowed tables
    "SELECT BuyerName, Email FROM dbo.Buyer",
    "SELECT TOP (5) BuyerName FROM dbo.Buyer WHERE MobileNo LIKE '017%'",
    "SELECT EmpName, BloodGroup, Phone FROM dbo.Buyer",               # the real 'A+ blood group … with number' probe
    "SELECT TOP (5) BasicSalary FROM dbo.Supplier",
    "SELECT n FROM dbo.EmployeeSalary",
    # new deny tokens
    "SELECT BuyerName FROM dbo.Buyer FOR XML PATH('')",
    "SELECT BuyerName FROM dbo.Buyer FOR JSON AUTO",
    "SELECT @@VERSION",
    "SELECT SUSER_SNAME()",
    "SELECT SYSTEM_USER",
    "SELECT HOST_NAME()",
    "SELECT ORIGINAL_LOGIN()",
])
def test_denied(bad):
    with pytest.raises(GuardError):
        g(bad)


def test_default_arguments_keep_pre_c4_rules():
    with pytest.raises(GuardError, match="schema not allowed"):
        guard("SELECT BuyerID FROM dbo.Buyer", VIEWS, "tsql")


def test_curated_examples_pass_the_guard():
    """The few-shot examples in config/catalog/*.yaml teach the SQL model; one the guard rejects costs a
    failed attempt and an extra LLM call on every question that imitates it (found 2026-09-27)."""
    from ragbot.data.catalog import load_catalogs
    for db, c in load_catalogs().items():
        for ex in c.examples:
            guard(ex["sql"], c.view_names, c.dialect, 200)
