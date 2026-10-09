"""P2.1: the value index — recorded names matched against a question's spelling, scoped, no model call."""
from datetime import datetime

import pytest

import ragbot.data.aggregates as agg
import ragbot.data.values as values
from ragbot.auth.models import Scope
from ragbot.data.values import ValueEntry, known_values_lines, lookup, resolve

STORES = [
    ValueEntry("store", "Maintenance Store (Misami)", "2", "", "Inventory", "rag.vw_StockBalance.StoreName"),
    ValueEntry("store", "Maintenance Store (Tarasima)", "4", "TAL", "Inventory", "rag.vw_StockBalance.StoreName"),
    ValueEntry("store", "Central Store (Misami)", "1", "", "Inventory", "rag.vw_StockBalance.StoreName"),
    ValueEntry("store", "Central Store (Tarasima)", "3", "TAL", "Inventory", "rag.vw_StockBalance.StoreName"),
    ValueEntry("store", "Misami Production Floor", "7", "", "Inventory", "rag.vw_StockBalance.StoreName"),
    ValueEntry("store", "General Store (RHL)", "56", "RHL", "Inventory", "rag.vw_StockBalance.StoreName"),
]
CATS = [ValueEntry("item_category", "Goods", "1", "GROUP", "Inventory", "rag.vw_Item.ItemCategory"),
        ValueEntry("item_category", "Chemical", "40", "GROUP", "Inventory", "rag.vw_Item.ItemCategory")]
ALL = Scope.unrestricted()
Q8OCT = ("please find the closing balance of 1178108 this item code for nonproduction iten and companyid=02 "
         "and store name mantainance store of misami")


def test_a_misspelled_store_with_its_company_resolves_to_the_recorded_name():
    hit, cands = resolve(Q8OCT, ALL, "store", STORES)
    assert hit is not None and hit.value == "Maintenance Store (Misami)" and hit.code == "2"
    assert hit.span == "mantainance store" and hit.score == 1.0          # the alias in entities.yaml
    assert cands == []


def test_a_close_spelling_without_an_alias_resolves_by_similarity():
    hit, _ = resolve("stock of 1178108 in the maintanence store of tarasima", ALL, "store", STORES)
    assert hit is not None and hit.value == "Maintenance Store (Tarasima)" and 0.85 <= hit.score < 1.0


def test_the_same_store_at_several_companies_is_ambiguous_without_a_company():
    hit, cands = resolve("closing balance of 1178108 in the maintenance store", ALL, "store", STORES)
    assert hit is None and set(cands) == {"Maintenance Store (Misami)", "Maintenance Store (Tarasima)"}


def test_short_words_and_unrelated_text_match_nothing():
    assert resolve("stock of 1178108 at the store", ALL, "store", STORES) == (None, [])
    assert resolve("how many goods received this week", ALL, "store", STORES) == (None, [])
    assert lookup("Grease is a goods item", ALL, "item_category", CATS)[0].value == "Goods"
    assert lookup("the chemicals store", ALL, "item_category", CATS) == []        # 'chemicals' is not 'Chemical' whole


def test_scope_hides_other_companies_stores_and_companies_without_a_code():
    tal = Scope(factories=["TAL"])
    assert resolve(Q8OCT, tal, "store", STORES) == (None, [])                    # Misami: no factory code
    hit, _ = resolve("stock in the central store of tarasima", tal, "store", STORES)
    assert hit is not None and hit.value == "Central Store (Tarasima)"
    assert resolve("stock in the general store of RHL", tal, "store", STORES) == (None, [])
    assert lookup("Goods", tal, "item_category", CATS)[0].value == "Goods"      # GROUP values: everyone


def test_known_values_lines_name_the_column_and_the_company():
    lines = known_values_lines(Q8OCT, ALL, ["Inventory", "SystemManager"], entries=STORES)
    assert lines[0] == "'mantainance store' in the question = StoreName 'Maintenance Store (Misami)' (code 2) in rag.vw_StockBalance"
    assert lines[-1].startswith("the company named in the question = CompanyID '02'")
    assert known_values_lines(Q8OCT, ALL, ["FM"], entries=STORES)[0].startswith("the company named")   # not FM's value


def test_index_loads_from_the_aggregate_copies_and_notices_a_refresh(tmp_path):
    db = tmp_path / "agg.db"
    spec = {"name": "values_stores", "entity": "store", "value_column": "StoreName", "code_column": "StoreID",
            "scope_column": "Factory", "database": "Inventory", "target": "rag.vw_StockBalance.StoreName"}
    agg.write("values_stores", [(["StoreID", "StoreName", "Kind", "Factory"],
                                 [["2", "Maintenance Store (Misami)", "Inventory Location", "GROUP"],
                                  ["4", "Maintenance Store (Tarasima)", "Inventory Location", "GROUP"]])], ["StoreName"], [], db)
    values.clear_cache()
    entries = values.load_index(db, [spec])
    assert [e.value for e in entries] == ["Maintenance Store (Misami)", "Maintenance Store (Tarasima)"]
    assert entries[0].factory == "" and entries[1].factory == "TAL"             # from the bracketed company
    assert isinstance(values.as_of(db), datetime) or values.as_of(db) is None
    agg.write("values_stores", [(["StoreID", "StoreName", "Kind", "Factory"], [["9", "New Store", "Inventory Location", "GROUP"]])],
              ["StoreName"], [], db)
    values.clear_cache()
    assert [e.value for e in values.load_index(db, [spec])] == ["New Store"]


def test_no_copy_means_no_entries(tmp_path):
    values.clear_cache()
    assert values.load_index(tmp_path / "none.db", [{"name": "values_x", "entity": "store", "value_column": "V"}]) == []
