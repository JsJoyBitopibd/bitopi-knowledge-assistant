"""Validate the catalog + fixed tools. Offline by default (including: every fixed tool's `example:`
question reaches that tool); --live also runs each view's definition and every fixed tool, with the
parameters its example produces, against the real database (read-only, rolls back).
Usage: python scripts/check_catalog.py [--live]
"""
import argparse, re, _path  # noqa: F401
from ragbot.auth.models import Scope
from ragbot.data.catalog import load_catalogs
from ragbot.data.connectors import run
from ragbot.data.guard import GuardError
from ragbot.data.tools import load_fixed_tools, match_fixed_tool, run_fixed_tool, _views_in
from ragbot.data.virtual import validate_definition


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
        for key, pats in (("match", [tool.get("match")]), ("requires", tool.get("requires", []))):
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
        views = _views_in(tool["sql"], cat)
        if not views:
            print(f"[offline] fixed tool {tool['name']}: no catalog view referenced")
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
    print(f"[offline] {len(cats)} catalogs, {sum(len(c.views) for c in cats.values())} views, "
          f"{len(tools)} fixed tools — {problems} problem(s)")
    return problems


def live(cats) -> int:
    problems = 0
    for name, cat in cats.items():
        for v in cat.views:
            sql = f"SELECT TOP (1) * FROM (\n{v.definition or f'SELECT * FROM {v.name}'}\n) AS q" \
                if cat.dialect == "tsql" else \
                f"SELECT * FROM (\n{v.definition or f'SELECT * FROM {v.name}'}\n) AS q LIMIT 1"
            try:
                cols, rows = run(cat.engine, sql, {}, conn_env=cat.connection_env, tool="check_catalog")
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
