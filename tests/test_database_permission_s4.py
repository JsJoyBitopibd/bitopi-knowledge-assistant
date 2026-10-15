"""Phase S4: every other road by which a database restricted with `restricted_to:` could reach a user who may not
read it — the value index, a fixed tool's value parameter, a denied fixed tool, the not-found chips, the agent's
charter, the sidebar's database count and the admin audit — plus the check that catches a half-armed S3 and the
rule armed in memory against the real catalogs. S1–S2 (tests/test_database_permission.py) covered the router,
the guard and the not-found sentence.
"""
import dataclasses

import pytest

import ragbot.data.tools as tools
from ragbot.auth.models import Scope
from ragbot.data.catalog import Catalog, View
from ragbot.data.values import GROUP, ValueEntry
from ragbot.models import QueryResult


def _scope(*groups: str, factories=("*",)) -> Scope:
    return Scope(factories=frozenset(factories), groups=frozenset(groups))


def _cat(name: str, restricted_to=()) -> Catalog:
    view = View(f"rag.vw_{name}", "one row per thing", "", ["Id"],
                {"Id": "id", "Factory": "factory short name"}, scope_column="Factory")
    return Catalog(name, "tsql", "SQLSERVER_CONN_X", [], [view], restricted_to=tuple(restricted_to))


def _result(database="Open") -> QueryResult:
    return QueryResult(database=database, engine="sqlserver", views=[], sql="", columns=[], rows=[])


PEOPLE_VALUE = ValueEntry("department", "Compliance Cell", "", GROUP, "People", "dbo.Department.Name")
OPEN_VALUE = ValueEntry("store", "Maintenance Store", "2", GROUP, "Open", "rag.vw_Open.StoreName")
MGR, HRM = _scope("ka-managers"), _scope("ka-managers", "ka-hr")
CFG = {"groups": {"KA-Managers": {"factories": ["*"]}, "KA-HR": {"factories": ["*"]}}, "admins": ["KA-Admins"]}


@pytest.fixture
def armed(monkeypatch):
    """Two catalogs, one restricted to KA-HR, seen by every module that asks catalog.load_catalogs."""
    cats = {"Open": _cat("Open"), "People": _cat("People", ["KA-HR"])}
    monkeypatch.setattr("ragbot.data.catalog.load_catalogs", lambda *a, **k: cats)
    monkeypatch.setattr(tools, "load_catalogs", lambda *a, **k: cats)
    return cats


def _broken(*a, **k):
    raise RuntimeError("catalog folder unreadable")


# ---------------------------------------------------------------- the value index
def test_a_denied_users_value_lookup_skips_a_restricted_databases_values(armed):
    from ragbot.data.values import lookup
    q = "how many staff are in the compliance cell of the maintenance store"
    mgr = {h.value for h in lookup(q, MGR, entries=[PEOPLE_VALUE, OPEN_VALUE])}
    hrm = {h.value for h in lookup(q, HRM, entries=[PEOPLE_VALUE, OPEN_VALUE])}
    assert "Compliance Cell" not in mgr and "Maintenance Store" in mgr
    assert {"Compliance Cell", "Maintenance Store"} <= hrm
    # a caller resolving with an unrestricted scope on a user's behalf passes the user's denials
    assert "Compliance Cell" not in {h.value for h in lookup(q, Scope.unrestricted(), entries=[PEOPLE_VALUE],
                                                             deny={"People"})}


def test_a_lookup_fails_closed_when_the_catalogs_cannot_be_read(monkeypatch):
    from ragbot.data.values import lookup
    monkeypatch.setattr("ragbot.data.catalog.load_catalogs", _broken)
    assert lookup("staff in compliance cell", MGR, entries=[PEOPLE_VALUE]) == []


def test_known_values_lines_never_name_a_restricted_value(armed):
    from ragbot.data.values import known_values_lines
    q = "staff in the compliance cell"
    mgr = known_values_lines(q, MGR, ["Open", "People"], entries=[PEOPLE_VALUE], with_company=False)
    hrm = known_values_lines(q, HRM, ["Open", "People"], entries=[PEOPLE_VALUE], with_company=False)
    assert not any("Compliance Cell" in line for line in mgr)
    assert any("Compliance Cell" in line for line in hrm)


# ---------------------------------------------------------------- fixed tools
def _tool(name, database, match=r"(?i)present today", **extra):
    return {"name": name, "database": database, "match": match, "sql": "SELECT 1", "example": f"{name}?", **extra}


def test_a_denied_databases_tool_is_skipped_and_the_next_tool_tried():
    tools_ = [_tool("people_present", "People"), _tool("open_present", "Open")]
    assert tools.match_fixed_tool("who is present today", tools_)[0]["name"] == "people_present"
    assert tools.match_fixed_tool("who is present today", tools_,
                                  denied=frozenset({"People"}))[0]["name"] == "open_present"


