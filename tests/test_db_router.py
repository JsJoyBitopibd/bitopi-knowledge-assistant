"""J1: the database router picks the catalogs a question is about, with no model call."""
from pathlib import Path

import numpy as np
import pytest

import ragbot.data.schema_index as si
from ragbot.data.catalog import Catalog, Table, View
from ragbot.data.db_router import evidence, explain, keyword_score, pick_catalogs


def _cat(name, keywords=(), tables=(), views=()):
    return Catalog(name, "tsql", "X", [], list(views), keywords=list(keywords), tables=list(tables))


def _t(name, cols=("Id",), rows=10):
    return Table(name, "table", rows, "", [], [{"name": c, "type": "int"} for c in cols])


@pytest.fixture
def cats(monkeypatch):
    si._CACHE.clear()
    monkeypatch.setattr(si, "json_sha", lambda cat: "x")
    monkeypatch.setattr(si, "vectors_path", lambda db: Path("does-not-exist.npz"))
    view = View("rag.vw_ExportOrder", "one row per order", "Orders.", ["ExportOrderID"],
                {"ExportOrderID": "id", "PCD": "planned cut date"})
    yield {
        "BitopiSplint": _cat("BitopiSplint", ["order", "orders", "pcd", "buyer"], [_t("dbo.ExportOrder")], [view]),
        "Inventory": _cat("Inventory", ["grn", "stock", "fabric roll"], [_t("dbo.GRNMaster"), _t("dbo.StockChild")]),
        "HR": _cat("HR", ["employee", "manpower"], [_t("dbo.EmployeeInformation"), _t("dbo.ManpowerBudget")]),
    }
    si._CACHE.clear()


def test_keywords_match_whole_words_only(cats):
    assert keyword_score("How many orders ship this month?", cats["BitopiSplint"]) == 3     # 'orders', not 'order'
    assert keyword_score("What is the IT policy on PO approval?", _cat("X", ["po"])) == 3    # 'po' as a word
    assert keyword_score("What is the IT policy?", _cat("X", ["po"])) == 0                   # not inside 'policy'
    assert keyword_score("Which PCD is next?", cats["BitopiSplint"]) == 3 + 1                # keyword + view column


def test_picks_the_database_whose_vocabulary_the_question_uses(cats):
    assert list(pick_catalogs("How many GRN were received this week?", cats, k=1)) == ["Inventory"]
    assert list(pick_catalogs("Which orders have a PCD next week?", cats, k=1)) == ["BitopiSplint"]


def test_table_names_count_as_evidence(cats):
    # 'employee' is a keyword (3) and 'employee information' is the whole name of an HR table (+2)
    ev = evidence("Show the employee information list", cats)
    assert ev[0].database == "HR" and ev[0].name_share == 1.0 and ev[0].score == pytest.approx(5.0)
    assert ev[1].score < ev[0].score


def test_top_k_and_order(cats):
    picked = pick_catalogs("GRN stock and orders", cats, k=2)
    assert list(picked) == ["Inventory", "BitopiSplint"]                   # 6 points vs 3.5; HR left out
    assert set(pick_catalogs("anything", cats, k=3)) == set(cats)         # no more catalogs than k: all of them


def test_curated_catalog_wins_a_tie(cats):
    ev = evidence("something no catalog knows about", cats)
    assert ev[0].database == "BitopiSplint" and ev[0].score == 0.5 and ev[1].score == ev[2].score == 0


def _with_vectors(name, qvec_dir):
    """A schema index whose single table vector points along `qvec_dir` (cosine 1 for that question)."""
    idx = si.SchemaIndex([f"dbo.T_{name}"], si.KeywordIndex([f"dbo.T_{name}"], ["t"]))
    v = np.zeros((1, 3), dtype=np.float32)
    v[0, qvec_dir] = 1.0
    idx.vectors = v
    return idx


def test_vectors_rank_databases_once_most_have_them(cats):
    q = [0.0, 1.0, 0.0]
    # every database has vectors: Inventory's table is the closest, HR's second
    si._CACHE["Inventory"] = (("x", 0), _with_vectors("Inventory", 1))
    si._CACHE["HR"] = (("x", 0), _with_vectors("HR", 1))
    si._CACHE["HR"][1].vectors[0] = [0.0, 0.6, 0.8]
    si._CACHE["BitopiSplint"] = (("x", 0), _with_vectors("BitopiSplint", 0))
    ev = evidence("a question with none of the keywords", cats, qvec=q)
    assert [e.database for e in ev] == ["Inventory", "HR", "BitopiSplint"]
    assert ev[0].score == 2.0 and ev[1].score == 1.0 and ev[2].score == 0.5


def test_no_vector_bonus_while_most_databases_lack_vectors(cats):
    si._CACHE["Inventory"] = (("x", 0), _with_vectors("Inventory", 1))      # only 1 of 3 has vectors
    ev = evidence("a question with none of the keywords", cats, qvec=[0.0, 1.0, 0.0])
    assert ev[0].database == "BitopiSplint"                                  # curated tie-break, not the vector
    assert all(e.score in (0.0, 0.5) for e in ev)


def test_explain_is_one_readable_line(cats):
    line = explain("How many GRN this week?", cats)       # keyword 3 + the whole name of dbo.GRNMaster (2)
    assert line.startswith("Inventory 5.0 (kw 3, name 1.00)") and " > " in line
