"""G2: sample column values only for the raw tables the SQL model actually needs, and keep earlier
samples when discovery runs again. Then the fence before the owner-approved run: people databases refused,
--count-only offline, --max-queries, the person/login skip list, and no samples in a people catalog."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ragbot.data import catalog as catalog_mod
from ragbot.data.catalog import Catalog, Table, View, load_catalogs
from ragbot.data.discovery import assemble, carried_samples, sample_candidates, samples_refused
from ragbot.data.schema_index import table_document
from ragbot.data.schema_usage import SELECT_HEADER, used_tables
from ragbot.logs import append_row

ROOT = Path(__file__).resolve().parents[1]
OPEN = ("password", "pwd", "token", "secret")

TABLES = [("dbo", "Status", "U", 50), ("dbo", "Big", "U", 5_000_000), ("dbo", "Other", "U", 20)]
COLUMNS = [("dbo", "Status", "Code", "varchar", 20, True, 1), ("dbo", "Big", "Kind", "varchar", 20, True, 1),
           ("dbo", "Other", "Flag", "varchar", 10, True, 1), ("dbo", "Status", "Email", "varchar", 50, True, 2)]


def test_only_the_listed_tables_are_sampled():
    assert sample_candidates(TABLES, COLUMNS, 200_000) == [("dbo", "Status", "Code"), ("dbo", "Other", "Flag")]
    assert sample_candidates(TABLES, COLUMNS, 200_000, only={"dbo.status"}) == [("dbo", "Status", "Code")]
    assert sample_candidates(TABLES, COLUMNS, 200_000, only=set()) == []


def test_earlier_samples_survive_a_new_run_and_fresh_ones_replace_them():
    old = assemble("DB", "X", TABLES, COLUMNS, [], [], [], {("dbo", "Status", "Code"): ["A", "B"],
                                                           ("dbo", "Other", "Flag"): ["Y"]})
    kept = carried_samples(old)
    assert kept == {("dbo", "Status", "Code"): ["A", "B"], ("dbo", "Other", "Flag"): ["Y"]}
    new = assemble("DB", "X", TABLES, COLUMNS, [], [], [], {**kept, ("dbo", "Status", "Code"): ["A", "B", "C"]})
    cols = {(t["name"], c["name"]): c.get("samples") for t in new["tables"] for c in t["columns"]}
    assert cols[("dbo.Status", "Code")] == ["A", "B", "C"] and cols[("dbo.Other", "Flag")] == ["Y"]
    assert carried_samples(None) == {}


def test_used_tables_come_from_schema_selection_and_generated_sql(tmp_path):
    cat = Catalog("DB", "tsql", "SQLSERVER_CONN_X", [], [View("rag.vw_Orders", "g", "d", [], {})],
                  tables=[Table("dbo.Status"), Table("dbo.Other"), Table("dbo.Never")])
    append_row("schema_select.csv", SELECT_HEADER, ["t", "u", "DB", "dbo.Status;dbo.Other", "q1"], folder=tmp_path)
    append_row("schema_select.csv", SELECT_HEADER, ["t", "u", "DB", "dbo.Status", "q2"], folder=tmp_path)
    append_row("schema_select.csv", SELECT_HEADER, ["t", "u", "OtherDB", "dbo.Never", "q3"], folder=tmp_path)
    header = ["ts", "user", "scope", "engine", "tool", "rows", "ms", "error", "params", "sql", "sql_exec"]
    append_row("sql.csv", header, ["t", "u", "", "sqlserver", "generated", 1, 5, "", {},
                                   "SELECT TOP (5) Flag FROM dbo.Other JOIN rag.vw_Orders o ON 1=1", ""], folder=tmp_path)
    append_row("sql.csv", header, ["t", "u", "", "sqlserver", "eo_by_id", 1, 5, "", {}, "SELECT * FROM dbo.Never", ""],
               folder=tmp_path)
    assert used_tables(cat, folder=tmp_path) == ["dbo.Other", "dbo.Status"]   # 2 uses each; views and fixed tools ignored


# --- the fence (scripts/discover_schema.py) --------------------------------------------------------------

class _Untouchable:
    """A connection (or pyodbc module) that fails the test the moment anything uses it."""
    def __getattr__(self, name):
        if name.startswith("__"):                # the import machinery's own probes (__spec__ ...)
            raise AttributeError(name)
        raise AssertionError(f"the database was used (.{name})")


class _Connected(Exception):
    pass


def _refuse_connect(a):
    raise _Connected(a.friendly)


@pytest.fixture
def ds(monkeypatch, tmp_path):
    """scripts/discover_schema.py, loaded fresh, writing to tmp_path/discovered, with every way to a database
    replaced by something that records the attempt and fails on use."""
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))           # for its `import _path`
    spec = importlib.util.spec_from_file_location("discover_schema_under_test", ROOT / "scripts" / "discover_schema.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "DISCOVERED", tmp_path / "discovered")
    mod.connected = []
    monkeypatch.setattr(mod, "_connect", lambda a: mod.connected.append(a.friendly) or _Untouchable())
    monkeypatch.setitem(sys.modules, "pyodbc", _Untouchable())
    return mod


def _write_doc(tmp_path, database="DB", tables=TABLES, columns=COLUMNS, samples=None):
    folder = tmp_path / "discovered"
    folder.mkdir(parents=True, exist_ok=True)
    doc = assemble(database, "X", tables, columns, [], [], [], samples or {})
    (folder / f"{database}.json").write_text(json.dumps(doc), encoding="utf-8")
    return doc


def test_count_only_reads_the_file_on_disk_and_never_the_database(ds, tmp_path, monkeypatch, capsys):
    _write_doc(tmp_path)
    monkeypatch.setattr(ds, "_catalog", lambda friendly: Catalog("DB", "tsql", "SQLSERVER_CONN_X", [], []))
    assert ds.main(["SQLSERVER_CONN_X", "DB", "--count-only"]) == 0
    out = capsys.readouterr().out
    assert "dbo.Other: 1" in out and "dbo.Status: 1" in out and "dbo.Big" not in out      # 5M rows: never
    assert "DB: 2 sampling queries over 2 table(s)" in out and "no database was queried" in out
    assert "within --max-queries 150" in out
    assert ds.connected == []


def test_count_only_follows_the_logs_and_reports_the_cap(ds, tmp_path, monkeypatch, capsys):
    _write_doc(tmp_path)
    (tmp_path / "logs").mkdir()
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    append_row("schema_select.csv", SELECT_HEADER, ["t", "u", "DB", "dbo.Status", "q"], folder=tmp_path / "logs")
    cat = Catalog("DB", "tsql", "SQLSERVER_CONN_X", [], [], tables=[Table("dbo.Status"), Table("dbo.Other"), Table("dbo.Big")])
    monkeypatch.setattr(ds, "_catalog", lambda friendly: cat)
    assert ds.main(["SQLSERVER_CONN_X", "DB", "--count-only", "--tables-from-logs"]) == 0
    out = capsys.readouterr().out
    assert "tables from the logs (1): dbo.status" in out
    assert "dbo.Status: 1" in out and "dbo.Other" not in out
    assert "DB: 1 sampling queries over 1 table(s)" in out
    assert ds.main(["SQLSERVER_CONN_X", "DB", "--count-only", "--max-queries", "1", "--tables", "dbo.Status,dbo.Other"]) == 0
    out = capsys.readouterr().out
    assert "DB: 2 sampling queries" in out and "would be refused: 2 sampling queries exceed --max-queries 1" in out
    assert ds.connected == []


def test_count_only_names_the_refusal_of_a_people_database(ds, tmp_path, monkeypatch, capsys):
    _write_doc(tmp_path, "HRDemo")
    monkeypatch.setattr(ds, "_catalog", lambda friendly: Catalog("HRDemo", "tsql", "SQLSERVER_CONN_X", [], [], sensitive_patterns=OPEN))
    assert ds.main(["SQLSERVER_CONN_X", "HRDemo", "--count-only"]) == 0
    assert "would be refused: HRDemo declares its own sensitive_patterns" in capsys.readouterr().out
    assert ds.main(["SQLSERVER_CONN_X", "Missing", "--count-only"]) == 1        # no discovery file: nothing to count
    assert ds.connected == []


def test_a_people_database_is_refused_before_anything_connects(ds, monkeypatch, capsys):
    people = Catalog("HRDemo", "tsql", "SQLSERVER_CONN_X", [], [], sensitive_patterns=OPEN)
    monkeypatch.setattr(ds, "_catalog", lambda friendly: people)
    assert ds.main(["SQLSERVER_CONN_X", "HRDemo", "--json", "--samples"]) == 2
    assert "refused: HRDemo declares its own sensitive_patterns" in capsys.readouterr().err
    restricted = Catalog("Pay", "tsql", "SQLSERVER_CONN_X", [], [], restricted_to=("ka-hr",))
    monkeypatch.setattr(ds, "_catalog", lambda friendly: restricted)
    assert ds.main(["SQLSERVER_CONN_X", "Pay", "--json", "--samples", "--tables", "dbo.x"]) == 2
    monkeypatch.setattr(ds, "_catalog", lambda friendly: None)
    assert ds.main(["SQLSERVER_CONN_X", "Unknown", "--json", "--samples"]) == 2
    assert "no catalog" in capsys.readouterr().err
    assert ds.connected == []
    # the override is the only way through, and it is the owner's to pass
    monkeypatch.setattr(ds, "_catalog", lambda friendly: people)
    monkeypatch.setattr(ds, "_connect", _refuse_connect)
    with pytest.raises(_Connected):
        ds.main(["SQLSERVER_CONN_X", "HRDemo", "--json", "--samples", "--allow-people-samples"])
    assert "drops these values when it loads" in capsys.readouterr().err


def test_without_samples_a_people_database_still_gets_its_metadata(ds, monkeypatch):
    monkeypatch.setattr(ds, "_catalog", lambda friendly: pytest.fail("no catalog check without --samples"))
    monkeypatch.setattr(ds, "_connect", _refuse_connect)
    with pytest.raises(_Connected):
        ds.main(["SQLSERVER_CONN_X", "HR", "--database", "HR", "--json"])


class _MetaCursor:
    """Answers discovery's five sys.* queries from synthetic rows and records every data query."""
    def __init__(self, tables, columns):
        self.tables, self.columns, self.data_queries, self._rows = tables, columns, [], []

    def execute(self, sql):
        if "SELECT DISTINCT TOP 31" in sql:
            self.data_queries.append(sql)
            self._rows = [("B",), ("A",)]
        elif "sys.extended_properties" in sql or "sys.foreign_keys" in sql or "is_primary_key" in sql:
            self._rows = []
        elif "c.max_length" in sql:
            self._rows = list(self.columns)
        elif "sys.partitions" in sql:
            self._rows = list(self.tables)
        else:
            raise AssertionError(f"unexpected query: {sql}")
        return self

    def fetchall(self):
        return list(self._rows)


