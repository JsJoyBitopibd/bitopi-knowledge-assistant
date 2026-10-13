"""Phase S: a database may name the groups allowed to read it (`restricted_to:`), and a user who holds none
of them cannot reach it by any road — the router, a fixed tool, a pasted statement or the day's SQL cache.

The rule is the user's groups and not a set of database names: a group grants and never takes away (the rule
config/scopes.yaml states in its own header), and a `databases` cap with intersection could only deny the HR
databases to KA-Managers by listing every other database on that group and keeping that list in step forever.
"""
import pytest

import ragbot.data.tools as tools
from ragbot.auth.models import Scope
from ragbot.data.catalog import Catalog, View
from ragbot.data.guard import GuardError


def _scope(*groups: str, factories=("*",)) -> Scope:
    return Scope(factories=frozenset(factories), groups=frozenset(groups))


def _cat(name: str, restricted_to=()) -> Catalog:
    view = View(f"rag.vw_{name}", "one row per thing", "", ["Id"],
                {"Id": "id", "Factory": "factory short name"}, scope_column="Factory")
    return Catalog(name, "tsql", "SQLSERVER_CONN_X", [], [view], restricted_to=tuple(restricted_to))


# ---------------------------------------------------------------- the rule itself
def test_a_database_that_names_no_group_is_open_to_everyone():
    """Every catalog behaved this way before Phase S, and all nineteen still do: nothing names a group yet."""
    assert _scope().allows_database(())
    assert _scope("ka-managers").allows_database([])
    assert Scope.unrestricted().allows_database(())


def test_only_a_member_of_a_named_group_may_read_a_restricted_database():
    hr = ["KA-HR"]
    assert _scope("ka-hr").allows_database(hr)
    assert _scope("KA-HR").allows_database(hr)                 # the scope lower-cases what it is given
    assert _scope("ka-hr", "ka-managers").allows_database(hr)  # another group alongside it still reads
    assert not _scope("ka-managers").allows_database(hr)
    assert not _scope().allows_database(hr)


def test_the_union_of_groups_cannot_widen_a_restriction():
    """The failure the roadmap named: a user in KA-HR and KA-Managers must not reach a KA-Finance database
    just because they hold two groups. Only the group the database names opens it."""
    both = _scope("ka-hr", "ka-managers")
    assert both.allows_database(["KA-HR"])
    assert not both.allows_database(["KA-Finance"])
    assert not both.allows_database(["KA-Finance", "KA-Board"])


def test_being_an_admin_is_not_a_master_key():
    """The admin flag shows the timings and the audit panel. If it also opened every database, the change that
    turned on the rule would hand every administrator the payroll."""
    admin = Scope(factories=frozenset({"*"}), is_admin=True, groups=frozenset({"ka-admins"}))
    assert not admin.allows_database(["KA-HR"])


def test_unrestricted_reads_everything():
    """Scripts, the watcher, the aggregates refresh and the eval harness run unrestricted."""
    assert Scope.unrestricted().allows_database(["KA-HR", "anything"])


# ---------------------------------------------------------------- the cache key
def test_the_groups_are_part_of_the_cache_key():
    """An answer cached for someone who may read a restricted database must never be served to someone who
    may not: every cache in the project is keyed on Scope.key()."""
    assert _scope("ka-hr").key() != _scope("ka-managers").key()
    assert "g=ka-hr" in _scope("ka-hr").key()
    assert _scope("ka-hr", "ka-managers").key() == _scope("ka-managers", "ka-hr").key()   # order-independent


# ---------------------------------------------------------------- loading
def test_restricted_to_is_read_from_the_catalog_and_a_typo_fails_loudly(tmp_path):
    """A key that decides access must not be silently ignored when it is mistyped — that would leave the
    database open while the file says it is closed."""
    from ragbot.data.catalog import load_catalogs
    base = ("database: Demo\ndialect: tsql\nconnection_env: SQLSERVER_CONN_X\n"
            "views:\n  - name: rag.vw_A\n    description: a\n    columns: {Id: id}\n")

    def written(tag: str, line: str):
        # a folder of its own per case: load_catalogs is cached on the folder's file mtimes, and three
        # rewrites of one file inside the same clock tick would be served the first parse
        folder = tmp_path / tag
        folder.mkdir()
        (folder / "demo.yaml").write_text(base + line, encoding="utf-8")
        return folder

    assert load_catalogs(written("ok", "restricted_to: [KA-HR]\n"))["Demo"].restricted_to == ("ka-hr",)
    assert load_catalogs(written("absent", ""))["Demo"].restricted_to == ()
    for tag, line in (("empty", "restricted_to: []\n"), ("scalar", "restricted_to: KA-HR\n"),
                      ("blank", "restricted_to: ['  ']\n")):
        with pytest.raises(RuntimeError, match="restricted_to"):
            load_catalogs(written(tag, line))


# ---------------------------------------------------------------- enforcement
def test_a_denied_database_is_never_put_in_front_of_the_model(monkeypatch):
    """The filter runs before the router, so a denied database is not ranked, its tables are never rendered
    into a prompt and the model is not told it exists."""
    cats = {"Open": _cat("Open"), "People": _cat("People", ["KA-HR"])}
    monkeypatch.setattr(tools, "load_catalogs", lambda *a, **k: cats)
    seen = {}

    def fake_generate(question, picked, *a, **k):
        seen["picked"] = sorted(picked)
        from ragbot.models import QueryResult
        return QueryResult(database="Open", engine="sqlserver", views=[], sql="", columns=[], rows=[])

    monkeypatch.setattr(tools, "generate_and_run", fake_generate)
    monkeypatch.setattr(tools, "_query_vector", lambda q: None)
    monkeypatch.setattr(tools, "match_fixed_tool", lambda *a, **k: None)

    tools.answer_from_data("how much does anyone earn", scope=_scope("ka-managers"))
    assert seen["picked"] == ["Open"]                      # People was dropped before the router

    tools.answer_from_data("how much does anyone earn", scope=_scope("ka-hr"))
    assert seen["picked"] == ["Open", "People"]


def test_the_guard_refuses_a_denied_database_even_when_the_statement_arrives_another_way():
    """Second check: a statement can reach _run_guarded from the day's question->SQL cache, a fixed tool, a
    companion or a repair, so it is refused there too rather than trusting the caller to have filtered."""
    people = _cat("People", ["KA-HR"])
    with pytest.raises(GuardError, match="outside the user's scope: database People"):
        tools._run_guarded("SELECT TOP (10) * FROM rag.vw_People", people,
                           scope=_scope("ka-managers"), user="u", refresh=False)


def test_a_not_found_reply_does_not_describe_a_database_the_user_may_not_read(monkeypatch):
    """The HR agent's sentence opens "People data I can read is the HRMS …". For a denied user that is both a
    description of data they have no access to and untrue."""
    from ragbot.agent import not_found
    from ragbot.domain_agents.registry import Agent

    agent = Agent(name="hr", title="HR", catalogs=("People",), keywords=("salary",),
                  extra={"where": "People data I can read is the HRMS."})
    monkeypatch.setattr("ragbot.domain_agents.pick_agent", lambda *a, **k: agent)
    monkeypatch.setattr("ragbot.data.catalog.load_catalogs",
                        lambda *a, **k: {"People": _cat("People", ["KA-HR"])})

    assert "HRMS" in not_found.general("what is the salary", _scope("ka-hr"))
    assert "HRMS" not in not_found.general("what is the salary", _scope("ka-managers"))
    assert "HRMS" in not_found.general("what is the salary")          # no scope given: unchanged behaviour