def test_a_denied_tools_value_parameter_is_never_resolved(monkeypatch):
    """An open tool whose value parameter would resolve to a name recorded in a restricted database: the name is
    not resolved and its label is never shown."""
    monkeypatch.setattr("ragbot.data.values.load_index", lambda *a, **k: [PEOPLE_VALUE])
    monkeypatch.setattr("ragbot.data.catalog.load_catalogs",
                        lambda *a, **k: {"Open": _cat("Open"), "People": _cat("People", ["KA-HR"])})
    t = _tool("dept_headcount", "Open", match=r"(?i)staff in",
              params={"dept": {"type": "value", "entity": "department", "label": "in {value}", "else_label": ""}})
    _, open_params = tools.match_fixed_tool("staff in compliance cell", [t])
    _, denied_params = tools.match_fixed_tool("staff in compliance cell", [t], denied=frozenset({"People"}))
    assert open_params["dept"] == "Compliance Cell" and open_params["dept_label"] == "in Compliance Cell"
    assert denied_params["dept"] == "" and denied_params["dept_label"] == ""


def test_run_fixed_tool_refuses_a_denied_database_before_the_local_copy(monkeypatch):
    """Fixed tools never pass through _run_guarded, so run_fixed_tool checks the rule itself — before its local
    copy or its SQL is touched."""
    def boom(*a, **k):
        raise AssertionError("nothing may be read for a denied database")
    monkeypatch.setattr(tools, "_from_aggregate", boom)
    monkeypatch.setattr(tools, "cached_run", boom)
    r = tools.run_fixed_tool(_tool("people_present", "People", aggregate=True), {},
                             {"People": _cat("People", ["KA-HR"])}, scope=MGR)
    assert r.denied and "database People" in r.error and not r.rows and r.sql == ""


