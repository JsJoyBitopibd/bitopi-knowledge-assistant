"""Validate the catalog + fixed tools. Offline by default (including: every fixed tool's `example:`
question reaches that tool); --live also runs each view's definition and every fixed tool, with the
parameters its example produces, against the real database (read-only, rolls back).
Usage: python scripts/check_catalog.py [--live]
"""
import argparse, re, _path  # noqa: F401
from ragbot.auth.models import Scope
from ragbot.config import settings
from ragbot.data.catalog import load_catalogs
from ragbot.data.connectors import run
from ragbot.data.guard import GuardError
from ragbot.data.tools import (companion_tool, load_fixed_tools, match_fixed_tool, run_companion, run_fixed_tool,
                               tool_sql, _views_in)
from ragbot.data.virtual import validate_definition
from ragbot.domain_agents import load_agents, validate_agents


def offline(cats) -> int:
    problems = 0
    for name, cat in cats.items():
        for v in cat.views:
            if not set(v.key_columns) <= set(v.columns):
                print(f"[offline] {name}.{v.name}: key_columns not a subset of columns: "
                      f"{set(v.key_columns) - set(v.columns)}")
                problems += 1
            if not v.scope_column:
                print(f"[offline] {name}.{v.name}: no scope_column — not available to users limited to some factories")
            if v.definition:
                try:
                    validate_definition(v.definition, cat.dialect)
                except GuardError as e:
                    print(f"[offline] {name}.{v.name}: invalid definition: {e}")
                    problems += 1
    tools = load_fixed_tools()
    for tool in tools:
        # a pattern that fails to compile breaks match_fixed_tool() for every question, not just
        # this tool's — re.search() is called unconditionally down the tool list.
        unless = tool.get("unless")
        for key, pats in (("match", [tool.get("match")]), ("requires", tool.get("requires", [])),
                          ("unless", [unless] if isinstance(unless, str) else (unless or []))):
            for pat in pats:
                if not pat:
                    continue
                try:
                    re.compile(pat)
                except re.error as e:
                    print(f"[offline] fixed tool {tool['name']}.{key}: regex does not compile: {e}")
                    problems += 1
        if tool["database"] not in cats:
            print(f"[offline] fixed tool {tool['name']}: database {tool['database']!r} not loaded")
            problems += 1
            continue
        cat = cats[tool["database"]]
        views = _views_in(tool_sql(tool), cat)          # group_by / measures placeholders filled with defaults
        if not views:
            print(f"[offline] fixed tool {tool['name']}: no catalog view referenced")
            problems += 1
        comp = companion_tool(tool)
        if comp is not None and (comp.get("database") not in cats or not comp.get("column") or not comp.get("sql")
                                 or not _views_in(comp["sql"].replace("@" + comp.get("param", "values"), "''"),
                                                  cats.get(comp.get("database"), cat))):
            print(f"[offline] fixed tool {tool['name']}: companion needs a loaded database, a column and SQL over its views")
            problems += 1
        # the example question must reach this tool: tools are tried in file order and the first match
        # wins, so a broader tool placed earlier can shadow this one
        if not tool.get("example"):
            print(f"[offline] fixed tool {tool['name']}: no example question")
            problems += 1
        else:
            hit = match_fixed_tool(tool["example"], tools)
            if hit is None or hit[0]["name"] != tool["name"]:
                print(f"[offline] fixed tool {tool['name']}: its example reaches "
                      f"{hit[0]['name'] if hit else 'no fixed tool'}")
                problems += 1
    # Phase K: every agent's catalogs and charter exist, every tool names a known agent that owns its database
    agents = load_agents()
    from ragbot.domain_agents.watch import validate as validate_watch
    from ragbot.domain_agents.risk import load_rule_sets, validate as validate_risk
    rule_sets = load_rule_sets()
    for p in validate_agents(agents, cats, tools) + validate_watch(agents, cats) + validate_risk(rule_sets):
        print(f"[offline] {p}")
        problems += 1
    # Phase S4: the per-database rule as configured — a half-armed S3 is a problem, "not armed" is said out loud
    from ragbot.auth.providers import load_users
    from ragbot.auth.scopes import load_scopes, restriction_problems
    s = settings()
    bad, notes = restriction_problems(cats, load_scopes(), str(s.get("auth.provider") or "local"),
                                      s.get("auth.open_groups") or [], load_users(), agents)
    for p in bad:
        print(f"[offline] {p}")
        problems += 1
    for w in notes:
        print(f"[offline] note: {w}")
    # Phase N1a: the playbook format and the cost sources. A draft or a cost without a value is not a problem
    # (playbook.ready() reports those); a file that does not load is one, and is counted instead of crashing.
    playbooks, costs, pb_problems = load_stage3(rule_sets)
    for p in pb_problems:
        print(f"[offline] {p}")
        problems += 1
    print(f"[offline] {len(cats)} catalogs, {sum(len(c.views) for c in cats.values())} views, "
          f"{len(tools)} fixed tools, {len(agents)} agents, "
          f"{sum(len(r.rules) for r in rule_sets.values())} risk rules, "
          f"{len(playbooks)} playbooks, {sum(1 for pb in playbooks if pb.status == 'approved')} approved; "
          f"cost sources {sum(1 for s in costs.values() if isinstance(s, dict) and s.get('value') is not None)} "
          f"of {len(costs)} with values — {problems} problem(s)")
    return problems


