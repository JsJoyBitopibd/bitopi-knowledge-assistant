"""C3: schema index — keyword and hybrid selection, FK expansion, stale vectors ignored, join hints.
Offline: a hashing embedder (bag of words) stands in for bge-m3."""
import hashlib
from types import SimpleNamespace as NS

import numpy as np
import pytest

import ragbot.data.schema_index as si
import ragbot.embed as embed
from ragbot.data.catalog import Catalog, Table
from ragbot.data.sensitive import words


def _t(name, cols, pk=None, fks=None, desc="", sensitive=False, rows=100):
    return Table(name, "table", rows, desc, pk or [], [{"name": c, "type": "int"} for c in cols], fks or [], sensitive)


TABLES = [
    _t("dbo.Supplier", ["SupplierID", "SupplierName", "CountryCode"], pk=["SupplierID"], desc="Fabric and trims suppliers"),
    _t("dbo.PurchaseOrder", ["POID", "SupplierID", "PODate", "Amount"], pk=["POID"],
       fks=[{"columns": ["SupplierID"], "ref_table": "dbo.Supplier", "ref_columns": ["SupplierID"]}]),
    _t("dbo.Buyer", ["BuyerID", "BuyerName"], pk=["BuyerID"], desc="Garment buyers (brands)"),
    _t("dbo.ExportOrder", ["ExportOrderID", "BuyerID", "PCD", "ShipDate"], pk=["ExportOrderID", "VersionNo"]),
    _t("dbo.Country", ["CountryCode", "CountryName"], pk=["CountryCode"]),
    _t("dbo.EmployeeSalary", ["EmpID", "Basic"], sensitive=True),
]


def _cat():
    return Catalog("Demo", "tsql", "SQLSERVER_CONN_DEMO", [], [], tables=list(TABLES))


def _hash_vec(text, dim=256):
    v = np.zeros(dim, dtype=np.float32)
    for w in words(text):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dim] += 1
    return v / (np.linalg.norm(v) or 1)


def test_column_words_rank_the_table_whose_columns_the_question_names():
    """P1: 'closing balance of item code for company id and store name' names four columns of one table."""
    stock = _t("dbo.InventoryStock", ["CompanyID", "LocationName", "ItemCode", "ItemName", "ClosingStock"])
    other = _t("dbo.ItemMaster", ["ItemCode", "ItemName", "UOM"])
    cat = Catalog("Demo", "tsql", "X", [], [], tables=[stock, other, _t("dbo.Unrelated", ["X", "Y"])])
    idx = si.build(cat, embed=False)
    assert idx.col_words[0] >= {"company", "id", "location", "name", "item", "code", "closing", "stock"}
    got = si._column_scores("closing stock of an item code for a company id and location name", idx.names, idx.col_words)
    assert got[0] == "dbo.InventoryStock" and "dbo.Unrelated" not in got     # one shared word is no evidence
    assert si.select_tables("closing stock of an item code for a company id and location name", cat, 1)[0] == "dbo.InventoryStock"


def test_table_notes_reach_the_index_and_its_cache_key(tmp_path, monkeypatch):
    cat = Catalog("Demo", "tsql", "X", [], [], tables=[_t("dbo.T1", ["A"]), _t("dbo.T2", ["B"]), _t("dbo.T3", ["C"])])
    first = si.get_index(cat)
    cat.table_notes = {"dbo.t2": "the stock ledger"}
    cat.tables[1].description = "the stock ledger"             # what _load_catalogs does with a note
    monkeypatch.setattr(si, "_RECHECK_SECONDS", 0.0)
    second = si.get_index(cat)
    assert second is not first                                   # the note changed the key: rebuilt
    assert second.search("stock ledger", 1) == ["dbo.T2"]


def test_a_catalog_may_set_its_own_k_and_the_total_is_capped():
    tables = [_t(f"dbo.Stock{i}", ["ItemCode", "Qty"]) for i in range(12)]
    cat = Catalog("Demo", "tsql", "X", [], [], tables=tables)
    assert len(si.select_tables("stock item code qty", cat, 3)) == 3
    cat.schema_rag_k = 10
    assert len(si.select_tables("stock item code qty", cat, 3)) == 10
    assert len(si.select_tables("stock item code qty", cat, 3, max_total=4)) == 4


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    si._CACHE.clear()
    monkeypatch.setattr(si, "vectors_path", lambda db: tmp_path / f"{db}.npz")
    sha = ["sha-1"]
    monkeypatch.setattr(si, "json_sha", lambda cat: sha[0])
    monkeypatch.setattr(embed, "get_embedder", lambda: NS(embed=lambda texts: [_hash_vec(t).tolist() for t in texts]))
    yield sha
    si._CACHE.clear()


def test_keyword_only_finds_table_by_split_column_words():
    got = si.select_tables("how many suppliers do we have", _cat(), k=2)
    assert got[0] == "dbo.Supplier"
    # no description to lean on: the plural in the question must still reach the singular name
    # (BM25 gives a word in half of a 2-document corpus zero weight, so use the full demo catalog)
    bare = Catalog("Demo2", "tsql", "E", [], [], tables=[*TABLES, _t("dbo.Factory", ["FactoryID", "FactoryName"])])
    assert si.select_tables("list all factories", bare, k=1) == ["dbo.Factory"]


def test_sensitive_tables_are_never_indexed():
    assert "dbo.EmployeeSalary" not in si.get_index(_cat()).names


def test_fk_neighbours_are_added():
    got = si.select_tables("purchase order amount by date", _cat(), k=1)
    assert got[0] == "dbo.PurchaseOrder" and "dbo.Supplier" in got      # joined via the declared FK


def test_hybrid_uses_saved_vectors_and_ignores_stale_ones(isolated):
    cat = _cat()
    si.build(cat, embed=True)
    si._CACHE.clear()
    idx = si.get_index(cat)
    assert idx.vectors is not None
    q = "brands we sell garments to"
    assert si.select_tables(q, cat, k=1, qvec=_hash_vec(q).tolist())[0] == "dbo.Buyer"
    isolated[0] = "sha-2"                       # discovery re-run: saved vectors no longer match
    si._CHECKED.clear()                         # get_index re-checks the files at most every 5 s (J5)
    assert si.get_index(cat).vectors is None


def test_join_hints_from_shared_key_names():
    hints = si.join_hints(["dbo.ExportOrder", "dbo.Buyer", "dbo.Supplier", "dbo.Country"], _cat())
    assert "dbo.ExportOrder.BuyerID = dbo.Buyer.BuyerID" in hints
    assert "dbo.Supplier.CountryCode = dbo.Country.CountryCode" in hints
    assert not any("VersionNo" in h for h in hints)   # composite keys give no hint


def test_no_discovered_tier_selects_nothing():
    assert si.select_tables("anything", Catalog("X", "tsql", "E", [], [])) == []


def test_table_name_matches_rank_above_column_matches():
    """Dozens of tables have a BuyerName column; the table NAMED for buyers must still come first."""
    noisy = [_t(f"dbo.Order{i}", ["OrderID", "BuyerName", "BuyerCode"]) for i in range(12)]
    cat = Catalog("Demo3", "tsql", "E", [], [], tables=[*noisy, _t("dbo.tblBuyerMaster", ["BuyerMasterID", "Buyer"])])
    assert si.select_tables("list all buyers", cat, k=3)[0] == "dbo.tblBuyerMaster"