# ---------------------------------------------------------------- the agent, the router, the chips
def test_the_hr_charter_never_reaches_a_denied_users_prompt(armed, monkeypatch):
    """The HR agent's charter names the people tables and what they hold. A user who may read none of its
    databases gets no agent: its charter never reaches their SQL prompt or their answer's agent label."""
    from ragbot.domain_agents.registry import Agent
    hr = Agent(name="hr", title="HR", catalogs=("People",), keywords=("present",))
    monkeypatch.setattr("ragbot.domain_agents.pick_agent", lambda *a, **k: hr)
    monkeypatch.setattr(tools, "match_fixed_tool", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_query_vector", lambda q: None)
    monkeypatch.setattr(tools, "pick_catalogs", lambda q, cats, *a, **k: dict(cats))
    seen = {}

    def fake_generate(question, picked, *a, agent="", **k):
        seen["agent"] = agent
        return _result()

    monkeypatch.setattr(tools, "generate_and_run", fake_generate)
    tools.answer_from_data("how many are present today", scope=MGR)
    assert seen["agent"] == ""
    tools.answer_from_data("how many are present today", scope=HRM)
    assert seen["agent"] == "hr"


def test_the_schema_index_is_never_loaded_for_a_denied_database(monkeypatch):
    """The real router (pick_catalogs -> db_router.evidence) loads the schema index of every catalog it ranks.
    With five catalogs that offer tables — more than are shown, so the ranking runs — the index of the
    restricted one is never loaded for a user outside its group, and is for a member."""
    from ragbot.data import schema_index
    from ragbot.data.catalog import Table

    def cat(name, restricted_to=()):
        c = _cat(name, restricted_to)
        return dataclasses.replace(c, tables=[Table(f"dbo.{name}Thing", rows=10,
                                                     columns=[{"name": "Id", "type": "int"}])])
    cats = {n: cat(n) for n in ("Open1", "Open2", "Open3", "Open4")} | {"People": cat("People", ["KA-HR"])}
    monkeypatch.setattr("ragbot.data.catalog.load_catalogs", lambda *a, **k: cats)
    monkeypatch.setattr(tools, "load_catalogs", lambda *a, **k: cats)
    monkeypatch.setattr(tools, "match_fixed_tool", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_query_vector", lambda q: None)
    monkeypatch.setattr("ragbot.domain_agents.pick_agent", lambda *a, **k: None)
    monkeypatch.setattr(tools, "generate_and_run", lambda *a, **k: _result())
    loaded = []

    class FakeIndex:
        def best_name_share(self, *a):
            return 0.0

        def best_cosine(self, *a):
            return None

    def fake_get_index(c, *a, **k):
        loaded.append(c.database)
        return FakeIndex()

    monkeypatch.setattr(schema_index, "get_index", fake_get_index)
    tools.answer_from_data("list every thing", scope=MGR)
    assert loaded and "People" not in loaded
    loaded.clear()
    tools.answer_from_data("list every thing", scope=HRM)
    assert "People" in loaded


def test_not_found_never_offers_a_denied_databases_example(armed, monkeypatch):
    from ragbot.agent import not_found
    monkeypatch.setattr("ragbot.domain_agents.pick_agent", lambda *a, **k: None)
    tools_ = [{"name": "manpower_present_latest", "database": "People",
               "example": "What is the number of manpower present today?", "description": "present today"},
              {"name": "orders", "database": "Open", "example": "How many orders are present today?"}]
    q = "how many manpower present today"
    assert "What is the number of manpower present today?" not in not_found.suggestions(q, [], tools=tools_, scope=MGR)
    assert "What is the number of manpower present today?" in not_found.suggestions(q, [], tools=tools_, scope=HRM)


def test_not_found_offers_no_tool_example_when_the_catalogs_cannot_be_read(monkeypatch):
    from ragbot.agent import not_found
    monkeypatch.setattr("ragbot.data.catalog.load_catalogs", _broken)
    monkeypatch.setattr("ragbot.domain_agents.pick_agent", lambda *a, **k: None)
    tools_ = [{"name": "orders", "database": "Open", "example": "How many orders ship this month?"}]
    assert not_found.suggestions("how many orders ship this month", [], tools=tools_, scope=MGR) == []


# ---------------------------------------------------------------- the UI and the audit
def test_the_database_count_counts_only_readable_databases(armed):
    from ragbot.data.catalog import readable_databases
    assert readable_databases(MGR) == ["Open"]
    assert readable_databases(HRM) == ["Open", "People"]
    assert readable_databases(Scope.unrestricted()) == ["Open", "People"]


def test_the_audit_download_withholds_rows_naming_a_restricted_database():
    from ragbot.logs import WITHHELD, withhold_restricted
    rows = [{"ts": "1", "user": "u", "question": "salary of 250109595", "refs": "D1:People", "agent": "hr",
             "databases_considered": "People;Open", "route": "data"},
            {"ts": "2", "user": "u", "question": "orders shipping", "refs": "D1:Open", "agent": "order",
             "databases_considered": "Open", "route": "data"}]
    out = withhold_restricted(rows, {"People"})
    assert out[0]["question"] == WITHHELD and out[0]["refs"] == WITHHELD and out[0]["agent"] == WITHHELD
    assert (out[0]["ts"], out[0]["user"], out[0]["route"]) == ("1", "u", "data")
    assert out[1] == rows[1]
    assert withhold_restricted(rows, set()) == rows


def test_the_audit_withholds_the_rows_that_cite_nothing_too():
    """Found by the S4 review: a failed fixed tool cites nothing but names its database in a warning; a not-found
    reply from the HR agent carries only the agent; and the thumbs row carries the first 200 characters of the
    answer under the same request id."""
    from ragbot.logs import WITHHELD, withhold_restricted
    rows = [{"ts": "1", "user": "u", "route": "data", "question": "who is present", "refs": "", "agent": "hr",
             "warnings": "data People: timeout", "databases_considered": "", "request_id": "r1"},
            {"ts": "2", "user": "u", "route": "feedback", "question": "turn 1", "refs": "", "agent": "",
             "warnings": "thumb=1 | Employee 12345 gross salary is 45,000", "databases_considered": "",
             "request_id": "r1"},
            {"ts": "3", "user": "u", "route": "data", "question": "salary of 1", "refs": "", "agent": "hr",
             "warnings": "", "databases_considered": "", "request_id": "r2"},
            {"ts": "4", "user": "u", "route": "data", "question": "orders", "refs": "D1:Open", "agent": "order",
             "warnings": "", "databases_considered": "Open", "request_id": "r3"}]
    out = withhold_restricted(rows, {"People"}, denied_agents={"hr"})
    assert out[0]["question"] == WITHHELD                     # named in a warning
    assert out[1]["warnings"] == WITHHELD                     # the thumbs row of the same request
    assert out[2]["question"] == WITHHELD                     # answered by an agent of denied databases only
    assert out[3] == rows[3]


def test_the_not_found_sentence_fails_closed_when_the_catalogs_cannot_be_read(monkeypatch):
    from ragbot.agent import not_found
    from ragbot.domain_agents.registry import Agent
    agent = Agent(name="hr", title="HR", catalogs=("People",), keywords=("salary",),
                  extra={"where": "People data I can read is the HRMS."})
    monkeypatch.setattr("ragbot.domain_agents.pick_agent", lambda *a, **k: agent)
    monkeypatch.setattr("ragbot.data.catalog.load_catalogs", _broken)
    assert "HRMS" not in not_found.general("what is the salary", MGR)


# ---------------------------------------------------------------- the configuration check
def test_restriction_problems_catch_a_half_armed_config():
    from ragbot.auth.scopes import restriction_problems
    from ragbot.domain_agents.registry import Agent
    open_only = {"Open": _cat("Open")}
    armed_ = {"Open": _cat("Open"), "People": _cat("People", ["KA-HR"])}
    p, w = restriction_problems(open_only, CFG, "none", ["KA-Managers"], {})
    assert p == [] and any("not armed" in x for x in w)                       # said out loud, not a problem
    p, _ = restriction_problems(armed_, CFG, "none", ["KA-Managers"], {})
    assert any("denies everyone" in x for x in p)                              # restricted while sign-in is off
    p, _ = restriction_problems({"People": _cat("People", ["KA-Payroll"])}, CFG, "local", [],
                                {"a": {"groups": ["KA-Payroll"]}})
    assert any("not configured" in x for x in p)                               # a group scopes.yaml lacks
    p, _ = restriction_problems(armed_, CFG, "local", [], {"a": {"groups": ["KA-Managers"]}})
    assert any("no account" in x for x in p)                                   # nobody holds the group
    p, _ = restriction_problems(armed_, CFG, "local", [], {"a": {"groups": ["KA-HR"]}})
    assert p == []                                                             # armed properly
    mixed = {"x": Agent(name="x", title="X", catalogs=("Open", "People"), keywords=())}
    _, w = restriction_problems(armed_, CFG, "local", [], {"a": {"groups": ["KA-HR"]}}, mixed)
    assert any("agent x" in x for x in w)
    # the provider is read case-insensitively, as the app reads it
    p, _ = restriction_problems(armed_, CFG, "None", ["KA-Managers"], {})
    assert any("denies everyone" in x for x in p)


def test_people_tables_in_an_open_catalog_block_arming():
    """Found by the S4 review: the ERP, the dashboard and the production databases offer employee masters and
    attendance of their own. Unarmed they are a note; armed, a problem until excluded or restricted."""
    from ragbot.auth.scopes import restriction_problems
    from ragbot.data.catalog import Table
    staff = dataclasses.replace(_cat("Dash"), tables=[Table("dbo.DailyAttendance_All_Company", rows=5),
                                                      Table("dbo.Orders", rows=5)])
    p, w = restriction_problems({"Dash": staff}, CFG, "none", ["KA-Managers"], {})
    assert p == [] and any("if armed: 1 other catalogs still offer 1 tables" in x and "Dash 1" in x for x in w)
    p, _ = restriction_problems({"Dash": staff, "People": _cat("People", ["KA-HR"])}, CFG, "local", [],
                                {"a": {"groups": ["KA-HR"]}})
    assert any("Dash offers 1 table" in x and "DailyAttendance_All_Company" in x for x in p)
    excluded = dataclasses.replace(staff, exclude_tables=["dbo.DailyAttendance*"])
    p, _ = restriction_problems({"Dash": excluded, "People": _cat("People", ["KA-HR"])}, CFG, "local", [],
                                {"a": {"groups": ["KA-HR"]}})
    assert p == []


# ---------------------------------------------------------------- armed in memory, real catalogs
def test_armed_in_memory_against_the_real_catalogs(monkeypatch):
    """The real 19 catalogs and fixed tools, with `restricted_to: [KA-HR]` on the three people catalogs in memory
    only: a KA-Managers user reads 16 of 19, never reaches the HR tool and is never offered its example; a KA-HR
    member and the unrestricted scope read all 19."""
    from ragbot.agent import not_found
    from ragbot.auth.scopes import scope_for_groups
    from ragbot.data import catalog
    from ragbot.data.catalog import denied_databases, readable_databases

    real = catalog.load_catalogs()
    people = {"Hrms5Misami", "HR", "HWATT"}
    assert people <= set(real) and len(real) == 19
    armed_ = {n: dataclasses.replace(c, restricted_to=("ka-hr",)) if n in people else c for n, c in real.items()}
    monkeypatch.setattr("ragbot.data.catalog.load_catalogs", lambda *a, **k: armed_)
    mgr, hrm = scope_for_groups(["KA-Managers"], CFG), scope_for_groups(["KA-HR"], CFG)
    assert len(readable_databases(mgr)) == 16 and denied_databases(mgr) == people
    assert len(readable_databases(hrm)) == 19 and len(readable_databases(Scope.unrestricted())) == 19
    q = "What is the number of manpower present today?"
    all_tools = tools.load_fixed_tools()
    hit = tools.match_fixed_tool(q, all_tools, denied=denied_databases(mgr))
    assert hit is None or hit[0]["name"] != "manpower_present_latest"
    assert tools.match_fixed_tool(q, all_tools, denied=denied_databases(hrm))[0]["name"] == "manpower_present_latest"
    assert q not in not_found.suggestions("how many manpower are present today", [], tools=all_tools, scope=mgr)
    assert q in not_found.suggestions("how many manpower are present today", [], tools=all_tools, scope=hrm)
