"""P3: three-part names stay rejected unless the guard is handed the sibling databases' own offered tables —
and then only for a raw table of a listed sibling, never a rag.* view, never a sensitive name, with the
SELECT * and big-table rules applied. The default (no map) is the rule every earlier test relies on."""
import pytest

from ragbot.data.guard import GuardError, guard

VIEWS = {"rag.vw_StockBalance"}
RAW = frozenset({"dbo.grnchild", "dbo.grnmaster"})
SIBLINGS = {"systemmanager": frozenset({"dbo.company", "dbo.companyunit"}),
            "bitopisplint": frozenset({"dbo.exportorder", "dbo.supplierdata"})}
BIG = {"dbo.grnchild": 942_738, "bitopisplint.dbo.exportorder": 2_000_000}


def g(sql, siblings=SIBLINGS):
    return guard(sql, VIEWS, "tsql", 200, allowed_tables=RAW, big_tables=BIG, allowed_catalogs=siblings)


@pytest.mark.parametrize("ok", [
    "SELECT TOP (20) g.GRNID, c.CompanyName FROM dbo.GRNMaster g JOIN SystemManager.dbo.Company c ON c.CompanyID = g.CompanyID",
    "SELECT TOP (20) g.GRNID, c.CompanyName FROM dbo.GRNMaster g JOIN [SystemManager].[dbo].[Company] c ON c.CompanyID = g.CompanyID",
    "SELECT TOP (5) s.SupplierName FROM BitopiSplint.dbo.SupplierData s WHERE s.SupplierName LIKE 'A%'",
])
def test_a_sibling_table_is_allowed_when_listed(ok):
    assert g(ok).upper().startswith("SELECT TOP")


@pytest.mark.parametrize("bad, why", [
    ("SELECT x FROM [Other].dbo.Buyer", "three-part name not allowed"),                          # not a sibling
    ("SELECT x FROM SystemManager.dbo.Menu", "table not in catalog"),                              # sibling, table not offered
    ("SELECT x FROM SystemManager.rag.vw_Company", "rag.* views cannot be read"),                 # another catalog's view
    ("SELECT Salary FROM SystemManager.dbo.EmployeeSalary", "restricted"),                         # sensitive name
    ("SELECT * FROM SystemManager.dbo.Company", "is not allowed on raw tables"),                   # raw-table rule
    ("SELECT TOP (5) ExportOrderID FROM BitopiSplint.dbo.ExportOrder", "filter it with a WHERE"),  # big-table rule
])
def test_everything_else_about_a_three_part_name_is_refused(bad, why):
    with pytest.raises(GuardError, match=why):
        g(bad)


def test_without_the_map_every_three_part_name_is_rejected_as_before():
    with pytest.raises(GuardError, match="three-part name not allowed"):
        g("SELECT TOP (5) c.CompanyName FROM SystemManager.dbo.Company c", siblings=None)
    with pytest.raises(GuardError, match="three-part name not allowed"):
        guard("SELECT x FROM [Other].dbo.Buyer", VIEWS, "tsql", 200, allowed_tables=RAW)
