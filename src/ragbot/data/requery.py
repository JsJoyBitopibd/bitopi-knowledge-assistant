"""Run the previous answer's query again, changed the way a follow-up asks (Phase T4).

agent/refine.py answers a follow-up from the rows already read when it can. When it cannot — the list stopped at its
row limit, the follow-up names a column the rows do not have ("style wise", "show the buyer beside each line"), it
asks for another period ("last week?") or for the next page — the earlier query is the starting point, cheapest first:

1. **the next page**: the same statement under OFFSET (data/paging.py) — no model;
2. **a fixed tool run again** with the parameter that changed — its period, its grouping, its sort — no model;
3. **one "modify this statement" call** (prompts/sql_modify.txt): the model sees the earlier question, its statement,
   column names and row count — never its rows — and the change asked for; the reply takes the same road as any
   model-written statement (guard, typed literals, scope filter, read-only connection).

Nothing here widens what the user may read: the databases they may not read are dropped first, every statement is
run under their scope, and a re-run fixed tool goes through run_fixed_tool's own checks.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from ..auth.models import Scope
from ..config import prompt, settings
from ..llm import get_chat
from ..models import QueryResult
from .catalog import Catalog, load_catalogs
from .guard import GuardError
from .schema_usage import log_generation

# labels the earlier answer's own cuts and reshapes added (T2, T3): never carried into a fresh query
_STALE = ("shown_label", "of_rows_label", "transform_label")


def requery(plan: Any, prior: QueryResult, previous_question: str, *, user: str = "", refresh: bool = False,
            scope: Scope, parent: str = "") -> Optional[QueryResult]:
    """A fresh result for a follow-up the stored rows could not answer, or None when there is no safe way to get one
    (the caller then treats the follow-up as a new question)."""
    known = load_catalogs()
    cats = {n: c for n, c in known.items() if scope.allows_database(c.restricted_to)}
    if prior.database not in cats:
        return None
    from .tools import load_fixed_tools
    base = prior.tool.split(":")[0]
    tool = next((t for t in load_fixed_tools() if t.get("name") == base), None)
    if plan.page is not None:
        r = run_page(prior, tool, cats, user=user, refresh=refresh, scope=scope)
    else:
        r = rerun_tool(plan, prior, tool, cats, user=user, refresh=refresh, scope=scope) if tool else None
        if r is None:
            r = modify_sql(plan, prior, previous_question, cats, user=user, refresh=refresh, scope=scope)
    if r is not None:
        r.derived_from = parent
    return r


# ---------------------------------------------------------------- 1. the next page
def run_page(prior: QueryResult, tool: Optional[dict[str, Any]], cats: dict[str, Catalog], *, user: str = "",
             refresh: bool = False, scope: Scope) -> Optional[QueryResult]:
    """The page after `prior`'s, of the same statement. A model-written statement is guarded again, the one place a
    statement may carry an OFFSET (data.max_page_offset); a fixed tool pages its own SQL."""
    from .paging import page_sql
    from .tools import _run_guarded, run_fixed_tool
    s = settings()
    size = int(s["data.max_rows"])
    page = prior.page + 1
    if page * size > int(s.get("data.max_page_offset", 2000)):
        return None
    if tool is not None:
        params = {k: v for k, v in prior.params.items() if k not in _STALE}
        r = run_fixed_tool(tool, params, cats, user, refresh, scope=scope, page=page)
    elif prior.tool.split(":")[0] == "generated":
        cat = cats[prior.database]
        try:
            sql = page_sql(prior.sql, cat.dialect, page, size, prior.key_columns)
            r = _run_guarded(sql, cat, scope=scope, user=user, refresh=refresh, tool="generated", cats=cats,
                             max_offset=int(s.get("data.max_page_offset", 2000)))
        except Exception as e:
            return QueryResult(database=prior.database, engine=prior.engine, views=prior.views, sql=prior.sql,
                               columns=[], rows=[], error=str(e)[:300])
    else:
        return None
    if r.error:
        return r
    first = page * size + 1
    labels = {"rows_from_label": str(first), "rows_to_label": str(first + len(r.rows) - 1)}
    return r.model_copy(update={"page": page, "transform": f"page {page + 1}", "source_rows": len(prior.rows),
                                "params": {**r.params, **labels}})


# ---------------------------------------------------------------- 2. a fixed tool, run again with what changed
def rerun_tool(plan: Any, prior: QueryResult, tool: dict[str, Any], cats: dict[str, Catalog], *, user: str = "",
               refresh: bool = False, scope: Scope) -> Optional[QueryResult]:
    """The tool with its period, grouping or sort changed, when those are all the follow-up asks and the tool has a
    parameter for each; None otherwise (the model is asked instead)."""
    from .modifiers import direction_words, order_clause
    from .tools import _group_by, _sort_column, _window, run_fixed_tool
    if plan.filters or plan.add or plan.keep or plan.scalar or plan.distinct \
            or set(plan.unresolved) - set(plan.regroup) - ({plan.resort} if plan.resort else set()):
        return None
    specs = tool.get("params") or {}
    params = {k: v for k, v in prior.params.items() if k not in _STALE}
    changed = False
    if plan.window:
        name = next((n for n, sp in specs.items() if sp.get("type") == "date_window"), None)
        if name is None:
            return None
        try:
            params["from"], params["to"], params[name] = _window(plan.window)
        except ValueError:
            return None
        changed = True
    if plan.regroup or plan.group_by:
        name = next((n for n, sp in specs.items() if sp.get("type") == "group_by"), None)
        if name is None:
            return None
        words = " and ".join(plan.regroup + [str(c) for c in plan.group_by])
        cols, heading = _group_by(f"{words} wise", specs[name], str(params.get("factory") or ""))
        if not cols:
            return None
        params[name] = cols
        params.update(heading)
        changed = True
    word = plan.resort or (plan.sort[0] if plan.sort else "")
    if word:
        name = next((n for n, sp in specs.items() if sp.get("type") == "order_by"), None)
        hit = _sort_column(word, specs[name]) if name else None
        if hit is None:
            return None
        col, kind = hit
        desc = plan.sort[1] if plan.sort and plan.sort[0] == word else kind == "number"
        params[name] = order_clause(col, desc)
        params["sort_label"] = f"sorted by {col} ({direction_words(desc, kind)})"
        changed = True
    if not changed:
        return None
    r = run_fixed_tool(tool, params, cats, user, refresh, scope=scope)
    if plan.limit and not r.error and len(r.rows) > plan.limit:
        rows = r.rows[-plan.limit:] if plan.from_end else r.rows[:plan.limit]
        cut = {"shown_label": f"{'last' if plan.from_end else 'first'} {plan.limit}", "of_rows_label": str(len(r.rows))}
        r = r.model_copy(update={"rows": rows, "params": {**r.params, **cut}})
    return r


# ---------------------------------------------------------------- 3. one model call: modify the statement
def _change(plan: Any, prior: QueryResult) -> str:
    """The follow-up in plain words for the model, from the plan — never the user's message as SQL."""
    from ..agent.refine import describe
    parts = []
    done = describe(plan, prior)
    if done:
        parts.append(done)
    if plan.add:
        parts.append("also return the column(s) for: " + ", ".join(plan.add))
    if plan.regroup:
        parts.append("group by: " + ", ".join(plan.regroup))
    if plan.resort:
        parts.append(f"sort by the column for \"{plan.resort}\"")
    others = [w for w in plan.unresolved if w not in plan.regroup and w != plan.resort]
    if others:
        parts.append("compute in the SQL: " + ", ".join(others))
    if plan.window:
        parts.append(f"the same for the period: {plan.window}")
    return "; ".join(parts)


