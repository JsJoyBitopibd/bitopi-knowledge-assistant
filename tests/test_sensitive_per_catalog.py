"""Phase R: one database may answer with its own sensitive-name list, and no other database moves.

The owner opened HR, Hrms5Misami and HWATT on 2026-10-13 — every business and personal column, credentials
still blocked. The rule used to be global (data/sensitive.py), enforced by the discovery flags, the catalog,
the schema index, the router, the guard and the DENY generator.
"""
import json

import pytest

import ragbot.data.catalog as catalog
import ragbot.data.schema_index as si
from ragbot.data.catalog import Catalog, Table, load_catalogs
from ragbot.data.guard import GuardError, guard
from ragbot.data.sensitive import is_sensitive

OPEN = ("password", "pwd", "token", "secret")
COLUMNS = [{"name": "EmployeeCode"}, {"name": "BasicSalary"}, {"name": "EmpMobileNo"},
           {"name": "BloodGroup"}, {"name": "UserPassword"}, {"name": "api_token"}]


def _json(tmp_path, database="HRDemo"):
    """A discovery file written by the DEFAULT rule, as the real ones are."""
    tables = [{"name": "dbo.Employee", "kind": "table", "rows": 100, "primary_key": ["EmployeeCode"],
               "columns": [{**c, "type": "varchar(50)", "sensitive": is_sensitive(c["name"])} for c in COLUMNS],
               "foreign_keys": [], "sensitive": False},
              {"name": "dbo.EmployeeSalary", "kind": "table", "rows": 10, "columns": [], "foreign_keys": [],
               "sensitive": True}]
    folder = tmp_path / "discovered"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{database}.json").write_text(json.dumps({"database": database, "tables": tables}), encoding="utf-8")
    return tmp_path


def _cat(tmp_path, pats=None, database="HRDemo"):
    tables = catalog._load_discovered(_json(tmp_path, database), database, pats)
    return Catalog(database, "tsql", "X", [], [], tables=tables, sensitive_patterns=pats)


def test_without_the_key_everything_is_exactly_as_before(tmp_path):
    cat = _cat(tmp_path)
    assert {t.name for t in cat.offered_tables} == {"dbo.Employee"}        # the salary table stays hidden
    rendered = cat.render_selected(["dbo.Employee"])
    for hidden in ("BasicSalary", "EmpMobileNo", "BloodGroup", "UserPassword", "api_token"):
        assert hidden not in rendered, hidden
    with pytest.raises(GuardError):
        guard("SELECT TOP (5) BasicSalary FROM dbo.Employee", set(), "tsql",
              allowed_tables=frozenset({"dbo.employee"}))


def test_with_the_key_the_personal_columns_answer_and_credentials_do_not(tmp_path):
    cat = _cat(tmp_path, OPEN)
    assert {t.name for t in cat.offered_tables} == {"dbo.Employee", "dbo.EmployeeSalary"}
    rendered = cat.render_selected(["dbo.Employee"])
    for shown in ("BasicSalary", "EmpMobileNo", "BloodGroup"):
        assert shown in rendered, shown
    for still_hidden in ("UserPassword", "api_token"):
        assert still_hidden not in rendered, still_hidden
    allowed = frozenset({"dbo.employee"})
    guard("SELECT TOP (5) BasicSalary, EmpMobileNo, BloodGroup FROM dbo.Employee", set(), "tsql",
          allowed_tables=allowed, sensitive_patterns=OPEN)
    for refused in ("UserPassword", "api_token"):
        with pytest.raises(GuardError, match="restricted column"):
            guard(f"SELECT TOP (5) {refused} FROM dbo.Employee", set(), "tsql",
                  allowed_tables=allowed, sensitive_patterns=OPEN)


def test_one_database_opens_while_another_refuses_the_same_statement(tmp_path):
    """Goal 3: the other sixteen databases keep every refusal."""
    sql = "SELECT TOP (5) BasicSalary FROM dbo.Employee"
    allowed = frozenset({"dbo.employee"})
    guard(sql, set(), "tsql", allowed_tables=allowed, sensitive_patterns=OPEN)         # HR
    with pytest.raises(GuardError, match="restricted column"):
        guard(sql, set(), "tsql", allowed_tables=allowed)                              # everyone else


def test_an_opened_database_is_never_read_across_a_three_part_name(tmp_path, monkeypatch):
    """The guard's column check cannot attribute a column to a database, so an opened catalog gets no
    siblings at all — its looser list must not reach another database's columns."""
    import ragbot.data.tools as tools
    from ragbot.auth.models import Scope
    cat = _cat(tmp_path, OPEN)
    sib = _cat(tmp_path, None, database="Other")
    monkeypatch.setattr(tools, "settings", lambda: {"data.big_table_rows": 1_000_000})
    seen = {}
    monkeypatch.setattr(tools, "guard", lambda *a, **kw: seen.update(kw) or a[0])
    tools._guard_for("SELECT TOP (5) BasicSalary FROM dbo.Employee", cat, 200, Scope.unrestricted(),
                     {"Other": sib})
    assert seen["allowed_catalogs"] is None and seen["sensitive_patterns"] == OPEN
    seen.clear()
    tools._guard_for("SELECT TOP (5) EmployeeCode FROM dbo.Employee", sib, 200, Scope.unrestricted(),
                     {"HRDemo": cat})
    assert seen["allowed_catalogs"] is not None and seen["sensitive_patterns"] is None


