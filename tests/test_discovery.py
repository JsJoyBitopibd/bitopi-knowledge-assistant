"""C1: sensitive-name matching and discovered-schema assembly (offline, synthetic sys.* rows)."""
import pytest

from ragbot.data.discovery import (assemble, budget_refused, candidates_from_doc, queries_by_table, sample_candidates,
                                   sample_skipped, samples_refused)
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
    assert ("dbo", "ExportOrder", "ShipmentStatus") in got
    assert ("dbo", "Buyer", "BuyerName") not in got               # a *Name column: the sampling skip list (G2)
    assert ("dbo", "Employee", "Grade") not in got                 # a people table: never sampled (S4 review)
    assert ("dbo", "Employee", "EmpMobileNo") not in got          # sensitive column
    assert ("dbo", "ExportOrder", "Remarks") not in got           # nvarchar(max)
    assert ("dbo", "ExportOrder", "BuyerID") not in got           # not text
    assert ("dbo", "ExportOrderBack", "Status") not in got        # 4M rows: too big to scan
    assert ("rag", "vw_X", "Status") not in got                   # views are never scanned


# --- G2: sampling is fenced before the owner-approved run -------------------------------------------------

@pytest.mark.parametrize("name", ["CreatedBy", "UpdatedBy", "ApprovedBy", "UserName", "UserID", "Users", "EmpName",
                                  "EmpCode", "LoginID", "LoginName", "BuyerName", "Created_By", "createdby",
                                  "USERNAME", "empname", "loginid", "Name",
                                  # review of 2026-10-15: a digit after the word, or words glued in a longer name
                                  "InspectorName1", "InspectorName2", "NTDUpdatedby", "LastUpdatedby", "EntryUserid",
                                  "ApprovedBy1", "ContactPerson", "AdminReportingPerson", "Merchandiser", "Owner",
                                  # the first live run, 2026-10-15: people named by their role
                                  "DMM", "ResponsibleDMM", "ExDirectorCode", "StoreManagerCode", "AccountsOfficerCode",
                                  "CommercialOfficerCode", "ResponsibleIncharge", "LineIncharge", "LineInChargeID",
                                  "ResponsibleUnitHead", "UnitHead", "MarketingHead", "Merchant", "ApproveAuth",
                                  "CommercialApproveAuth", "ApproveAuthority", "OperatorCode", "DriverLecence",
                                  "ReceiveTo", "AccNo", "ApprovedRemarks"])
def test_columns_named_for_a_person_or_a_login_are_never_sampled(name):
    assert sample_skipped(name), name


@pytest.mark.parametrize("name", ["ShipmentStatus", "Status", "Grade", "Buyer", "BuyerStyleNo", "UnitCode",
                                  "Kind", "Flag", "OrderType", "Season", "ApprovalStatus", "Designation",
                                  "StaffCategory", "ContactStatus", "TINNo", "Currency", "EntryType", "AccountSet",
                                  "DocStatus", "VisitorType"])
def test_code_and_status_columns_are_still_sampled(name):
    assert not sample_skipped(name), name


def test_the_skip_list_applies_to_sampling_only():
    """A skipped column is still an ordinary column: discovery records it, the SQL model may read it."""
    cols = [("dbo", "Status", "Code", "varchar", 20, True, 1), ("dbo", "Status", "CreatedBy", "varchar", 50, True, 2),
            ("dbo", "Status", "UserName", "nvarchar", 40, True, 3), ("dbo", "Status", "EmpName", "varchar", 60, True, 4)]
    tables = [("dbo", "Status", "U", 40)]
    assert sample_candidates(tables, cols, 200_000) == [("dbo", "Status", "Code")]
    doc = assemble("DB", "X", tables, cols, [], [], [], {})
    t = doc["tables"][0]
    assert [c["name"] for c in t["columns"]] == ["Code", "CreatedBy", "UserName", "EmpName"]
    assert not any(c["sensitive"] for c in t["columns"])
    assert not is_sensitive("CreatedBy") and not is_sensitive("UserName")


