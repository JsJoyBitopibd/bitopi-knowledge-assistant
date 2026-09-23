"""Data tools: fixed (hand-written SQL) first, then model-generated SQL through the guard.

Both return a QueryResult carrying everything a [D#] reference needs (db, views, row keys, SQL, as-of).
`sql` is what the user is shown (references rag.* views); `sql_executed` is what actually ran, with
virtual views expanded (see virtual.py) when the DBA has not yet created the real rag.* views.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlglot import exp, parse_one

from ..config import prompt, settings
from ..llm import get_chat
from ..models import QueryResult
from .catalog import Catalog, load_catalogs
from .connectors import run
from .guard import GuardError, guard
from .virtual import rewrite_virtual


# ---------------------------------------------------------------- fixed tools
def load_fixed_tools(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or settings().path("fixed_tools")
    if not path.exists():
        return []
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def _date_window(text: str) -> tuple[date, date]:
    t = text.lower()
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    if "this week" in t:
        return monday, monday + timedelta(days=7)
    if "next week" in t:
        return monday + timedelta(days=7), monday + timedelta(days=14)
    if "this month" in t:
        start = today.replace(day=1)
        nxt = (start.replace(month=start.month + 1) if start.month < 12 else start.replace(year=start.year + 1, month=1))
        return start, nxt
    if "next month" in t:
        this_start = today.replace(day=1)
        start = (this_start.replace(month=this_start.month + 1) if this_start.month < 12
                 else this_start.replace(year=this_start.year + 1, month=1))
        nxt = (start.replace(month=start.month + 1) if start.month < 12 else start.replace(year=start.year + 1, month=1))
        return start, nxt
    m = re.search(r"last (\d+) days", t)
    if m:
        n = int(m.group(1))
        return today - timedelta(days=n), today + timedelta(days=1)
    m = re.search(r"next (\d+) days", t)
    if m:
        return today, today + timedelta(days=int(m.group(1)))
    return today, today + timedelta(days=7)


def match_fixed_tool(question: str, tools: list[dict[str, Any]]) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    for tool in tools:
        m = re.search(tool["match"], question)
        if not m:
            continue
        if any(not re.search(r, question) for r in tool.get("requires", [])):
            continue
        params: dict[str, Any] = {}
        for name, spec in tool.get("params", {}).items():
            groups = spec.get("from_group", [])
            groups = groups if isinstance(groups, list) else [groups]
            raw = next((m.group(g) for g in groups if g <= (m.lastindex or 0) and m.group(g)), None)
            if raw is None:
                break
            if spec.get("type") == "date_window":
                params["from"], params["to"] = _date_window(raw)
                continue
            if spec.get("type") == "int":
                raw = int(raw)
            if spec.get("upper"):
                raw = str(raw).upper()
            params[name] = raw
        else:
            return tool, params
    return None


def run_fixed_tool(tool: dict[str, Any], params: dict[str, Any], cats: dict[str, Catalog], user: str = "") -> QueryResult:
    cat = cats[tool["database"]]
    sql = tool["sql"].strip()
    try:
        sql_exec, views = rewrite_virtual(sql, cat)
        cols, rows = run(cat.engine, sql_exec, params, conn_env=cat.connection_env, display_sql=sql,
                         tool=tool["name"], user=user)
        return QueryResult(database=cat.database, engine=cat.engine, views=views, sql=sql, sql_executed=sql_exec,
                           params=params, tool=tool["name"], columns=cols, rows=rows, key_columns=_keys_for(views, cat))
    except Exception as e:
        return QueryResult(database=cat.database, engine=cat.engine, views=_views_in(sql, cat), sql=sql,
                           params=params, tool=tool["name"], columns=[], rows=[], error=str(e)[:300])


# ---------------------------------------------------------------- generated SQL
def _views_in(sql: str, cat: Catalog) -> list[str]:
    try:
        tree = parse_one(sql, read=cat.dialect)
        names = {".".join(p for p in [t.db, t.name] if p) for t in tree.find_all(exp.Table)}
    except Exception:
        names = set(re.findall(r"\brag\.\w+", sql, re.IGNORECASE))
    return sorted(n for n in names if cat.view(n))


def _keys_for(views: list[str], cat: Catalog) -> list[str]:
    keys: list[str] = []
    for v in views:
        vw = cat.view(v)
        if vw:
            keys += [k for k in vw.key_columns if k not in keys]
    return keys


def _extract_params(sql: str, cat: Catalog) -> dict[str, Any]:
    """Resolve well-known date parameters; anything else must be a literal the model put in SQL."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    known = {"today": today, "tomorrow": today + timedelta(1), "thisMon": monday, "nextMon": monday + timedelta(7),
             "nextMon2": monday + timedelta(14), "monthStart": today.replace(day=1)}
    style = "@" if cat.dialect == "tsql" else ":"
    names = set(re.findall(re.escape(style) + r"(\w+)", sql))
    missing = [n for n in names if n not in known]
    if missing:
        raise GuardError(f"unresolved parameters: {missing}")
    return {n: known[n] for n in names}


