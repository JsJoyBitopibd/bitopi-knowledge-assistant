"""A catalog with `enabled: false` stays on disk but is not loaded, so the router never offers a database that is
not on the server in use (2026-10-07: four dev-only databases after the switch to the live server)."""
from ragbot.data.catalog import _load_catalogs

BODY = "database: {db}\ndescription: test\ndialect: tsql\nconnection_env: SQLSERVER_CONN\nkeywords: [x]\n"


def test_disabled_catalog_is_not_loaded(tmp_path):
    (tmp_path / "Kept.yaml").write_text(BODY.format(db="Kept"), encoding="utf-8")
    (tmp_path / "Gone.yaml").write_text("enabled: false\n" + BODY.format(db="Gone"), encoding="utf-8")
    (tmp_path / "Explicit.yaml").write_text("enabled: true\n" + BODY.format(db="Explicit"), encoding="utf-8")
    assert sorted(_load_catalogs(tmp_path, ("enabled",))) == ["Explicit", "Kept"]


def test_include_disabled_loads_every_catalog(tmp_path):
    (tmp_path / "Kept.yaml").write_text(BODY.format(db="Kept"), encoding="utf-8")
    (tmp_path / "Gone.yaml").write_text("enabled: false\n" + BODY.format(db="Gone"), encoding="utf-8")
    assert sorted(_load_catalogs(tmp_path, ("all",), True)) == ["Gone", "Kept"]