def test_candidates_from_doc_mirror_the_live_rule():
    """--count-only works from the JSON on disk; it must pick exactly the columns a live run would read from
    the same sys.* rows, nvarchar lengths included (assemble() stores characters, sys.columns says bytes)."""
    tables = TABLES + [("dbo", "Codes", "U", 10)]
    columns = COLUMNS + [
        ("dbo", "Codes", "C50", "nvarchar", 100, 1, 1),    # nvarchar(50) = 100 bytes: sampled
        ("dbo", "Codes", "C60", "nvarchar", 120, 1, 2),    # nvarchar(60) = 120 bytes: too long
        ("dbo", "Codes", "V100", "varchar", 100, 1, 3),    # varchar(100): sampled
        ("dbo", "Codes", "V101", "varchar", 101, 1, 4),    # too long
        ("dbo", "Codes", "N10", "nchar", 20, 1, 5),        # nchar(10): sampled
        ("dbo", "Codes", "Memo", "nvarchar", -1, 1, 6),    # nvarchar(max): never
        ("dbo", "Codes", "Qty", "int", 4, 1, 7),           # not text
    ]
    doc = assemble("DB", "X", tables, columns, [], [], [], {})
    live = sample_candidates(tables, columns, 200_000)
    assert candidates_from_doc(doc) == live
    assert {c for s, t, c in live if t == "Codes"} == {"C50", "V100", "N10"}
    only = {"dbo.codes"}
    assert candidates_from_doc(doc, only) == sample_candidates(tables, columns, 200_000, only=only)
    assert candidates_from_doc(doc, set()) == []
    assert candidates_from_doc(doc, max_rows=100) == sample_candidates(tables, columns, 100)
    # ExportOrderID and ShipmentStatus; Grade (EmpMobileNo is sensitive); BuyerName is skipped
    assert queries_by_table(live) == [("dbo.Codes", 3), ("dbo.ExportOrder", 2)]      # dbo.Employee: a people table


def _people(**kw):
    from ragbot.data.catalog import Catalog
    return Catalog("HRDemo", "tsql", "X", [], [], **kw)


def test_a_people_database_is_refused_and_an_ordinary_one_is_not():
    assert samples_refused(_people()) is None
    assert "sensitive_patterns" in samples_refused(_people(sensitive_patterns=("password", "pwd")))
    assert "restricted_to" in samples_refused(_people(restricted_to=("ka-hr",)))
    assert "no catalog" in samples_refused(None)


def test_the_query_budget():
    assert budget_refused(150, 150) is None and budget_refused(0, 150) is None
    assert "151" in budget_refused(151, 150) and "--max-queries 150" in budget_refused(151, 150)


def test_a_people_table_in_any_database_is_never_sampled():
    """The ERP, dashboard and production databases hold employee masters and attendance too (the S4 review): their
    values never reach the SQL prompt or the schema index, whichever catalog they sit in."""
    tables = [("dbo", "DailyAttendance_All_Company", "U", 50), ("dbo", "vwEmployee", "U", 40),
              ("dbo", "Status", "U", 40)]
    cols = [("dbo", t, "Code", "varchar", 20, True, 1) for _, t, _, _ in tables]
    assert sample_candidates(tables, cols, 200_000) == [("dbo", "Status", "Code")]


@pytest.mark.parametrize("values", [["Open", "Mr. Karim, Cell: 01711000000"], ["USD", "Mr. Rahim"], ["a@b.com"],
                                    ["8801911000000"], ["Md. Abdul", "BDT"], ["Mst Rina"], ["Engr. Kabir"]])
def test_a_column_whose_values_hold_a_person_keeps_none(values):
    from ragbot.data.discovery import samples_look_personal
    assert samples_look_personal(values), values


@pytest.mark.parametrize("values", [["USD", "BDT", "EUR"], ["Dr", "Cr"], ["MD"], ["Approved", "Pending"],
                                    ["123456789012"], ["Majumder Textile Ltd"], ["PO-01711"]])
def test_ordinary_values_are_kept(values):
    from ragbot.data.discovery import samples_look_personal
    assert not samples_look_personal(values), values


def test_personal_values_are_not_carried_and_are_dropped_at_load():
    from ragbot.data.discovery import carried_samples
    previous = {"tables": [{"name": "dbo.APOMaster", "columns": [
        {"name": "APORef", "samples": ["Mr. Karim, Cell: 01711000000", "PO 12"]},
        {"name": "Kind", "samples": ["A", "B"]}]}]}
    assert carried_samples(previous) == {("dbo", "APOMaster", "Kind"): ["A", "B"]}