def _literal(v: Any) -> str:
    from ..agent.templated import fmt
    return fmt(v)


def modify_sql(plan: Any, prior: QueryResult, previous_question: str, cats: dict[str, Catalog], *, user: str = "",
               refresh: bool = False, scope: Scope) -> Optional[QueryResult]:
    """One call to the SQL model to change the earlier statement; one retry after a guard or database error."""
    from .tools import _run_guarded, _split_database_line
    s = settings()
    cat = cats[prior.database]
    change = _change(plan, prior)
    if not change or not prior.sql:
        return None
    bound = {k: _literal(v) for k, v in prior.params.items() if not str(k).endswith("_label")
             and re.search(rf"[@:]{re.escape(str(k))}\b", prior.sql)}
    cap = int(s["data.max_rows"])
    from ..agent.refine import capped
    system = (prompt("sql_modify").replace("{catalog}", f"=== Database: {cat.database} ({cat.dialect}) ===\n"
                                                          f"{cat.render()}")
              .replace("{tables}", _tables_text(previous_question + " " + change, cat, scope))
              .replace("{today}", date.today().isoformat()))
    user_msg = (f"Previous question: {previous_question}\n"
                f"Previous statement (database {cat.database}, {cat.dialect}):\n{prior.sql[:1200]}\n"
                + (f"Its parameters had these values (write them as literals): "
                   f"{', '.join(f'@{k} = {v}' for k, v in bound.items())}\n" if bound else "")
                + f"Its columns: {', '.join(prior.columns[:30])}\n"
                f"It returned {len(prior.rows)} row(s)"
                + (f", stopping at its limit of {cap}" if capped(prior, cap) else "") + ".\n"
                f"Change asked for: {change}")
    messages = [{"role": "user", "content": user_msg}]
    chat = get_chat()
    gen = dict(user=user, agent=prior.agent, considered=[cat.database])
    last_err = ""
    for attempt in range(2):
        reply = chat.chat(messages, system=system, max_tokens=int(s.get("llm.max_tokens_sql", 1536)),
                          temperature=0, purpose="sql", user=user)       # an LLMError reaches the orchestrator
        raw = re.sub(r"^```\w*|```$", "", reply.text.strip(), flags=re.M).strip()
        if re.match(r"^\s*CANNOT\b", raw, re.I):
            # the listed columns cannot do it: say so by treating the follow-up as a new question, never by
            # answering with the earlier rows as if they had been changed
            log_generation(kind="modify", database_used=cat.database, guard_error="model: " + raw[:200], raw_reply=raw,
                           **gen)
            return None
        db_name, sql = _split_database_line(raw, {cat.database: cat}, cat.database)
        try:
            r = _run_guarded(sql, cats[db_name], scope=scope, user=user, refresh=refresh, tool="generated",
                             considered=[cat.database], agent=prior.agent, cats=cats)
            log_generation(kind="modify", database_used=db_name, rows=len(r.rows), stop_reason=reply.stop_reason or "",
                           raw_reply=raw, **gen)
            return r
        except Exception as e:                            # the guard's refusal or the database's error
            last_err = str(e)[:300]
            log_generation(kind="modify", database_used=db_name, guard_error=last_err if isinstance(e, GuardError)
                           else "", db_error="" if isinstance(e, GuardError) else last_err, raw_reply=raw, **gen)
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": prompt("sql_retry_error").replace("{error}", last_err)
                          .replace("{kinds}", "")}]
    return QueryResult(database=cat.database, engine=cat.engine, views=prior.views, sql=prior.sql, columns=[], rows=[],
                       error=f"the change could not be made: {last_err}", agent=prior.agent)


def _tables_text(text: str, cat: Catalog, scope: Scope) -> str:
    """The raw tables the change may need (schema RAG over the earlier question and the change), for a user who
    may read raw tables at all; '(none)' otherwise."""
    if not scope.all_factories:
        return "(none)"
    try:
        from .schema_index import join_hints
        from .tools import _schema_selection
        sel = _schema_selection(text, {cat.database: cat}, None).get(cat.database, [])
        return cat.render_selected(sel, join_hints(sel, cat)) or "(none)"
    except Exception:
        return "(none)"