def _score(question: str, cat: Catalog) -> int:
    q = question.lower()
    kw = sum(3 for w in cat.keywords if w.lower() in q)
    cols = sum(1 for v in cat.views for w in [v.name.split(".")[-1].lower(), *[c.lower() for c in v.columns]]
              if w and w in q)
    return kw + cols


def _pick_catalog(question: str, cats: dict[str, Catalog]) -> Catalog:
    return max(cats.values(), key=lambda c: _score(question, c))


def _split_database_line(raw: str, cats: dict[str, Catalog], fallback_db: str) -> tuple[str, str]:
    """First line 'DATABASE: <name>' picks the catalog; anything else falls back to keyword scoring."""
    lines = raw.splitlines()
    if lines and lines[0].strip().upper().startswith("DATABASE:"):
        name = lines[0].split(":", 1)[1].strip()
        match = next((k for k in cats if k.lower() == name.lower()), None)
        sql = "\n".join(lines[1:]).strip()
        return (match or fallback_db), sql
    return fallback_db, raw


def generate_and_run(question: str, cats: dict[str, Catalog], user: str = "") -> QueryResult:
    """One SQL-generation call sees every loaded catalog and must name the database it chose
    (`DATABASE: <name>`) before the SQL, so the guard's view allow-list matches the right catalog."""
    s = settings()
    chat = get_chat()
    catalogs_text = "\n\n".join(
        f"=== Database: {name} ({c.dialect}) — {c.description or 'no description'} ===\n{c.render()}"
        for name, c in cats.items())
    system = prompt("sql_generate").replace("{catalogs}", catalogs_text).replace("{today}", date.today().isoformat())
    messages = [{"role": "user", "content": question}]
    fallback = _pick_catalog(question, cats)
    last_err = ""
    for attempt in range(1 + int(s["data.sql_retries"])):
        reply = chat.chat(messages, system=system, max_tokens=int(s.get("llm.max_tokens_sql", 1536)),
                          temperature=0, purpose="sql", user=user)
        raw = re.sub(r"^```\w*|```$", "", reply.text.strip(), flags=re.M).strip()
        db_name, sql = _split_database_line(raw, cats, fallback.database)
        cat = cats[db_name]
        try:
            safe = guard(sql, cat.view_names, cat.dialect, int(s["data.max_rows"]))
            sql_exec, views = rewrite_virtual(safe, cat)
            params = _extract_params(safe, cat)
            cols, rows = run(cat.engine, sql_exec, params, conn_env=cat.connection_env, display_sql=safe, user=user)
            return QueryResult(database=cat.database, engine=cat.engine, views=views, sql=safe, sql_executed=sql_exec,
                               params=params, columns=cols, rows=rows, key_columns=_keys_for(views, cat))
        except Exception as e:
            last_err = f"{e.__class__.__name__}: {e}"
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"That query failed: {last_err}\n"
                                                      "Return DATABASE: <name> on the first line, then the corrected SQL only."}]
    return QueryResult(database=fallback.database, engine=fallback.engine, views=[], sql="", columns=[], rows=[],
                       error=last_err)


def answer_from_data(question: str, user: str = "") -> list[QueryResult]:
    """Try fixed tools across all catalogs; else generate SQL, letting the model pick the database."""
    cats = load_catalogs()
    if not cats:
        return []
    hit = match_fixed_tool(question, load_fixed_tools())
    if hit:
        tool, params = hit
        if tool["database"] in cats:
            return [run_fixed_tool(tool, params, cats, user)]
    return [generate_and_run(question, cats, user)]
