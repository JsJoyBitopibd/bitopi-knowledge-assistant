"""Data tools: fixed (hand-written SQL) first, then model-generated SQL through the guard.

Both return a QueryResult carrying everything a [D#] reference needs (db, views, row keys, SQL, as-of).
`sql` is what the user is shown (references rag.* views); `sql_executed` is what actually ran, with
virtual views expanded (see virtual.py) when the DBA has not yet created the real rag.* views.
"""
from __future__ import annotations

import re
import time
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml
from sqlglot import exp, parse_one

from .. import trace
from ..auth.models import Scope
from ..config import prompt, settings
from ..llm import get_chat
from ..models import QueryResult
from .cache import cached_run
from .catalog import Catalog, load_catalogs
from .guard import GuardError, guard
from .virtual import rewrite_virtual


# ---------------------------------------------------------------- fixed tools
def load_fixed_tools(path: Path | None = None) -> list[dict[str, Any]]:
    """Cached on the file's mtime — this is read on every data question (and now on every route)."""
    path = path or settings().path("fixed_tools")
    if not path.exists():
        return []
    return list(_load_fixed_tools(path, path.stat().st_mtime_ns))


@lru_cache(maxsize=2)
def _load_fixed_tools(path: Path, _mtime: int) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or []


def needs_clarification(question: str) -> Optional[str]:
    """The clarifying question from config/clarify.yaml whose `match` fires (and `unless` does not), or None."""
    path = settings().path("fixed_tools").parent / "clarify.yaml"
    if not path.exists():
        return None
    for rule in _load_fixed_tools(path, path.stat().st_mtime_ns):
        if re.search(rule["match"], question) and not (rule.get("unless") and re.search(rule["unless"], question)):
            return rule["ask"]
    return None


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
                params[name] = raw.lower()   # the phrase ("next week"), for templated answers; not in the SQL
                continue
            if spec.get("type") == "int":
                raw = int(raw)
            if spec.get("upper"):
                raw = str(raw).upper()
            params[name] = raw
        else:
            return tool, params
    return None


def _from_aggregate(tool: dict[str, Any], params: dict[str, Any], cat: Catalog, user: str,
                    scope: Scope) -> Optional[QueryResult]:
    """Answer a fixed tool from its local pre-computed copy (data/aggregates.py), or None to go live
    (no copy yet, no way to apply the user's scope to it, or the local query failed). The result's
    as_of is the copy's refresh time."""
    from . import aggregates
    from .connectors import _log
    name = tool["aggregate"]
    as_of = aggregates.available(name)
    if as_of is None or not tool.get("local_sql"):
        return None
    sql = tool["local_sql"].strip()
    if not scope.all_factories:
        # The copy holds every factory's rows. Read it only through the scope filter; when the copy has
        # no factory column yet (refreshed before F1), go live, where the view filters the rows.
        spec = next((a for a in aggregates.load_specs() if a.get("name") == name), {})
        col = spec.get("scope_column")
        if not col or not aggregates.has_column(name, col):
            return None
        sql = aggregates.scoped_local_sql(sql, name, col, scope.db_factories())
        if sql is None:
            return None
    t0 = time.perf_counter()
    try:
        cols, rows = aggregates.run_local(sql, params, int(settings()["data.max_rows"]))
    except Exception as e:
        _log("local", tool["name"], sql, sql, params, 0, (time.perf_counter() - t0) * 1000, error=str(e)[:300], user=user,
             scope=scope.key())
        return None
    _log("local", tool["name"], sql, sql, params, len(rows), (time.perf_counter() - t0) * 1000, user=user,
         scope=scope.key())
    views = _views_in(tool["sql"], cat)
    return QueryResult(database=cat.database, engine="local", views=views, sql=sql, sql_executed=sql,
                       params=params, tool=tool["name"], columns=cols, rows=rows,
                       key_columns=_keys_for(views, cat), as_of=as_of)


def run_fixed_tool(tool: dict[str, Any], params: dict[str, Any], cats: dict[str, Catalog], user: str = "",
                   refresh: bool = False, *, scope: Scope) -> QueryResult:
    cat = cats[tool["database"]]
    sql = tool["sql"].strip()
    factory = params.get("factory")
    if factory and not scope.allows_factory(str(factory)):
        # Say so instead of running it: the view filter would return nothing, and the templated answer
        # would then state a false "RHL has 0 order(s)".
        return QueryResult(database=cat.database, engine=cat.engine, views=_views_in(sql, cat), sql=sql,
                           params=params, tool=tool["name"], columns=[], rows=[], denied=True,
                           error=f"outside the user's scope: factory {factory}")
    local = _from_aggregate(tool, params, cat, user, scope) if tool.get("aggregate") and not refresh else None
    if local is not None:
        return local
    try:
        sql_exec, views = rewrite_virtual(sql, cat, scope=scope)
        # `heavy: true` in fixed_tools.yaml: a known-slow query (e.g. over the 4M-row, unindexed
        # dbo.ExportOrderBack) gets the longer timeout tier instead of failing at the default.
        timeout = int(settings().get("data.timeout_seconds_heavy", 30)) if tool.get("heavy") else None
        cols, rows, as_of = cached_run(cat.engine, sql_exec, params, conn_env=cat.connection_env, display_sql=sql,
                                       tool=tool["name"], user=user, refresh=refresh, timeout=timeout,
                                       scope_key=scope.key())
        return QueryResult(database=cat.database, engine=cat.engine, views=views, sql=sql, sql_executed=sql_exec,
                           params=params, tool=tool["name"], columns=cols, rows=rows, key_columns=_keys_for(views, cat),
                           as_of=as_of)
    except Exception as e:
        return QueryResult(database=cat.database, engine=cat.engine, views=_views_in(sql, cat), sql=sql,
                           params=params, tool=tool["name"], columns=[], rows=[], error=str(e)[:300])