def test_a_run_over_the_query_cap_stops_before_its_first_sampling_query(ds, tmp_path, capsys):
    assert ds._args(["X", "DB"]).max_queries == 150
    cur = _MetaCursor(TABLES, COLUMNS + [("dbo", "Status", "CreatedBy", "varchar", 50, True, 3),
                                         ("dbo", "Other", "UserName", "nvarchar", 40, True, 2)])
    with pytest.raises(SystemExit) as e:
        ds.write_json(SimpleNamespace(), cur, ds._args(["X", "DB", "--json", "--samples", "--max-queries", "1"]), None)
    assert e.value.code == 2 and cur.data_queries == []
    assert not (tmp_path / "discovered" / "DB.json").exists()                  # nothing written either
    assert "2 sampling queries exceed --max-queries 1" in capsys.readouterr().err
    # at the cap it runs: one query per candidate, none for the login and person columns
    ds.write_json(SimpleNamespace(), cur, ds._args(["X", "DB", "--json", "--samples", "--max-queries", "2"]), None)
    assert len(cur.data_queries) == 2
    assert not any("CreatedBy" in q or "UserName" in q or "Email" in q for q in cur.data_queries)
    doc = json.loads((tmp_path / "discovered" / "DB.json").read_text(encoding="utf-8"))
    sampled = {(t["name"], c["name"]): c["samples"] for t in doc["tables"] for c in t["columns"] if c.get("samples")}
    assert sampled == {("dbo.Status", "Code"): ["A", "B"], ("dbo.Other", "Flag"): ["A", "B"]}