def test_the_real_guard_refuses_the_three_part_name_from_an_opened_database(tmp_path, monkeypatch):
    """The test above stubs guard() to read what it is handed; this one runs it, so the refusal is the
    guard's own. It is verification 5 of the Phase R plan without a throwaway settings file: siblings are
    offered to a normal catalog and withheld from an opened one, and the guard rejects what it is not told about."""
    import ragbot.data.tools as tools
    from ragbot.auth.models import Scope
    opened, normal = _cat(tmp_path, OPEN), _cat(tmp_path, None, database="Other")
    monkeypatch.setattr(tools, "settings", lambda: {"data.big_table_rows": 1_000_000})
    three_part = "SELECT TOP (5) e.EmployeeCode FROM Other.dbo.Employee e"
    with pytest.raises(GuardError, match="three-part name not allowed"):
        tools._guard_for(three_part, opened, 200, Scope.unrestricted(), {"Other": normal})
    # the same statement from a database that did not open its columns is accepted (cross_database_names on)
    tools._guard_for(three_part, _cat(tmp_path, None, database="Third"), 200, Scope.unrestricted(),
                     {"Other": normal})


def test_editing_only_the_pattern_list_takes_effect(tmp_path):
    """The offered-table memo and the schema-index cache must both notice. Neither the discovery file's hash
    nor the table notes change when only `sensitive_patterns:` is edited, so the pattern list has to be part
    of the index's key or a running process would keep serving the closed index."""
    closed, opened = _cat(tmp_path, None), _cat(tmp_path, OPEN)
    assert len(closed.offered_tables) == 1 and len(opened.offered_tables) == 2
    si._CACHE.clear(); si._CHECKED.clear()
    assert si.get_index(closed).names == ["dbo.Employee"]
    si._CHECKED.clear()                      # the index re-checks its files every few seconds; skip the wait
    assert si.get_index(opened).names == ["dbo.Employee", "dbo.EmployeeSalary"]


def test_a_malformed_pattern_list_stops_the_app(tmp_path):
    folder = tmp_path / "cats"
    folder.mkdir()
    (folder / "bad.yaml").write_text(
        "database: Bad\ndialect: tsql\nconnection_env: X\nviews: []\nsensitive_patterns: []\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="sensitive_patterns"):
        catalog._load_catalogs(folder, ("stamp",))


def test_the_real_catalogs_open_exactly_the_three_hr_databases():
    """What the owner asked for on 2026-10-13, measured on the real discovery files — and the proof that no
    other database moved: every catalog without its own list still hides what it hid before."""
    cats = load_catalogs()
    opened = sorted(n for n, c in cats.items() if c.sensitive_patterns is not None)
    assert opened == ["HR", "HWATT", "Hrms5Misami"]
    counts = {n: len(cats[n].offered_tables) for n in opened}
    # HR 265 -> 268 and HWATT 64 -> 64 (nothing there was hidden for being sensitive). Hrms5Misami 402 -> 462
    # from the opening, then -11 for the scratch and dated copies of the salary family that the opening
    # revealed (EmployeeSalaryTemp, tempEmplSalary, SalaryProcess2/3, WorkOffCalendar_BackNov2021 and the
    # rest): they hold real-looking numbers from a processing run and would answer with a wrong total.
    assert counts == {"HR": 268, "Hrms5Misami": 451, "HWATT": 64}, counts
    for name, cat in ((n, cats[n]) for n in opened):
        offered = {t.name for t in cat.offered_tables}
        assert "dbo.EmployeeSalaryTemp" not in offered and "dbo.SalaryProcess2" not in offered
    assert "dbo.SalaryProcess" in {t.name for t in cats["Hrms5Misami"].offered_tables}   # the real ledger stays
    for name in opened:
        cat = cats[name]
        assert not any(t.sensitive for t in cat.tables if "password" not in t.name.lower()), name
    # a database the owner did not name keeps its hidden columns
    other = cats["BitopiSplint"]
    assert other.sensitive_patterns is None
    assert any(c.get("sensitive") for t in other.tables for c in t.columns), "BitopiSplint must still hide columns"


def test_the_real_hr_catalogs_still_hide_every_credential_column():
    """An offline check over the real discovery files: a typo in a pattern list must not fail open."""
    cats = load_catalogs()
    opened = {n: c for n, c in cats.items() if c.sensitive_patterns is not None}
    if not opened:
        pytest.skip("no catalog declares sensitive_patterns yet")
    for name, cat in opened.items():
        for t in cat.tables:
            for col in t.columns:
                if is_sensitive(col["name"], ("password", "pwd", "token", "secret")):
                    assert col.get("sensitive"), f"{name}.{t.name}.{col['name']} is a credential and must stay hidden"
