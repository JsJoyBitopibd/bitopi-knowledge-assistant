"""G1: code columns in raw tables (a Buyer code such as C/09/7) point the SQL model at the table that
names them (dbo.Contact_Master.ContactName), per table, via the catalog's `hints:`."""
import pytest

from ragbot.data.catalog import Catalog, Hint, Table, _load_catalogs
from ragbot.data.schema_index import fk_neighbours, join_hints

HINT = Hint("Buyer", "dbo.Contact_Master.ContactID", ["dbo.tblSampleRequestMaster"],
            "a buyer code such as C/09/7; the buyer's name is dbo.Contact_Master.ContactName")


def _cat():
    col = lambda n: {"name": n, "type": "varchar"}  # noqa: E731
    return Catalog("BitopiSplint", "tsql", "X", [], [], hints=[HINT], tables=[
        Table("dbo.tblSampleRequestMaster", rows=5000, columns=[col("SampleNo"), col("Buyer")]),
        Table("dbo.CheckAnit", rows=100, columns=[col("Buyer")]),          # a Buyer column that is not a code
        Table("dbo.Contact_Master", rows=6517, primary_key=["ContactID"], columns=[col("ContactID"), col("ContactName")]),
    ])


def test_a_hinted_table_pulls_in_the_table_that_names_its_codes():
    cat = _cat()
    assert fk_neighbours(["dbo.tblSampleRequestMaster"], cat) == ["dbo.Contact_Master"]
    assert fk_neighbours(["dbo.CheckAnit"], cat) == []                   # not listed: no guess


def test_hint_targets_come_before_foreign_key_neighbours_so_the_cap_keeps_them():
    cat = _cat()
    fks = [Table(f"dbo.Child{i}", rows=1, columns=[], foreign_keys=[
        {"columns": ["SampleNo"], "ref_table": "dbo.tblSampleRequestMaster", "ref_columns": ["SampleNo"]}]) for i in range(8)]
    cat.tables += fks
    assert fk_neighbours(["dbo.tblSampleRequestMaster"], cat)[0] == "dbo.Contact_Master"


def test_the_join_line_says_where_the_name_is():
    cat = _cat()
    hints = join_hints(["dbo.tblSampleRequestMaster", "dbo.Contact_Master"], cat)
    assert hints[0].startswith("dbo.tblSampleRequestMaster.Buyer = dbo.Contact_Master.ContactID")
    assert "ContactName" in hints[0]
    assert join_hints(["dbo.tblSampleRequestMaster"], cat) == []           # target not selected: no join line
    shown = cat.render_selected(["dbo.tblSampleRequestMaster", "dbo.Contact_Master"], hints)
    assert "Likely joins" in shown and "C/09/7" in shown


def test_the_catalog_loads_hints_and_rejects_a_malformed_one(tmp_path):
    base = "database: DB\ndialect: tsql\nconnection_env: X\nviews: []\n"
    (tmp_path / "db.yaml").write_text(base + "hints:\n  - column: Buyer\n    target: dbo.Contact_Master.ContactID\n"
                                      "    tables: [dbo.T]\n", encoding="utf-8")
    assert _load_catalogs(tmp_path, ("a",))["DB"].hints_for("DBO.T")[0].target_table == "dbo.Contact_Master"
    (tmp_path / "db.yaml").write_text(base + "hints:\n  - column: Buyer\n    target: Contact_Master\n    tables: [dbo.T]\n",
                                      encoding="utf-8")
    with pytest.raises(RuntimeError, match="hint"):
        _load_catalogs(tmp_path, ("b",))
