"""C1: sensitive-name matching and discovered-schema assembly (offline, synthetic sys.* rows)."""
import pytest

from ragbot.data.discovery import assemble, sample_candidates
from ragbot.data.sensitive import is_sensitive


@pytest.mark.parametrize("name", ["EmpNID", "NID_No", "DOB", "DateOfBirth", "BasicSalary", "EmpMobileNo",
                                  "empmobile", "EmailAddress", "BankAccNo", "UserPassword", "BloodGroup",
                                  "api_token", "Religion", "HR_Salaries"])
def test_sensitive_names(name):
    assert is_sensitive(name), name


@pytest.mark.parametrize("name", ["ManID", "TownID", "ExportOrderID", "ShipmentStatus", "BuyerStyleNo",
                                  "PCD", "FileRefNo", "Doberman", "UnitName"])
def test_ordinary_names(name):
    assert not is_sensitive(name), name


TABLES = [("dbo", "ExportOrder", "U", 120000), ("dbo", "Buyer", "U", 300), ("dbo", "Employee", "U", 5000),
          ("dbo", "ExportOrderBack", "U", 4_000_000), ("rag", "vw_X", "V", None)]
COLUMNS = [
    ("dbo", "ExportOrder", "ExportOrderID", "varchar", 30, 0, 1),
    ("dbo", "ExportOrder", "BuyerID", "int", 4, 0, 2),
    ("dbo", "ExportOrder", "ShipmentStatus", "nvarchar", 40, 1, 3),
    ("dbo", "ExportOrder", "Remarks", "nvarchar", -1, 1, 4),
    ("dbo", "Buyer", "BuyerID", "int", 4, 0, 1),
    ("dbo", "Buyer", "BuyerName", "varchar", 100, 0, 2),
    ("dbo", "Employee", "EmpMobileNo", "varchar", 20, 1, 1),
    ("dbo", "Employee", "Grade", "varchar", 10, 1, 2),
    ("dbo", "ExportOrderBack", "Status", "varchar", 20, 1, 1),
    ("rag", "vw_X", "Status", "varchar", 20, 1, 1),
]


def test_assemble_shapes_tables_keys_and_descriptions():
    doc = assemble(
        "BitopiSplint", "SQLSERVER_CONN_BITOPISPLINT", TABLES, COLUMNS,
        primary_keys=[("dbo", "ExportOrder", "ExportOrderID", 1), ("dbo", "Buyer", "BuyerID", 1)],
        foreign_keys=[("FK_EO_Buyer", "dbo", "ExportOrder", "BuyerID", "dbo", "Buyer", "BuyerID", 1)],
        descriptions=[("dbo", "ExportOrder", None, "Confirmed export orders"),
                      ("dbo", "ExportOrder", "ShipmentStatus", "Shipping state")],
        samples={("dbo", "ExportOrder", "ShipmentStatus"): ["SHIPPED", "TO SHIP"],
                 ("dbo", "Employee", "EmpMobileNo"): ["01711..."]})
    t = {x["name"]: x for x in doc["tables"]}
    eo = t["dbo.ExportOrder"]
    assert eo["kind"] == "table" and eo["rows"] == 120000 and eo["description"] == "Confirmed export orders"
    assert eo["primary_key"] == ["ExportOrderID"]
    assert eo["foreign_keys"] == [{"columns": ["BuyerID"], "ref_table": "dbo.Buyer", "ref_columns": ["BuyerID"]}]
    cols = {c["name"]: c for c in eo["columns"]}
    assert [c["name"] for c in eo["columns"]] == ["ExportOrderID", "BuyerID", "ShipmentStatus", "Remarks"]
    assert cols["ShipmentStatus"]["type"] == "nvarchar(20)" and cols["Remarks"]["type"] == "nvarchar(max)"
    assert cols["ShipmentStatus"]["samples"] == ["SHIPPED", "TO SHIP"]
    assert cols["ShipmentStatus"]["description"] == "Shipping state"
    emp = {c["name"]: c for c in t["dbo.Employee"]["columns"]}
    assert emp["EmpMobileNo"]["sensitive"] and "samples" not in emp["EmpMobileNo"]   # never kept, even if given
    assert t["rag.vw_X"]["kind"] == "view" and t["rag.vw_X"]["rows"] is None


def test_sample_candidates_are_small_short_text_and_not_sensitive():
    got = set(sample_candidates(TABLES, COLUMNS, max_rows=200_000))
    assert ("dbo", "ExportOrder", "ShipmentStatus") in got and ("dbo", "Buyer", "BuyerName") in got
    assert ("dbo", "Employee", "Grade") in got
    assert ("dbo", "Employee", "EmpMobileNo") not in got          # sensitive column
    assert ("dbo", "ExportOrder", "Remarks") not in got           # nvarchar(max)
    assert ("dbo", "ExportOrder", "BuyerID") not in got           # not text
    assert ("dbo", "ExportOrderBack", "Status") not in got        # 4M rows: too big to scan
    assert ("rag", "vw_X", "Status") not in got                   # views are never scanned