# --- defence in depth: a people catalog never carries a sampled value ---------------------------------------

HR_TABLES = [("dbo", "Employee", "U", 100)]
HR_COLUMNS = [("dbo", "Employee", "Gender", "varchar", 10, True, 1),
              ("dbo", "Employee", "Designation", "varchar", 50, True, 2)]
HR_SAMPLES = {("dbo", "Employee", "Gender"): ["Female", "Male"],
              ("dbo", "Employee", "Designation"): ["Operator", "Sewing Supervisor"]}


def test_a_people_catalog_drops_every_sample_at_load(tmp_path):
    """Whatever an --allow-people-samples run wrote, the values reach neither the SQL prompt nor the schema
    index; an ordinary catalog over the same file keeps them."""
    _write_doc(tmp_path, "HRDemo", HR_TABLES, HR_COLUMNS, HR_SAMPLES)
    _write_doc(tmp_path, "Plain", *_renamed("Line"))                     # Employee is a people table anywhere
    (tmp_path / "hr.yaml").write_text("database: HRDemo\ndialect: tsql\nconnection_env: X\nviews: []\n"
                                      "sensitive_patterns: [password, pwd, token, secret]\n", encoding="utf-8")
    (tmp_path / "plain.yaml").write_text("database: Plain\ndialect: tsql\nconnection_env: X\nviews: []\n",
                                         encoding="utf-8")
    cats = catalog_mod._load_catalogs(tmp_path, ("g2-samples",))
    hr, plain = cats["HRDemo"], cats["Plain"]
    assert not any("samples" in c for t in hr.tables for c in t.columns)
    rendered = hr.render_selected(["dbo.Employee"])
    assert "Gender" in rendered and "Designation" in rendered               # the columns stay, their values go
    for value in ("Female", "Male", "Operator", "Sewing Supervisor", "values:"):
        assert value not in rendered, value
        assert value not in table_document(hr.table("dbo.Employee")), value
    assert "(values: Female, Male)" in plain.render_selected(["dbo.Line"])
    assert "Sewing Supervisor" in table_document(plain.table("dbo.Line"))
    on_disk = json.loads((tmp_path / "discovered" / "HRDemo.json").read_text(encoding="utf-8"))
    assert any(c.get("samples") for t in on_disk["tables"] for c in t["columns"])   # the file is left as written


