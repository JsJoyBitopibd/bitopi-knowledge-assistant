"""Data-quality checks (Phase K4): read-only SELECTs from config/data_quality.yaml, evaluated in Python
and written as a Markdown report (scripts/data_quality.py). The checks describe the source systems; a
finding is for the data owners, never something the assistant works around silently."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from .virtual import validate_definition

KINDS = ("freshness", "count", "share")


@dataclass
class CheckResult:
    name: str
    agent: str
    database: str
    what: str
    status: str          # ok | stale | issue | info | error
    value: str           # what was found, in words
    fix: str = ""
    seconds: float = 0.0


def load_checks(path: Path) -> list[dict[str, Any]]:
    """The checks, validated: unique names, a known kind, one safe SELECT each."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    checks = data.get("checks") or []
    seen: set[str] = set()
    for c in checks:
        name = c.get("name")
        if not name or name in seen:
            raise ValueError(f"data quality check without a unique name: {name!r}")
        seen.add(name)
        if c.get("kind") not in KINDS:
            raise ValueError(f"{name}: kind must be one of {KINDS}")
        if not c.get("database") or not c.get("sql"):
            raise ValueError(f"{name}: database and sql are required")
        validate_definition(c["sql"], "tsql")   # one SELECT, no parameters, no write/system tokens
    return checks


def _as_date(v: Any) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.fromisoformat(str(v)[:19]).date()
    except ValueError:
        return None


def evaluate(check: dict[str, Any], cols: list[str], rows: list[list[Any]], today: date) -> tuple[str, str]:
    """(status, value) for one check's single result row. Pure: no database, no clock."""
    row = dict(zip([c.lower() for c in cols], rows[0])) if rows else {}
    kind = check["kind"]
    if kind == "freshness":
        last = _as_date(row.get("last_date"))
        if last is None:
            return "stale", "no rows"
        age = (today - last).days
        limit = int(check.get("max_age_days", 1))
        status = "stale" if age > limit else "ok"
        return status, f"last {last.isoformat()} ({age} day(s) ago; limit {limit})"
    if kind == "count":
        n = int(row.get("n") or 0)
        limit = check.get("max", 0)
        if limit is None:
            return "info", f"{n}"
        return ("issue" if n > int(limit) else "ok"), f"{n} (limit {limit})"
    bad, total = int(row.get("bad") or 0), int(row.get("total") or 0)
    share = bad / total if total else 0.0
    limit = float(check.get("max_share", 0))
    status = "issue" if total and share > limit else "ok"
    return status, f"{bad:,} of {total:,} ({share:.0%}; limit {limit:.0%})"


Runner = Callable[..., tuple[list[str], list[list[Any]]]]


def run_checks(checks: list[dict[str, Any]], catalogs: dict[str, Any], runner: Runner, *,
               timeout: int = 120, today: Optional[date] = None) -> list[CheckResult]:
    """Run each check read-only through `runner` (connectors.run) against its catalog's connection."""
    today = today or date.today()
    out: list[CheckResult] = []
    for c in checks:
        base = dict(name=c["name"], agent=c.get("agent", ""), database=c["database"], what=c.get("what", ""),
                    fix=c.get("fix", ""))
        cat = catalogs.get(c["database"])
        if cat is None:
            out.append(CheckResult(**base, status="error", value="database not catalogued"))
            continue
        t0 = time.perf_counter()
        try:
            cols, rows = runner(cat.engine, c["sql"].strip(), {}, conn_env=cat.connection_env,
                                tool=f"quality:{c['name']}", timeout=timeout,
                                database=getattr(cat, "connection_database", None))
            status, value = evaluate(c, cols, rows, today)
        except Exception as e:   # one failing check must not stop the report
            status, value = "error", f"{e.__class__.__name__}: {str(e)[:160]}"
        out.append(CheckResult(**base, status=status, value=value, seconds=round(time.perf_counter() - t0, 1)))
    return out


_ORDER = {"error": 0, "stale": 1, "issue": 2, "info": 3, "ok": 4}


def render_markdown(results: list[CheckResult], generated: datetime) -> str:
    counts = {s: sum(1 for r in results if r.status == s) for s in _ORDER}
    lines = [f"# Data-quality report — {generated:%d %b %Y %H:%M}", "",
             "Read-only checks from `config/data_quality.yaml` (Phase K4). "
             + ", ".join(f"{n} {s}" for s, n in counts.items() if n) + ".", ""]
    for agent in sorted({r.agent for r in results}):
        lines += [f"## {agent or 'other'}", "", "| Status | Check | Database | Found | Who should act |",
                  "|---|---|---|---|---|"]
        for r in sorted((r for r in results if r.agent == agent), key=lambda r: (_ORDER[r.status], r.name)):
            lines.append(f"| {r.status} | {r.what} (`{r.name}`) | {r.database} | {r.value} | {r.fix} |")
        lines.append("")
    return "\n".join(lines)