def load_stage3(rule_sets) -> tuple[list, dict, list[str]]:
    """(playbooks, cost sources, problems) for the offline check: see playbook.load_and_validate."""
    from ragbot.domain_agents.playbook import load_and_validate
    return load_and_validate(rule_sets)


def live(cats) -> int:
    problems = 0
    for name, cat in cats.items():
        for v in cat.views:
            # `WHERE 1 = 0`: the server compiles the view and returns its columns without reading a row. TOP (1) alone
            # made an aggregating view total every row first (VistaQ: 88 million inspections) on a live server.
            sql = f"SELECT TOP (1) * FROM (\n{v.definition or f'SELECT * FROM {v.name}'}\n) AS q WHERE 1 = 0" \
                if cat.dialect == "tsql" else \
                f"SELECT * FROM (\n{v.definition or f'SELECT * FROM {v.name}'}\n) AS q WHERE 1 = 0 LIMIT 1"
            try:
                cols, rows = run(cat.engine, sql, {}, conn_env=cat.connection_env, tool="check_catalog",
                                 database=cat.connection_database)
                missing = set(v.columns) - set(cols)
                extra = set(cols) - set(v.columns)
                if missing:
                    print(f"[live] {name}.{v.name}: catalog documents columns not returned: {missing}")
                    problems += 1
                if extra:
                    print(f"[live] {name}.{v.name}: query returns undocumented columns: {extra}")
            except Exception as e:
                print(f"[live] {name}.{v.name}: FAILED — {e.__class__.__name__}: {e}")
                problems += 1
    tools = load_fixed_tools()
    for tool in tools:
        hit = match_fixed_tool(tool.get("example", ""), tools) if tool.get("example") else None
        params = hit[1] if hit and hit[0]["name"] == tool["name"] else tool.get("example_params")
        if params is None or tool["database"] not in cats:      # {} is fine: a tool without parameters
            continue
        try:
            qr = run_fixed_tool(tool, params, cats, user="check_catalog", scope=Scope.unrestricted())
            if qr.error:
                print(f"[live] fixed tool {tool['name']}: FAILED — {qr.error}")
                problems += 1
            else:
                print(f"[live] fixed tool {tool['name']}: OK ({len(qr.rows)} row(s))")
                extra = run_companion(tool, qr, cats, user="check_catalog", scope=Scope.unrestricted())
                if extra is not None:
                    print(f"[live]   companion: {'FAILED — ' + extra.error if extra.error else f'OK ({len(extra.rows)} row(s))'}")
                    problems += 1 if extra.error else 0
        except Exception as e:
            print(f"[live] fixed tool {tool['name']}: FAILED — {e.__class__.__name__}: {e}")
            problems += 1
    print(f"[live] {problems} problem(s)")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    cats = load_catalogs()
    if not cats:
        print("no catalogs loaded under config/catalog/*.yaml"); raise SystemExit(1)
    n = offline(cats)
    if a.live:
        n += live(cats)
    raise SystemExit(1 if n else 0)


if __name__ == "__main__":
    main()
