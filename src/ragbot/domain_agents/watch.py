"""Watch rules (Phase L): each agent's `watch:` list in config/agents/<agent>.yaml, run on a schedule by
scripts/watch.py with no model call. A rule is one read-only SELECT over rag.* views, grouped by Factory,
whose value column Python turns into a level with the rule's thresholds:

    watch:
      - name: behind_schedule
        title: '{n} confirmed order(s) past their ship date with no export invoice (last 30 days)'
        question: 'Which {factory} orders are behind schedule?'   # what a click on the brief asks
        database: BitopiSplint
        sql: SELECT Factory, COUNT(*) AS n FROM rag.vw_OrderShipment WHERE ... GROUP BY Factory
        value: n          # the column the levels read
        amber: 1          # value >= amber -> amber; >= red -> red (direction: below flips both)
        red: 50
        weight: 3         # rank in the attention list: consequence for shipment, highest first
        metric: Behind schedule   # optional: also shown as a number at the top of the brief
        kind: ops         # default; `data` = a check on the data itself (its age), shown as a warning line,
                          # left out of the health score and the attention list

A rule without amber/red is information only (a metric, never "attention"). Thresholds are provisional
until the Level-3 interviews set them (docs/AGENTS_DESIGN.md §5); they live in config, not code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Optional

from ..auth.models import Scope
from .registry import Agent

LEVELS = ("green", "amber", "red")


@dataclass
class Signal:
    agent: str
    rule: str
    factory: str
    level: str                 # green | amber | red | info
    value: Optional[float]
    title: str
    question: str
    weight: float
    metric: str
    database: str
    views: list[str]
    sql: str
    as_of: str                 # ISO time the rule's query ran
    values: dict[str, Any] = field(default_factory=dict)
    kind: str = "ops"          # ops = about the factory; data = about the data itself (e.g. how old it is)


def rules(agents: dict[str, Agent]) -> list[dict[str, Any]]:
    """Every agent's watch rules, each tagged with its agent."""
    out = []
    for a in agents.values():
        for r in a.extra.get("watch") or []:
            out.append({**r, "agent": a.name})
    return out


def validate(agents: dict[str, Agent], catalogs: dict[str, Any]) -> list[str]:
    """Problems in the watch rules: unknown database, SQL the guard refuses, no Factory column, bad levels."""
    from ..data.guard import guard
    problems, seen = [], set()
    for r in rules(agents):
        key = f"{r['agent']}.{r.get('name')}"
        if not r.get("name") or key in seen:
            problems.append(f"watch {key}: needs a unique name")
        seen.add(key)
        for k in ("title", "database", "sql", "value"):
            if not r.get(k):
                problems.append(f"watch {key}: missing {k}")
        cat = catalogs.get(r.get("database"))
        if cat is None:
            problems.append(f"watch {key}: database {r.get('database')!r} is not loaded")
            continue
        try:
            guard(r["sql"], cat.view_names, cat.dialect, 200)
        except Exception as e:
            problems.append(f"watch {key}: guard refuses the SQL: {e}")
        if r.get("kind", "ops") not in ("ops", "data"):
            problems.append(f"watch {key}: kind must be ops or data")
        if "factory" not in r.get("sql", "").lower():
            problems.append(f"watch {key}: the SQL must return a Factory column (signals are scoped by factory)")
        a, red = r.get("amber"), r.get("red")
        if (a is None) != (red is None):
            problems.append(f"watch {key}: give both amber and red, or neither (information only)")
        elif a is not None:
            below = r.get("direction") == "below"
            if (float(a) > float(red)) if not below else (float(a) < float(red)):
                problems.append(f"watch {key}: amber and red are in the wrong order for direction "
                                f"{'below' if below else 'above'}")
    return problems


def level(rule: dict[str, Any], value: Optional[float]) -> str:
    """green / amber / red from the rule's thresholds, or info when it has none."""
    a, r = rule.get("amber"), rule.get("red")
    if a is None or r is None:
        return "info"
    if value is None:
        return "green"
    if rule.get("direction") == "below":
        return "red" if value <= float(r) else "amber" if value <= float(a) else "green"
    return "red" if value >= float(r) else "amber" if value >= float(a) else "green"


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == datetime.min.time() else v.isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _shown(v: Any) -> str:
    """How a value reads in a title: 1,234 · 6.17 · 20 Sep 2026 (CLAUDE.md: dates display as 12 Oct 2026)."""
    if isinstance(v, bool) or v is None:
        return "" if v is None else str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-":
        try:
            return date.fromisoformat(v).strftime("%d %b %Y").lstrip("0")
        except ValueError:
            return v
    return str(v)


def _fill(template: str, values: dict[str, Any]) -> str:
    try:
        return template.format(**{k: _shown(v) for k, v in values.items()})
    except (KeyError, IndexError, ValueError):
        return template


Runner = Callable[..., tuple[list[str], list[list[Any]]]]


def run_rule(rule: dict[str, Any], cat: Any, runner: Runner, *, timeout: int = 60) -> list[Signal]:
    """One rule -> one signal per factory row. Guarded and expanded like any query; unrestricted scope,
    because the store keeps every factory and the brief filters by the viewer's scope when it reads."""
    from ..data.guard import guard
    from ..data.virtual import rewrite_virtual
    sql = rule["sql"].strip()
    safe = guard(sql, cat.view_names, cat.dialect, 200)
    sql_exec, views = rewrite_virtual(safe, cat, scope=Scope.unrestricted())
    cols, rows = runner(cat.engine, sql_exec, {}, conn_env=cat.connection_env, display_sql=safe,
                        tool=f"watch:{rule['agent']}.{rule['name']}", timeout=timeout,
                        database=getattr(cat, "connection_database", None))
    now = datetime.now().isoformat(timespec="seconds")
    out = []
    for row in rows:
        vals = {c: _plain(v) for c, v in zip(cols, row)}
        factory = str(vals.get("Factory") or vals.get("factory") or "").strip()
        raw = vals.get(rule["value"])
        value = float(raw) if isinstance(raw, (int, float)) else None
        vals["factory"] = factory
        out.append(Signal(agent=rule["agent"], rule=rule["name"], factory=factory, level=level(rule, value),
                          value=value, title=_fill(rule["title"], vals),
                          question=_fill(rule.get("question", ""), vals), weight=float(rule.get("weight", 1)),
                          metric=rule.get("metric", ""), database=cat.database, views=views, sql=safe, as_of=now,
                          values=vals, kind=rule.get("kind", "ops")))
    return out


def run_all(agents: dict[str, Agent], catalogs: dict[str, Any], runner: Runner, *,
            timeout: int = 60) -> tuple[list[Signal], list[str]]:
    """Every rule; a failing rule is reported and skipped, the others still run."""
    signals, errors = [], []
    for r in rules(agents):
        cat = catalogs.get(r.get("database"))
        if cat is None:
            errors.append(f"{r['agent']}.{r.get('name')}: database {r.get('database')!r} not loaded")
            continue
        try:
            signals += run_rule(r, cat, runner, timeout=timeout)
        except Exception as e:
            errors.append(f"{r['agent']}.{r['name']}: {e.__class__.__name__}: {str(e)[:200]}")
    return signals, errors