def _renamed(table):
    """HR_TABLES/COLUMNS/SAMPLES with dbo.Employee renamed, for a table that is not a people table."""
    return ([(s, table, k, r) for s, _, k, r in HR_TABLES], [(s, table, *rest) for s, _, *rest in HR_COLUMNS],
            {(s, table, c): v for (s, _, c), v in HR_SAMPLES.items()})


def test_a_people_table_in_an_ordinary_catalog_drops_its_samples_at_load(tmp_path):
    """An employee master in the ERP or the dashboard: never sampled now (discovery.sample_candidates), and a
    file sampled before that rule loses the values at load and on the next run."""
    _write_doc(tmp_path, "Plain", HR_TABLES, HR_COLUMNS, HR_SAMPLES)
    (tmp_path / "plain.yaml").write_text("database: Plain\ndialect: tsql\nconnection_env: X\nviews: []\n",
                                         encoding="utf-8")
    plain = catalog_mod._load_catalogs(tmp_path, ("g2-people-table",))["Plain"]
    assert not any("samples" in c for t in plain.tables for c in t.columns)
    assert "Female" not in plain.render_selected(["dbo.Employee"])
    assert "Operator" not in table_document(plain.table("dbo.Employee"))
    on_disk = json.loads((tmp_path / "discovered" / "Plain.json").read_text(encoding="utf-8"))
    assert carried_samples(on_disk) == {}


def test_the_real_people_catalogs_are_refused_and_the_others_are_not():
    cats = load_catalogs()
    for name in ("HR", "HWATT", "Hrms5Misami"):
        assert samples_refused(cats[name]), name
    assert samples_refused(cats["BitopiSplint"]) is None and samples_refused(cats["Production"]) is None


def test_a_run_that_opens_another_database_under_this_name_is_refused(ds, monkeypatch, capsys):
    """Review of 2026-10-15: "... Production --database HR --samples" would sample HR into Production.json, past
    the people fence, because the catalog was looked up by the file name only. No flag overrides it."""
    production = Catalog("Production", "tsql", "SQLSERVER_CONN_PRODUCTION", [], [])
    monkeypatch.setattr(ds, "_catalog", lambda friendly: production)
    for argv in (["SQLSERVER_CONN_PRODUCTION", "Production", "--json", "--samples", "--database", "HR"],
                 ["SQLSERVER_CONN_OTHER", "Production", "--json", "--samples", "--allow-people-samples"]):
        assert ds.main(argv) == 2
        assert "refused: this run opens" in capsys.readouterr().err
    assert ds.connected == []


def test_an_earlier_sample_of_a_skip_listed_column_is_not_carried():
    """A skip-listed column is never sampled again, so a carried sample would stay in the file for good."""
    from ragbot.data.discovery import carried_samples
    previous = {"tables": [{"name": "dbo.Courier", "columns": [
        {"name": "ContactPerson", "samples": ["Rahim", "Karim"]},
        {"name": "Status", "samples": ["Open", "Closed"]}]}]}
    assert carried_samples(previous) == {("dbo", "Courier", "Status"): ["Open", "Closed"]}
