"""C2: curated views + discovered tables. Without a selection the prompt is exactly what it was before
discovery existed; sensitive or excluded tables are never offered; sensitive columns never rendered."""
import json

import yaml

from ragbot.data.catalog import MAX_RENDERED_COLUMNS, load_catalogs

YAML = {
    "database": "DemoDB", "dialect": "tsql", "connection_env": "SQLSERVER_CONN_DEMO",
    "rules": ["Use TOP (n)."],
    "views": [{"name": "rag.vw_Order", "grain": "one row per order", "description": "Orders.",
               "key_columns": ["OrderID"], "columns": {"OrderID": "order key"}}],
    "exclude_tables": ["dbo.*Back"],
}
DISCOVERED = {"database": "DemoDB", "tables": [
    {"name": "dbo.Buyer", "kind": "table", "rows": 300, "description": "Buyers", "primary_key": ["BuyerID"],
     "columns": [{"name": "BuyerID", "type": "int"}, {"name": "BuyerName", "type": "varchar(100)"},
                 {"name": "Email", "type": "varchar(100)", "sensitive": True},
                 {"name": "Country", "type": "varchar(40)", "samples": ["BD", "US"]}],
     "foreign_keys": [], "sensitive": False},
    {"name": "dbo.ExportOrderBack", "kind": "table", "rows": 4_000_000, "columns": [{"name": "X", "type": "int"}]},
    {"name": "dbo.EmployeeSalary", "kind": "table", "rows": 10, "columns": [], "sensitive": True},
    {"name": "dbo.Wide", "kind": "table", "rows": 5,
     "columns": [{"name": f"C{i}", "type": "int"} for i in range(MAX_RENDERED_COLUMNS + 5)]},
]}


def _setup(tmp_path, discovered=True):
    (tmp_path / "demo.yaml").write_text(yaml.safe_dump(YAML), encoding="utf-8")
    if discovered:
        (tmp_path / "discovered").mkdir()
        (tmp_path / "discovered" / "DemoDB.json").write_text(json.dumps(DISCOVERED), encoding="utf-8")
    return load_catalogs(tmp_path)["DemoDB"]


def test_render_without_selection_is_unchanged_by_discovery(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    plain = _setup(tmp_path / "a", discovered=False)
    with_disc = _setup(tmp_path / "b")
    assert with_disc.tables and not plain.tables
    assert with_disc.render() == plain.render()


def test_offered_and_allowed_exclude_sensitive_and_excluded_tables(tmp_path):
    cat = _setup(tmp_path)
    offered = {t.name for t in cat.offered_tables}
    assert offered == {"dbo.Buyer", "dbo.Wide"}
    assert cat.allowed_names == {"rag.vw_order", "dbo.buyer", "dbo.wide"}


def test_render_selected_shows_only_safe_columns(tmp_path):
    cat = _setup(tmp_path)
    text = cat.render_selected(["dbo.Buyer", "dbo.EmployeeSalary", "dbo.ExportOrderBack"],
                               ["dbo.X.BuyerID = dbo.Buyer.BuyerID"])
    assert "dbo.Buyer (300 rows) — Buyers" in text and "Key: BuyerID" in text
    assert "Country: varchar(40) (values: BD, US)" in text
    assert "Email" not in text                        # sensitive column never rendered
    assert "EmployeeSalary" not in text and "ExportOrderBack" not in text   # not offered
    assert "Likely joins" in text and "dbo.X.BuyerID = dbo.Buyer.BuyerID" in text
    assert "dbo.Buyer" not in cat.render()          # the static part never lists discovered tables
    assert cat.render_selected(["dbo.EmployeeSalary"]) == ""


def test_wide_tables_are_capped(tmp_path):
    text = _setup(tmp_path).render_selected(["dbo.Wide"])
    assert f"C{MAX_RENDERED_COLUMNS - 1}:" in text and f"C{MAX_RENDERED_COLUMNS}:" not in text
    assert "5 more columns not shown" in text