# ---------------------------------------------------------------- generated SQL
def _views_in(sql: str, cat: Catalog) -> list[str]:
    """Curated views and discovered tables the statement reads (for the [D#] reference)."""
    try:
        tree = parse_one(sql, read=cat.dialect)
        names = {".".join(p for p in [t.db, t.name] if p) for t in tree.find_all(exp.Table)}
    except Exception:
        names = set(re.findall(r"\b\w+\.\w+", sql, re.IGNORECASE))
    return sorted(n for n in names if cat.view(n) or cat.table(n))


def _keys_for(views: list[str], cat: Catalog) -> list[str]:
    """Row-key columns for the [D#] reference: a curated view's key_columns, a raw table's primary key."""
    keys: list[str] = []
    for v in views:
        vw = cat.view(v)
        cols = vw.key_columns if vw else (cat.table(v).primary_key if cat.table(v) else [])
        keys += [k for k in cols if k not in keys]
    return keys


def _schema_selection(question: str, cats: dict[str, Catalog]) -> dict[str, list[str]]:
    """Phase C5: the discovered tables to show for this question, per database ({} when schema RAG is
    off or no catalog has a discovered tier). Uses the same query vector as document retrieval."""
    s = settings()
    if not s.get("data.schema_rag", True) or not any(c.offered_tables for c in cats.values()):
        return {}
    from .schema_index import select_tables
    k = int(s.get("data.schema_rag_k", 6))
    qvec = None
    try:
        from ..retrieve.retriever import _embed_query
        qvec = list(_embed_query(question))
    except Exception:
        pass   # keyword-only selection still works without the embedder
    return {name: select_tables(question, c, k, qvec) for name, c in cats.items() if c.offered_tables}


def _guard_for(sql: str, cat: Catalog, max_rows: int, scope: Scope) -> str:
    """Guard with the catalog's FULL offered set as the allow-list (security), independent of which
    tables the prompt happened to show (relevance). Raw tables have no common factory column to filter
    on, so a user limited to some factories may read the rag views only."""
    offered = cat.offered_tables if scope.all_factories else []
    big_rows = int(settings().get("data.big_table_rows", 1_000_000))
    return guard(sql, cat.view_names, cat.dialect, max_rows,
                 allowed_tables=frozenset(t.name.lower() for t in offered),
                 big_tables={t.name.lower(): t.rows for t in offered if t.rows and t.rows > big_rows})


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


def generate_and_run(question: str, cats: dict[str, Catalog], user: str = "", refresh: bool = False, *,
                     scope: Scope) -> QueryResult:
    """One SQL-generation call sees every loaded catalog and must name the database it chose
    (`DATABASE: <name>`) before the SQL, so the guard's view allow-list matches the right catalog."""
    s = settings()
    chat = get_chat()
    catalogs_text = "\n\n".join(
        f"=== Database: {name} ({c.dialect}) — {c.description or 'no description'} ===\n{c.render()}"
        for name, c in cats.items())
    from .schema_index import join_hints
    # raw tables are not offered to a user limited to some factories (see _guard_for)
    selected = _schema_selection(question, cats) if scope.all_factories else {}
    from .schema_usage import log_selection
    for name, sel in selected.items():
        log_selection(name, sel, question, user)       # which tables to sample (G2)
    tables_text = "\n\n".join(t for t in (cats[n].render_selected(sel, join_hints(sel, cats[n]))
                                          for n, sel in selected.items()) if t)
    # Static text first (rules, curated views, examples), the per-question tables last: a stable
    # prefix lets the provider reuse its cached prompt across questions.
    system = (prompt("sql_generate").replace("{catalogs}", catalogs_text)
              .replace("{tables}", tables_text or "(none selected for this question)")
              .replace("{today}", date.today().isoformat()))
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
            safe = _guard_for(sql, cat, int(s["data.max_rows"]), scope)
            sql_exec, views = rewrite_virtual(safe, cat, scope=scope)
            views += [n for n in _views_in(safe, cat) if not cat.view(n)]   # raw tables, for the [D#] reference
            params = _extract_params(safe, cat)
            cols, rows, as_of = cached_run(cat.engine, sql_exec, params, conn_env=cat.connection_env,
                                           display_sql=safe, user=user, refresh=refresh, scope_key=scope.key())
            return QueryResult(database=cat.database, engine=cat.engine, views=views, sql=safe, sql_executed=sql_exec,
                               params=params, columns=cols, rows=rows, key_columns=_keys_for(views, cat), as_of=as_of)
        except Exception as e:
            last_err = f"{e.__class__.__name__}: {e}"
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"That query failed: {last_err}\n"
                                                      "Return DATABASE: <name> on the first line, then the corrected SQL only."}]
    return QueryResult(database=fallback.database, engine=fallback.engine, views=[], sql="", columns=[], rows=[],
                       error=last_err)


def answer_from_data(question: str, user: str = "", refresh: bool = False, *, scope: Scope) -> list[QueryResult]:
    """Try fixed tools across all catalogs; else generate SQL, letting the model pick the database.
    refresh=True skips the SQL result cache (data/cache.py) and reads live. Every statement is limited
    to the user's scope (data/virtual.py)."""
    with trace.span("data"):
        cats = load_catalogs()
        if not cats:
            return []
        hit = match_fixed_tool(question, load_fixed_tools())
        if hit:
            tool, params = hit
            if tool["database"] in cats:
                return [run_fixed_tool(tool, params, cats, user, refresh, scope=scope)]
        return [generate_and_run(question, cats, user, refresh, scope=scope)]
