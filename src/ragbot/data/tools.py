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
from ..agent.templated import is_blank_result
from ..auth.models import Scope
from ..config import prompt, settings
from ..llm import get_chat
from ..models import QueryResult
from ..ttl_cache import TTLCache
from .cache import cached_run
from .catalog import Catalog, catalog_stamp, load_catalogs
from .db_router import _STATEMENT, keyword_score, pick_catalogs
from .guard import GuardError, guard
from .schema_usage import log_generation
from .virtual import rewrite_virtual


# ---------------------------------------------------------------- periods
# The period a question names, shared by every fixed tool whose `match:` contains `{period}` (expanded on load)
# and by agent/not_found.py. Besides the relative words: dates written day first as in Bangladesh (6-10-2026,
# 06/10/2026, 2026-10-06, 7th october, Oct 7 2026), ranges (from 1 Oct to 5 Oct), months (in September,
# September 2026) and weekdays (monday, last monday). No capturing groups: a tool's group numbers stay as written.
# A bare month needs in / for / of / during or a year, so "May I see …" is not May.
_MON = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
        r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_DATE = (r"(?:\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}"
         rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MON})\.?(?:,?\s+\d{{4}})?"
         rf"|(?:{_MON})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?)")
PERIOD = (rf"(?:from\s+|between\s+)?{_DATE}\s*(?:to|till|until|and|-|–)\s*{_DATE}"   # ranges first: longest wins
          rf"|(?:on\s+)?{_DATE}"
          rf"|(?:on\s+|last\s+)?(?:{'|'.join(_WEEKDAYS)})"
          rf"|(?:in|for|of|during)\s+(?:{_MON})(?:\s+\d{{4}})?(?!\.?\s*\d)|(?:{_MON})\s+\d{{4}}"   # not "for october 7"
          r"|today|toady|yesterday|this week|last week|this month|last month|last \d+ days")
_MONTH_NUM = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                          "nov", "dec"), start=1)}


def _show(d: date) -> str:
    return f"{d.day} {d:%b %Y}"           # CLAUDE.md: dates display as 12 Oct 2026


def _one_date(s: str, today: date) -> date:
    """One written date, day first. Without a year: the latest such day that is not in the future."""
    s = re.sub(r"^(?:on|from|between)\s+", "", s.strip().lower()).rstrip(".")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s)
    if m:
        year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
        return date(year, int(m[2]), int(m[1]))
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)\.?(?:,?\s+(\d{4}))?", s)
    day, mon, year = (m[1], m[2], m[3]) if m else (None, None, None)
    if m is None:
        m = re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", s)
        if m is None:
            raise ValueError(f"not a date: {s!r}")
        mon, day, year = m[1], m[2], m[3]
    month = _MONTH_NUM.get(mon[:3])
    if month is None:
        raise ValueError(f"not a month: {mon!r}")
    if year:
        return date(int(year), month, int(day))
    d = date(today.year, month, int(day))
    return d if d <= today else date(today.year - 1, month, int(day))


def _next_month(d: date) -> date:
    return d.replace(year=d.year + 1, month=1) if d.month == 12 else d.replace(month=d.month + 1)


def _explicit_window(t: str, today: date) -> Optional[tuple[date, date, str]]:
    """(from, to, label) for a written date, range, month or weekday; None for anything else (the relative
    words). Raises ValueError for one that cannot be a real day (31-02-2026) or a range that runs backwards."""
    m = re.fullmatch(rf"(?:from\s+|between\s+)?({_DATE})\s*(?:to|till|until|and|-|–)\s*({_DATE})", t)
    if m:
        a, b = _one_date(m[1], today), _one_date(m[2], today)
        if b < a:
            raise ValueError(f"range ends before it starts: {t!r}")
        return a, b + timedelta(days=1), f"from {_show(a)} to {_show(b)}"
    m = re.fullmatch(rf"(?:on\s+)?({_DATE})", t)
    if m:
        d = _one_date(m[1], today)
        return d, d + timedelta(days=1), f"on {_show(d)}"
    m = re.fullmatch(rf"(?:on\s+|(last)\s+)?({'|'.join(_WEEKDAYS)})", t)
    if m:
        back = (today.weekday() - _WEEKDAYS.index(m[2])) % 7 or (7 if m[1] else 0)
        d = today - timedelta(days=back)
        return d, d + timedelta(days=1), f"on {_show(d)}"
    m = re.fullmatch(rf"(?:(?:in|for|of|during)\s+)?({_MON})\.?(?:\s+(\d{{4}}))?", t)
    if m:
        start = date(int(m[2]) if m[2] else today.year, _MONTH_NUM[m[1][:3]], 1)
        if not m[2] and start > today:
            start = start.replace(year=start.year - 1)
        return start, _next_month(start), f"in {start:%B %Y}"
    return None


def _window(text: str) -> tuple[date, date, str]:
    """(from, to, label) for a period phrase. The label reads after a heading and at the end of a follow-up
    question ("today", "this week", "on 6 Oct 2026", "in October 2026") and parses back to the same period."""
    t = text.strip().lower().rstrip(".")
    found = _explicit_window(t, date.today())
    if found:
        return found
    start, end = _date_window(t)
    return start, end, re.sub(r"\btoady\b", "today", t)


# ---------------------------------------------------------------- groupings and figures named in the question
# Owner's rule (8 Oct 2026): show what was asked, nothing else. "Unit, Line and Buyer wise DHU of TAL" is answered
# with exactly those columns, so the groupings (`type: group_by`) and the figures (`type: measures`) of a tool are
# parameters read from the question. Each maps to a column or expression the tool's YAML lists (a whitelist): no
# word of the question ever reaches the SQL as text.
_GROUP_MARK = re.compile(r"\bwise\b|\b(?:by|per|each|every)\b|\bbroken\s+down\b|\bbreak\s*-?\s*down\b", re.I)
_DIM_WORDS = (                          # word -> dimension key; PO/EO/order mean the file (VistaQ has no PO or EO)
    (r"factor(?:y|ies)", "factory"), (r"units?|blocks?", "unit"), (r"lines?", "line"),
    (r"buyers?|customers?|brands?", "buyer"), (r"styles?", "style"), (r"colou?rs?", "color"),
    (r"file\s+refs?|files?", "file"),
    (r"purchase\s+orders?|export\s+orders?|pos?|eos?|orders?", "po_eo"),
)
_DIM_RE = re.compile(r"\b(?:" + "|".join(f"(?P<d{i}>{p})" for i, (p, _) in enumerate(_DIM_WORDS)) + r")\b", re.I)
_DIM_NAMES = {"factory": "factory", "unit": "unit", "line": "line", "buyer": "buyer", "style": "style",
              "color": "colour", "file": "file"}
_PCT = r"\s*(?:rate|%|percent(?:age)?)"
_QTY = r"\s*(?:qty|quantity|pcs|pieces)"
_MEASURE_WORDS = (                      # tried in this order; a span already taken is not read twice
    (rf"\b(?:ftp|first[\s-]?pass){_QTY}\b", "ftp_qty"),
    (rf"\b(?:ftp|first[\s-]?pass)(?:{_PCT})?", "ftp_pct"),
    (rf"\bdefective{_PCT}", "defective_pct"),
    (rf"\bdefective(?:{_QTY})?\b", "defective"),
    (rf"\brectif(?:ied|ication){_PCT}", "rectified_pct"),
    (r"\brectif(?:ied|ication)\b", "rectified"),
    (rf"\breject(?:ion)?{_PCT}", "reject_pct"),
    (r"\breject(?:s|ed|ions?)?\b", "rejected"),
    (r"\bdhu\b", "dhu"),
    (r"\bdefects?\b", "defects"),
    (r"\b(?:checked|inspected)\b", "checked"),
    (r"\b(?:output|production)\b", "output"),
)


def _group_by(question: str, spec: dict[str, Any], factory: str) -> tuple[list[str], dict[str, str]]:
    """The dimension columns the question groups by, in its order, and the heading params ({by}, {note}).
    Empty when there is no grouping marker or no dimension word: the tool does not match then."""
    if not _GROUP_MARK.search(question):
        return [], {}
    allowed: dict[str, str] = spec.get("columns", {})
    keys: list[str] = []
    po_eo = False
    for m in _DIM_RE.finditer(question):
        key = next(_DIM_WORDS[i][1] for i in range(len(_DIM_WORDS)) if m.group(f"d{i}"))
        if key == "po_eo":
            key, po_eo = "file", True
        if key in allowed and key not in keys:
            keys.append(key)
    if not keys:
        return [], {}
    cols = [allowed[k] for k in keys]
    if not factory and "factory" in allowed and allowed["factory"] not in cols:
        cols.insert(0, allowed["factory"])      # unit names repeat across factories: say which, unless one is asked
    names = [_DIM_NAMES.get(k, k) for k in keys]
    by = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return cols, {"by": by, "note": spec.get("po_eo_note", "") if po_eo else ""}


def _measures(question: str, spec: dict[str, Any]) -> list[str]:
    """The figures the question names (keys of the tool's `columns:`), in its order; the tool's default if none."""
    allowed: dict[str, str] = spec.get("columns", {})
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, str]] = []
    for pat, key in _MEASURE_WORDS:
        for m in re.finditer(pat, question, re.I):
            if key in allowed and not any(m.start() < e and s < m.end() for s, e in taken):
                taken.append((m.start(), m.end()))
                found.append((m.start(), key))
    keys: list[str] = []
    for _, key in sorted(found):
        if key not in keys:
            keys.append(key)
    return keys or [k for k in spec.get("default", []) if k in allowed]


def tool_sql(tool: dict[str, Any], params: Optional[dict[str, Any]] = None) -> str:
    """The tool's SQL with its `{name}` placeholders for group_by / measures params filled from the whitelist in
    its YAML. Without params (check_catalog, view detection) the defaults are used."""
    sql = tool.get("sql", "")
    params = params or {}
    for name, spec in (tool.get("params") or {}).items():
        kind = spec.get("type")
        if kind == "group_by":
            cols = params.get(name) or [spec.get("columns", {}).get(k) for k in spec.get("default", ["factory"])]
            allowed = set(spec.get("columns", {}).values())
            cols = [c for c in cols if c in allowed]
            sql = sql.replace("{" + name + "}", ", ".join(cols))
        elif kind == "measures":
            keys = params.get(name) or spec.get("default", [])
            exprs = [spec["columns"][k] for k in keys if k in spec.get("columns", {})]
            sql = sql.replace("{" + name + "}", ", ".join(exprs))
    return sql


def _expand_list(sql: str, name: str, values: list[Any]) -> tuple[str, dict[str, Any]]:
    """`IN (@files)` -> `IN (@files_0, @files_1, …)` with one bind parameter per value (never SQL text)."""
    names = [f"{name}_{i}" for i in range(len(values))]
    return re.sub(rf"@{name}\b", ", ".join("@" + n for n in names), sql), dict(zip(names, values))


# ---------------------------------------------------------------- fixed tools
def load_fixed_tools(path: Path | None = None) -> list[dict[str, Any]]:
    """Cached on the file's mtime — this is read on every data question (and now on every route)."""
    path = path or settings().path("fixed_tools")
    if not path.exists():
        return []
    return list(_load_fixed_tools(path, path.stat().st_mtime_ns))


@lru_cache(maxsize=2)
def _load_fixed_tools(path: Path, _mtime: int) -> list[dict[str, Any]]:
    tools = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    for t in tools:
        if isinstance(t, dict) and isinstance(t.get("match"), str):
            t["match"] = t["match"].replace("{period}", PERIOD)
    return tools


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
    # single days first: "today" used to fall through to the 7-day default below
    if "yesterday" in t:
        return today - timedelta(days=1), today
    if "tomorrow" in t:
        return today + timedelta(days=1), today + timedelta(days=2)
    if "today" in t or "toady" in t:          # a typo staff really type ("defect summary toady")
        return today, today + timedelta(days=1)
    if "this week" in t:
        return monday, monday + timedelta(days=7)
    if "next week" in t:
        return monday + timedelta(days=7), monday + timedelta(days=14)
    if "last week" in t:
        return monday - timedelta(days=7), monday
    if "last month" in t:
        start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        return start, today.replace(day=1)
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
    # a pasted card list or table header arrives on many lines; `.` in the patterns does not cross a newline
    question = re.sub(r"\s+", " ", question).strip()
    for tool in tools:
        m = re.search(tool["match"], question)
        if not m:
            continue
        if any(not re.search(r, question) for r in tool.get("requires", [])):
            continue
        # a qualifier the tool's SQL cannot apply (a stage, a PO, a figure it does not return): the model writes
        # it. One pattern or a list of them; any match skips the tool.
        skip = tool.get("unless")
        if skip and any(re.search(p, question) for p in ([skip] if isinstance(skip, str) else skip)):
            continue
        params: dict[str, Any] = {}
        for name, spec in tool.get("params", {}).items():
            if spec.get("type") == "group_by":
                cols, heading = _group_by(question, spec, str(params.get("factory") or ""))
                if not cols:
                    break                            # nothing to group by: not this tool
                params[name] = cols
                params.update(heading)
                continue
            if spec.get("type") == "measures":
                params[name] = _measures(question, spec)
                continue
            if spec.get("type") == "value":
                # P2.1: a recorded name the question uses — however spelled — from the value index; the bound
                # parameter is the value as recorded, never the question's text. '' when none or several match
                # (the tool then runs for all of them). Visibility is not decided here: the view's scope filter is.
                from .values import resolve
                try:
                    hit, cands = resolve(question, Scope.unrestricted(), spec.get("entity", "store"))
                except Exception:
                    hit, cands = None, []
                params[name] = hit.value if hit else spec.get("default", "")
                params[f"{name}_label"] = (spec.get("label", "in {value}").format(value=hit.value) if hit
                                           else spec.get("else_label", ""))
                continue
            if spec.get("type") in ("factory", "line", "unit", "style"):
                # Phase Q: the factory, line, unit or style the question names, read once by
                # data/entities.py:question_entities — "TAL line 2102", "line no. 2102", "tarasima L-2102".
                # Two of a kind ("lines 2102 and 2103") is not this tool's shape: the model writes the filter.
                from .entities import question_entities
                kind = spec["type"]
                ents = question_entities(question)
                found = {"factory": ents.factories, "line": ents.lines, "unit": ents.units,
                         "style": ents.styles}[kind]
                raw = found[0] if len(found) == 1 else (None if found else spec.get("default"))
                if raw is None:
                    break
                params[name] = str(raw).upper() if spec.get("upper", kind == "factory") else raw
                # no trailing space in a label: templated._fill strips string parameters, so a template writes
                # "{factory_label} line {line}" and the empty case's double space is collapsed there
                params[f"{name}_label"] = (spec.get("label", "{value}").format(value=params[name]) if found
                                           else spec.get("else_label", ""))
                continue
            if spec.get("type") == "entity":
                # P1: a company named by code ("companyid=02") or by name/alias ("misami") becomes a bound parameter
                # from config/reference/entities.yaml — never the question's text. '' (the default) means "any".
                from .entities import company_from_question
                c = company_from_question(question) if spec.get("entity", "company") == "company" else None
                params[name] = c.code if c else spec.get("default", "")
                params[f"{name}_label"] = spec.get("label", "at company {code}").format(code=c.code) if c else ""
                continue
            groups = spec.get("from_group", [])
            groups = groups if isinstance(groups, list) else [groups]
            # m.re.groups, not m.lastindex: with nested groups ("(FR-(\d{2}-\d+))") lastindex is the outer one
            raw = next((m.group(g) for g in groups if g <= m.re.groups and m.group(g)), None)
            if raw is None:
                raw = spec.get("default")      # e.g. window: { default: 'last 30 days' } for an optional phrase
            if raw is None:
                break
            if spec.get("type") == "date_window":
                try:
                    # the label ("next week", "on 6 Oct 2026") is for templated answers; not in the SQL
                    params["from"], params["to"], params[name] = _window(raw)
                except ValueError:
                    return None    # a date we cannot read (31-02-2026): the model answers, no tool assumes a day
                continue
            if spec.get("type") == "int":
                raw = int(raw)
            if spec.get("upper"):
                raw = str(raw).upper()
            params[name] = raw
        else:
            if "@today" in tool.get("sql", "") or "@today" in tool.get("local_sql", ""):
                params["today"] = date.today()   # "has passed" = before today, not before the window's end
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


def _params_in(sql: str, params: dict[str, Any], dialect: str) -> dict[str, Any]:
    """Only the parameters the statement actually references. A parameter the SQL no longer uses must not
    reach the result cache's key, or the same rows would be read once per factory (Phase Q, Q4)."""
    marker = "@" if dialect == "tsql" else ":"
    return {k: v for k, v in params.items() if re.search(rf"{re.escape(marker)}{re.escape(str(k))}\b", sql)}


def _apply_filter(tool: dict[str, Any], params: dict[str, Any], columns: list[str],
                  rows: list[list[Any]]) -> tuple[list[str], list[list[Any]]]:
    """A tool's `filter:` narrows a result the database returned for every factory (Phase Q, Q4).

    `(@factory = '' OR Factory = @factory)` inside five subqueries of the production dashboard gave SQL Server
    a far worse plan than the plain all-factory query: measured 11 Oct 2026, the all-factory run took 8.1 s and
    the same statement with `@factory = 'TAL'` hit the 30 s timeout. Reading every factory once and keeping the
    asked one here is faster, and one cached result then serves every factory of that scope.

    `rows:` keeps the rows whose column equals the parameter; `columns:` keeps the listed columns (a `{param}`
    placeholder names one). An empty parameter means "every factory": nothing is filtered.
    """
    spec = tool.get("filter") or {}
    if not spec:
        return columns, rows
    cols, out = list(columns), [list(r) for r in rows]
    rowspec = spec.get("rows") or {}
    if rowspec:
        want = str(params.get(rowspec.get("param", "factory"), "") or "").strip()
        if want:
            column = rowspec["column"]
            if column not in cols:
                # never silently hand back every factory's rows for a question about one
                raise KeyError(f"{tool['name']}: filter column {column!r} is not in the result")
            i = cols.index(column)
            out = [r for r in out if str(r[i]).strip().upper() == want.upper()]
    colspec = spec.get("columns") or {}
    if colspec:
        keep = [str(c).format(**{k: str(v or "") for k, v in params.items()}) for c in colspec.get("keep", [])]
        if all(keep):                      # a placeholder that resolved to "" means "show them all"
            picked = [c for c in keep if c in cols]
            if picked:
                idx = [cols.index(c) for c in picked]
                cols, out = picked, [[r[i] for i in idx] for r in out]
    return cols, out


def run_fixed_tool(tool: dict[str, Any], params: dict[str, Any], cats: dict[str, Catalog], user: str = "",
                   refresh: bool = False, *, scope: Scope) -> QueryResult:
    cat = cats[tool["database"]]
    sql = tool_sql(tool, params).strip()
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
        # rewrite_virtual only reports rag.* views, so a fixed tool over a discovered table (the HR ones,
        # whose databases have no rag schema) had an empty [D#] card: database, SQL and time but no table
        # name, against docs/REFERENCE_FORMAT.md. Same line as _run_guarded and the pasted-statement path.
        views += [n for n in _views_in(sql, cat) if not cat.view(n)]
        # `heavy: true` in fixed_tools.yaml: a known-slow query (e.g. over the 4M-row, unindexed
        # dbo.ExportOrderBack) gets the longer timeout tier instead of failing at the default.
        timeout = int(settings().get("data.timeout_seconds_heavy", 30)) if tool.get("heavy") else None
        # Only the parameters the statement uses are bound and keyed; the result keeps the full set, so the
        # templated lead, the [D#] card and the citation check still see the factory that was asked for.
        bound = _params_in(sql, params, cat.dialect)
        cols, rows, as_of = cached_run(cat.engine, sql_exec, bound, conn_env=cat.connection_env, display_sql=sql,
                                       tool=tool["name"], user=user, refresh=refresh, timeout=timeout,
                                       scope_key=scope.key(), database=getattr(cat, "connection_database", None),
                                       ttl=tool.get("cache_ttl_seconds"))
        cols, rows = _apply_filter(tool, params, cols, rows)
        return QueryResult(database=cat.database, engine=cat.engine, views=views, sql=sql, sql_executed=sql_exec,
                           params=params, tool=tool["name"], columns=cols, rows=rows, key_columns=_keys_for(views, cat),
                           as_of=as_of)
    except Exception as e:
        return QueryResult(database=cat.database, engine=cat.engine, views=_views_in(sql, cat), sql=sql,
                           params=params, tool=tool["name"], columns=[], rows=[], error=str(e)[:300])


# ---------------------------------------------------------------- generated SQL
def companion_tool(tool: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The tool-shaped spec of a tool's `companion:` (its own database, SQL and answer_list), or None."""
    comp = tool.get("companion")
    if not comp:
        return None
    return {**comp, "name": f"{tool['name']}:companion", "agent": comp.get("agent", tool.get("agent", ""))}


def run_companion(tool: dict[str, Any], result: QueryResult, cats: dict[str, Catalog], user: str = "",
                  refresh: bool = False, *, scope: Scope) -> Optional[QueryResult]:
    """A second result from another database for the rows just read, e.g. each VistaQ file's export orders and POs
    from the ERP (VistaQ records QC per file, not per PO or EO). Runs only when the main result has the companion's
    `column`; its distinct values (at most 200, as many as a result has rows) are bound as parameters. The answer cites it as [D2] with its own
    database, SQL and as-of time."""
    comp = companion_tool(tool)
    if comp is None or result.error or not result.rows or comp.get("column") not in result.columns \
            or comp.get("database") not in cats:
        return None
    i = result.columns.index(comp["column"])
    values = sorted({str(r[i]) for r in result.rows if r[i] not in (None, "")})[:200]
    if not values:
        return None
    sql, params = _expand_list(comp["sql"], comp.get("param", "values"), values)
    # P3: the parent's own parameters (a company, a store, a window) are available to the companion's SQL too
    params = {**{k: v for k, v in result.params.items() if k not in params}, **params}
    out = run_fixed_tool({**comp, "sql": sql}, params, cats, user, refresh, scope=scope)
    out.agent = comp["agent"]
    return out


def merge_companion(main: QueryResult, extra: QueryResult, spec: dict[str, Any]) -> Optional[QueryResult]:
    """P3 (the unbuilt ROADMAP K2 line): two databases' rows in one table. A companion that declares
    `merge: {on: [key columns], add: [columns]}` has its listed columns left-joined onto the main result's rows
    by key, in Python — every cell is copied verbatim from one of the two row sets, so the citation verifier
    licenses the merged table from [D1] and [D2] together; a main row with no companion match shows "—".
    Returns None when a key or added column is missing from either side."""
    # YAML 1.1 turns a bare `on:` key into the boolean True (found 2026-10-09): accept both spellings
    on = [str(c) for c in (spec.get("on") or spec.get(True) or [])]
    add = [str(c) for c in spec.get("add") or []]
    if not on or not add or any(c not in main.columns for c in on) or any(c not in extra.columns for c in on + add):
        return None
    mi = [main.columns.index(c) for c in on]
    ei = [extra.columns.index(c) for c in on]
    ai = [extra.columns.index(c) for c in add]
    index: dict[tuple, list[Any]] = {}
    for row in extra.rows:
        index.setdefault(tuple(str(row[j]) for j in ei), row)
    rows = [list(row) + ([index[tuple(str(row[j]) for j in mi)][k] for k in ai]
                         if tuple(str(row[j]) for j in mi) in index else [None] * len(ai)) for row in main.rows]
    added = [c if c not in main.columns else f"{c} ({extra.database})" for c in add]
    return QueryResult(database=f"{main.database} + {extra.database}", engine=main.engine, views=main.views + extra.views,
                       sql=main.sql, sql_executed=main.sql_executed, params=main.params, tool=f"{main.tool}:merged",
                       columns=main.columns + added, rows=rows, key_columns=main.key_columns,
                       as_of=min(main.as_of, extra.as_of), agent=main.agent)


def _views_in(sql: str, cat: Catalog, siblings: Optional[dict[str, Catalog]] = None) -> list[str]:
    """Curated views and discovered tables the statement reads (for the [D#] reference); a sibling database's
    table as `Database.schema.table` (P3) when the statement names one the sibling offers."""
    three: set[str] = set()
    try:
        tree = parse_one(sql, read=cat.dialect)
        names = set()
        for t in tree.find_all(exp.Table):
            if t.catalog and siblings:
                sib = next((s for n, s in siblings.items() if n.lower() == t.catalog.lower()), None)
                if sib is not None and sib.table(f"{t.db}.{t.name}"):
                    three.add(f"{next(n for n in siblings if n.lower() == t.catalog.lower())}.{t.db}.{t.name}")
                continue
            names.add(".".join(p for p in [t.db, t.name] if p))
    except Exception:
        names = set(re.findall(r"\b\w+\.\w+", sql, re.IGNORECASE))
    return sorted(n for n in names if cat.view(n) or cat.table(n)) + sorted(three)


def _keys_for(views: list[str], cat: Catalog, siblings: Optional[dict[str, Catalog]] = None) -> list[str]:
    """Row-key columns for the [D#] reference: a curated view's key_columns, a raw table's primary key (a sibling
    database's table through that sibling's catalog)."""
    keys: list[str] = []
    for v in views:
        vw = cat.view(v)
        if vw:
            cols = vw.key_columns
        elif cat.table(v):
            cols = cat.table(v).primary_key
        elif siblings and v.count(".") == 2:
            db, rest = v.split(".", 1)
            sib = siblings.get(db)
            cols = sib.table(rest).primary_key if sib is not None and sib.table(rest) else []
        else:
            cols = []
        keys += [k for k in cols if k not in keys]
    return keys


def _query_vector(question: str) -> Optional[list[float]]:
    """The question's bge-m3 vector (the same cached embedding document retrieval uses), for the schema
    index and the database router; None when no embedder is available (keyword-only still works)."""
    try:
        from ..retrieve.retriever import _embed_query
        return list(_embed_query(question))
    except Exception:
        return None


def _schema_selection(question: str, cats: dict[str, Catalog],
                      qvec: Optional[list[float]] = None) -> dict[str, list[str]]:
    """Phase C5: the discovered tables to show for this question, per database ({} when schema RAG is
    off or no catalog has a discovered tier). Uses the same query vector as document retrieval."""
    s = settings()
    if not s.get("data.schema_rag", True) or not any(c.offered_tables for c in cats.values()):
        return {}
    from .schema_index import select_tables
    k = int(s.get("data.schema_rag_k", 6))
    cap = int(s.get("data.schema_rag_max_tables", 14))
    return {name: select_tables(question, c, k, qvec, max_total=cap) for name, c in cats.items() if c.offered_tables}


def siblings_of(cat: Catalog, cats: dict[str, Catalog]) -> dict[str, Catalog]:
    """The other catalogs of `cats` on the same SQL Server instance as `cat` (same connection line, or two lines
    whose Server= is the same), keyed by the SQL database name a three-part reference must use (P3). Empty
    unless data.cross_database_names is on. The VistaQ pair is on another host and never joins the 17."""
    from .connectors import server_of, sql_database
    if not settings().get("data.cross_database_names", False) or cat.dialect != "tsql":
        return {}
    mine = server_of(cat.connection_env)
    out: dict[str, Catalog] = {}
    for other in cats.values():
        if other is cat or other.dialect != "tsql" or not other.routable:
            continue
        same_line = other.connection_env == cat.connection_env
        if same_line or (mine and server_of(other.connection_env) == mine):
            name = sql_database(other.connection_env, getattr(other, "connection_database", None))
            if name:
                out[name] = other
    return out


def _guard_for(sql: str, cat: Catalog, max_rows: int, scope: Scope,
               siblings: Optional[dict[str, Catalog]] = None) -> str:
    """Guard with the catalog's FULL offered set as the allow-list (security), independent of which
    tables the prompt happened to show (relevance). Raw tables have no common factory column to filter
    on, so a user limited to some factories may read the rag views only — and may never cross databases."""
    offered = cat.offered_tables if scope.all_factories else []
    big_rows = int(settings().get("data.big_table_rows", 1_000_000))
    big = {t.name.lower(): t.rows for t in offered if t.rows and t.rows > big_rows}
    allowed_catalogs: Optional[dict[str, frozenset[str]]] = None
    # Phase R: a catalog with its own sensitive list (the HR databases) never crosses a three-part name. The
    # guard's column check cannot tell which database a column belongs to, so the looser list would otherwise
    # apply to a sibling's columns as well. Cross-database joins from such a database need another mechanism.
    if cat.sensitive_patterns is not None:
        siblings = None
    if siblings and scope.all_factories:
        allowed_catalogs = {}
        for db_name, sib in siblings.items():
            allowed_catalogs[db_name.lower()] = frozenset(t.name.lower() for t in sib.offered_tables)
            big.update({f"{db_name.lower()}.{t.name.lower()}": t.rows for t in sib.offered_tables
                        if t.rows and t.rows > big_rows})
    return guard(sql, cat.view_names, cat.dialect, max_rows,
                 allowed_tables=frozenset(t.name.lower() for t in offered), big_tables=big,
                 allowed_catalogs=allowed_catalogs, sensitive_patterns=cat.sensitive_patterns)


def _guard_and_fix(sql: str, cat: Catalog, max_rows: int, scope: Scope,
                   siblings: Optional[dict[str, Catalog]] = None, *,
                   mode: str = "known") -> tuple[str, list[str]]:
    """Guard a model-written statement, then quote any bare number it compares to a text column (Phase Q,
    data/typefix.py) and guard the result again — the fix only adds quotes, so it passes, and the guard is
    never asked to trust text it has not seen."""
    safe = _guard_for(sql, cat, max_rows, scope, siblings)
    if not settings().get("data.typefix.enabled", True):
        return safe, []
    from . import typefix
    fixed, notes = typefix.fix(safe, cat, mode=mode, siblings=siblings)
    if not notes:
        return safe, []
    return _guard_for(fixed, cat, max_rows, scope, siblings), notes


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


def _pick_catalog(question: str, cats: dict[str, Catalog]) -> Catalog:
    """Fallback when the model's reply names no database: the best keyword match (db_router), among the
    routable catalogs when there are any."""
    pool = [c for c in cats.values() if c.routable] or list(cats.values())
    return max(pool, key=lambda c: keyword_score(question, c))


# Question -> (database, guarded SQL) the model wrote for it (J2). A repeated question — the same one
# from another user, a dashboard chip, a follow-up after Refresh — re-executes that SQL against the live
# database and skips the 3-5 s generation call. Keyed on the day (the model writes literal dates for
# "this month"), the catalogs' file stamp (an edited catalog may change the right SQL), the catalogs
# picked and the user's scope. Only SQL that passed the guard AND ran is stored, and it is guarded
# again on replay. Setting: data.sql_cache_ttl_seconds (default a day; 0 = off).
_SQL = TTLCache(maxsize=512)


def _sql_key(question: str, cats: dict[str, Catalog], scope: Scope, agent: str = "") -> tuple:
    norm = re.sub(r"\s+", " ", question.strip().lower()).rstrip("?!. ")
    return norm, tuple(sorted(cats)), catalog_stamp(), scope.key(), date.today().isoformat(), agent


def _named_catalog(name: str, cats: dict[str, Catalog]) -> Optional[str]:
    """The catalog a model-written DATABASE line names. The prompt shows each database with its dialect
    ("Hrms5Misami (tsql)") and the model copies that label, so an exact-only match found nothing and the
    statement silently went to the keyword fallback's database instead — on 2026-10-13 that sent a correct
    HR statement to BitopiSplint, whose guard then refused dbo.vwAllCompanyEmployeeList and the question
    answered not-found (golden case 100). Match the name as written first, then the bare identifier in
    front of whatever qualifier the label carries."""
    def _clean(s: str) -> str:
        return s.strip().strip("`\"'[]").strip()

    name = _clean(name)
    if not name:
        return None
    exact = next((k for k in cats if k.lower() == name.lower()), None)
    if exact:
        return exact
    bare = _clean(re.split(r"[\s(,;]", name, maxsplit=1)[0])
    return next((k for k in cats if bare and k.lower() == bare.lower()), None)


def _split_database_line(raw: str, cats: dict[str, Catalog], fallback_db: str) -> tuple[str, str]:
    """First line 'DATABASE: <name>' picks the catalog; anything else falls back to keyword scoring."""
    lines = raw.splitlines()
    if lines and lines[0].strip().upper().startswith("DATABASE:"):
        name = lines[0].split(":", 1)[1].strip()
        match = _named_catalog(name, cats)
        sql = "\n".join(lines[1:]).strip()
        return (match or fallback_db), sql
    return fallback_db, raw


def _run_guarded(sql: str, cat: Catalog, *, scope: Scope, user: str, refresh: bool, tool: str = "generated",
                 considered: list[str] = (), agent: str = "", cats: Optional[dict[str, Catalog]] = None) -> QueryResult:
    """One statement through the chain every model-written, corrected or lookup statement takes: guard (the
    catalog's full allow-list) -> virtual-view rewrite with the scope filter -> parameters -> cached_run.
    Raises GuardError / the database's error; a statement that reads no table is refused."""
    s = settings()
    siblings = siblings_of(cat, {c.database: c for c in (cats or {}).values()}) if cats else {}
    safe, _notes = _guard_and_fix(sql, cat, int(s["data.max_rows"]), scope, siblings)
    sql_exec, views = rewrite_virtual(safe, cat, scope=scope)
    views += [n for n in _views_in(safe, cat, siblings) if not cat.view(n)]
    if not views:
        raise GuardError("the generated SQL reads no view or table (the model found none)")
    params = _extract_params(safe, cat)
    cols, rows, as_of = cached_run(cat.engine, sql_exec, params, conn_env=cat.connection_env, display_sql=safe,
                                   tool=tool, user=user, refresh=refresh, scope_key=scope.key(),
                                   database=getattr(cat, "connection_database", None))
    return QueryResult(database=cat.database, engine=cat.engine, views=views, sql=safe, sql_executed=sql_exec,
                       params=params, tool=tool, columns=cols, rows=rows, key_columns=_keys_for(views, cat, siblings),
                       as_of=as_of, databases_considered=list(considered), agent=agent)


def generate_and_run(question: str, cats: dict[str, Catalog], user: str = "", refresh: bool = False, *,
                     scope: Scope, qvec: Optional[list[float]] = None, agent: str = "") -> QueryResult:
    """One SQL-generation call sees the catalogs picked for the question (data/db_router.py) and must
    name the database it chose (`DATABASE: <name>`) before the SQL, so the guard's view allow-list
    matches the right catalog. A question answered before today replays its cached SQL instead (J2).
    `agent` (Phase K) adds that domain agent's charter to the prompt."""
    from ..domain_agents import charter_text
    s = settings()
    considered = list(cats)
    ttl = int(s.get("data.sql_cache_ttl_seconds", 86400))
    key = _sql_key(question, cats, scope, agent) if ttl > 0 else None
    cached = _SQL.get(key) if key is not None and not refresh else None
    chat = get_chat()
    catalogs_text = "\n\n".join(
        f"=== Database: {name} ({c.dialect}) — {c.description or 'no description'} ===\n{c.render()}"
        for name, c in cats.items())
    from .schema_index import join_hints
    # raw tables are not offered to a user limited to some factories (see _guard_for)
    with trace.span("data.schema"):
        selected = _schema_selection(question, cats, qvec) if scope.all_factories else {}
    from .schema_usage import log_selection
    for name, sel in selected.items():
        log_selection(name, sel, question, user)       # which tables to sample (G2)
    tables_text = "\n\n".join(t for t in (cats[n].render_selected(sel, join_hints(sel, cats[n]))
                                          for n, sel in selected.items()) if t)
    # Known values (P2.1): the recorded spelling of a store, company or category the question names, so the
    # model filters on the value as recorded, never on the question's spelling. Scoped: nothing the user may not see.
    values_text = "(none)"
    if s.get("data.values.enabled", True):
        try:
            from .entities import has_company_column
            from .values import known_values_lines
            lines = known_values_lines(question, scope, list(cats),
                                       with_company=has_company_column(cats.values()))
            values_text = "\n".join(f"- {ln}" for ln in lines) if lines else "(none)"
        except Exception:
            values_text = "(none)"
    # Phase Q: the records the question names and the column of the shown views that holds each, with its kind
    entities_text = "(none)"
    try:
        from .entities import entity_lines
        found = entity_lines(question, list(cats.values()))
        entities_text = "\n".join(f"- {ln}" for ln in found) if found else "(none)"
    except Exception:
        entities_text = "(none)"
    # P3: when the picked catalogs share one SQL Server instance and the owner turned it on, a statement may read a
    # sibling database's listed tables as Database.schema.table (the guard checks each against its own catalog)
    cross_rule = "Never with a database prefix."
    if s.get("data.cross_database_names", False):
        from .connectors import sql_database
        groups = {n: siblings_of(c, cats) for n, c in cats.items()}
        joined = sorted({sql_database(c.connection_env, getattr(c, "connection_database", None))
                         for n, c in cats.items() if groups[n]} | {m for g in groups.values() for m in g})
        if len(joined) >= 2:
            cross_rule = (f"The databases {', '.join(joined)} are on one server: a query opened on one of them may "
                          f"read another's listed tables as Database.schema.name (e.g. {joined[0]}.dbo.<table>), "
                          "never its rag.* views; prefer one database when it suffices.")
    # Static text first (rules, curated views, examples), the per-question tables last: a stable
    # prefix lets the provider reuse its cached prompt across questions.
    system = (prompt("sql_generate").replace("{agent}", charter_text(agent))
              .replace("{cross_db_rule}", cross_rule)
              .replace("{catalogs}", catalogs_text)
              .replace("{tables}", tables_text or "(none selected for this question)")
              .replace("{values}", "Known values (recorded spellings of words in the question):\n" + values_text)
              .replace("{entities}", "Records the question names (filter on these columns):\n" + entities_text)
              .replace("{today}", date.today().isoformat()))
    messages = [{"role": "user", "content": question}]
    fallback = _pick_catalog(question, cats)
    last_err = ""
    t_start = time.perf_counter()
    attempts = 1 + int(s["data.sql_retries"]) + (1 if cached else 0)   # a failed replay costs no model attempt
    # A statement that returned nothing is remembered only briefly (P0): long enough that a dashboard chip
    # or a repeat within minutes replays it without a model call, never for the rest of the day — the
    # 8 Oct 2026 closing-balance question replayed its NULL for nine minutes and failed twice.
    short_ttl = min(ttl, int(s.get("data.result_cache_ttl_seconds", 180)))
    gen = dict(user=user, agent=agent, considered=considered)
    for attempt in range(attempts):
        replay = attempt == 0 and cached is not None
        if replay:
            db_name, sql = cached
            raw, db_line, stop = None, "", ""
        else:
            # ChatModel.chat() records the call as `llm.sql` itself (ragbot/trace.py): no span here
            reply = chat.chat(messages, system=system, max_tokens=int(s.get("llm.max_tokens_sql", 1536)),
                              temperature=0, purpose="sql", user=user)
            raw = re.sub(r"^```\w*|```$", "", reply.text.strip(), flags=re.M).strip()
            stop = reply.stop_reason or ""
            db_name, sql = _split_database_line(raw, cats, fallback.database)
            db_line = raw.splitlines()[0].strip() if raw.upper().startswith("DATABASE:") else ""
        cat = cats[db_name]
        siblings = siblings_of(cat, cats)        # same-server catalogs a three-part name may read (P3, opt-in)
        kind = "replay" if replay else ("retry" if attempt > (1 if cached else 0) else "generate")
        try:
            safe, fixes = _guard_and_fix(sql, cat, int(s["data.max_rows"]), scope, siblings)
            if fixes:
                log_generation(kind="typefix", database_line=db_line, database_used=db_name,
                               raw_reply="; ".join(fixes), **gen)
            sql_exec, views = rewrite_virtual(safe, cat, scope=scope)
            views += [n for n in _views_in(safe, cat, siblings) if not cat.view(n)]   # raw tables, for the [D#] reference
            if not views:
                # A statement that reads no view or table — the model's way of saying "not found", e.g.
                # SELECT 'System doesn''t have the data.' — is not a source: its constant row would become
                # a cited "database answer" (found by the scope leak suite, case s33, 2026-10-06).
                log_generation(kind="guard", database_line=db_line, database_used=db_name, fallback=not db_line,
                               guard_error="reads no view or table", stop_reason=stop, raw_reply=raw or sql, **gen)
                return QueryResult(database=cat.database, engine=cat.engine, views=[], sql=safe, columns=[],
                                   rows=[], error="the generated SQL reads no view or table (the model found none)",
                                   databases_considered=considered, agent=agent)
            params = _extract_params(safe, cat)
            try:
                cols, rows, as_of = cached_run(cat.engine, sql_exec, params, conn_env=cat.connection_env,
                                               display_sql=safe, user=user, refresh=refresh, scope_key=scope.key(),
                                               database=getattr(cat, "connection_database", None))
            except Exception as db_err:
                # Phase Q: the statement compared a number to a column that holds text, and the server tried to
                # cast the column. Quote the literals once more — this time on columns whose kind nothing
                # records too — and run it again, with no model call. Only then is the model re-asked.
                from . import typefix
                if fixes or not typefix.is_conversion_error(str(db_err)):
                    raise
                safe2, fixes = _guard_and_fix(sql, cat, int(s["data.max_rows"]), scope, siblings, mode="aggressive")
                if not fixes:
                    raise
                log_generation(kind="typefix", database_line=db_line, database_used=db_name,
                               db_error=str(db_err)[:300], raw_reply="; ".join(fixes), **gen)
                safe = safe2
                sql_exec, views = rewrite_virtual(safe, cat, scope=scope)
                views += [n for n in _views_in(safe, cat, siblings) if not cat.view(n)]
                params = _extract_params(safe, cat)
                cols, rows, as_of = cached_run(cat.engine, sql_exec, params, conn_env=cat.connection_env,
                                               display_sql=safe, user=user, refresh=refresh, scope_key=scope.key(),
                                               database=getattr(cat, "connection_database", None))
            blank = is_blank_result(cols, rows)
            log_generation(kind=kind, database_line=db_line, database_used=db_name, fallback=bool(raw) and not db_line,
                           rows=0 if blank else len(rows), stop_reason=stop, raw_reply=raw or sql, **gen)
            qr = QueryResult(database=cat.database, engine=cat.engine, views=views, sql=safe, sql_executed=sql_exec,
                             params=params, columns=cols, rows=rows, key_columns=_keys_for(views, cat, siblings),
                             as_of=as_of, databases_considered=considered, sql_cached=replay, agent=agent)
            if blank and not replay:
                qr = _repair_blank(qr, question, cats, cat, raw or "", messages, system, chat, selected.get(db_name, []),
                                   scope=scope, user=user, refresh=refresh, started=t_start, considered=considered,
                                   agent=agent, extra_used=attempt)
                blank = is_blank_result(qr.columns, qr.rows)
            if key is not None:
                if replay and blank:
                    _SQL.pop(key)                     # a day-long entry that now returns nothing is forgotten …
                if not replay or blank:
                    _SQL.put(key, (qr.database if qr.database in cats else db_name, qr.sql or safe),
                             short_ttl if blank else ttl)   # … and kept only briefly; a corrected statement for the day
            return qr
        except Exception as e:
            last_err = f"{e.__class__.__name__}: {e}"
            is_guard = isinstance(e, GuardError)
            log_generation(kind="guard" if is_guard else "error", database_line=db_line, database_used=db_name,
                           fallback=bool(raw) and not db_line, guard_error=last_err if is_guard else "",
                           db_error="" if is_guard else last_err, stop_reason=stop, raw_reply=raw or sql, **gen)
            if replay:
                _SQL.pop(key)             # a cached statement that no longer runs is dropped (schema change?)
                continue                  # and the question is generated afresh
            from . import typefix
            kinds = typefix.kinds_line(sql, cat)
            messages += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": prompt("sql_retry_error")
                             .replace("{error}", last_err)
                             .replace("{kinds}", f"Column kinds of what the query read:\n{kinds}\n" if kinds else "")}]
    return QueryResult(database=fallback.database, engine=fallback.engine, views=[], sql="", columns=[], rows=[],
                       error=last_err, databases_considered=considered, agent=agent)


def _repair_blank(qr: QueryResult, question: str, cats: dict[str, Catalog], cat: Catalog, raw: str,
                  messages: list[dict], system: str, chat: Any, shown_tables: list[str], *, scope: Scope, user: str,
                  refresh: bool, started: float, considered: list[str], agent: str,
                  extra_used: int = 0) -> QueryResult:
    """P2.2: a model-written statement returned nothing — look the recorded values up, correct a misspelling, or
    re-ask once (data/empty_retry.py). The repaired result carries its lookups ([D2]…), the filters it tried and,
    on a correction, which literal became which recorded value. Off for the `both` route and by setting."""
    from . import empty_retry
    if not empty_retry.is_allowed():
        return qr
    s = settings()

    def executor(for_cat: Catalog) -> empty_retry.Executor:
        return lambda stmt, tool: _run_guarded(stmt, for_cat, scope=scope, user=user, refresh=refresh, tool=tool,
                                               considered=considered, agent=agent, cats=cats)

    def ask(instruction: str, rows_sent: int) -> tuple[str, empty_retry.Executor]:
        convo = messages + [{"role": "assistant", "content": raw}, {"role": "user", "content": instruction}]
        reply = chat.chat(convo, system=system, max_tokens=int(s.get("llm.max_tokens_sql", 1536)), temperature=0,
                          purpose="sql", user=user, sent_rows=rows_sent)
        raw2 = re.sub(r"^```\w*|```$", "", reply.text.strip(), flags=re.M).strip()
        db2, sql2 = _split_database_line(raw2, cats, cat.database)
        log_generation(user=user, agent=agent, kind="retry", considered=considered, database_used=db2,
                       database_line=raw2.splitlines()[0] if raw2.upper().startswith("DATABASE:") else "",
                       stop_reason=reply.stop_reason or "", raw_reply=raw2)
        return sql2, executor(cats[db2])

    with trace.span("data.repair"):
        out = empty_retry.repair(question=question, cat=cat, scope_all=scope.all_factories, sql=qr.sql, result=qr,
                                 shown_tables=shown_tables, execute=executor(cat), ask=ask, started=started,
                                 is_blank=lambda r: is_blank_result(r.columns, r.rows), extra_used=extra_used)
    result = out.result
    result.lookups = out.lookups
    if result.corrections:
        # the corrected literal joins the query's filter line, where the verifier reads it (citations.py)
        for col, _asked, matched in result.corrections:
            result.params = {**result.params, col: matched}
    if not result.key_columns and result.filters:
        result.key_columns = [c for c in result.filters if c in result.columns]    # a table with no key: the filters
    if out.note:
        log_generation(user=user, agent=agent, kind="repair", considered=considered, database_used=result.database,
                       rows=len(result.rows), raw_reply=out.note)
    return result


def _qualify_bare_tables(sql: str, cat: Catalog) -> str:
    """`FROM GRNChild a JOIN GRNMaster b` -> `FROM dbo.GRNChild a JOIN dbo.GRNMaster b` when the catalog offers
    dbo.<name>: staff paste statements the way SSMS accepts them, the guard wants the schema said. Only the word
    after FROM/JOIN is touched; nothing else in the statement changes."""
    try:
        tree = parse_one(sql, read=cat.dialect)
        bare = {t.name for t in tree.find_all(exp.Table) if not t.db and cat.table(f"dbo.{t.name}")}
    except Exception:
        return sql
    for name in bare:
        sql = re.sub(rf"(\b(?:from|join)\s+)(?<![.\w]){re.escape(name)}\b", rf"\1dbo.{name}", sql, flags=re.I)
    return sql


def run_pasted(statement: str, cats: dict[str, Catalog], user: str = "", refresh: bool = False, *,
               scope: Scope) -> QueryResult:
    """A question that IS a SQL statement (P1) runs as written — under the same guard, scope and row cap as a
    model-written one — against the catalog that offers the most of the tables it names. On 8 Oct 2026 two pasted
    statements were scored on their words ("ORDER BY" -> the order agent), rewritten by the model and run on
    other databases. No catalog offers the tables -> an error result, which the orchestrator answers not-found."""
    s = settings()
    sql = statement.strip().rstrip(";").strip()
    ranked = sorted(((len(_views_in(_qualify_bare_tables(sql, c), c)), n) for n, c in cats.items()), reverse=True)
    considered = [n for k, n in ranked if k > 0]
    if not considered:
        return QueryResult(database="", engine="sqlserver", views=[], sql=sql, columns=[], rows=[], tool="pasted",
                           error="no catalog offers the tables the statement names", databases_considered=[])
    cat = cats[considered[0]]
    sql = _qualify_bare_tables(sql, cat)
    max_rows = int(s["data.max_rows"])
    if cat.dialect == "tsql" and re.match(r"^\s*select\b", sql, re.I) \
            and not re.match(r"^\s*select\s+(?:distinct\s+)?top\b", sql, re.I):
        # the guard inserts TOP itself unless the statement has ORDER BY, where it asks the model to add it;
        # a pasted statement has no model to ask, so the row cap is put on it here
        sql = re.sub(r"^\s*select\s+(distinct\s+)?", lambda m: f"SELECT {m.group(1) or ''}TOP ({max_rows}) ", sql,
                     count=1, flags=re.I)
    try:
        safe = _guard_for(sql, cat, max_rows, scope)
        sql_exec, views = rewrite_virtual(safe, cat, scope=scope)
        views += [n for n in _views_in(safe, cat) if not cat.view(n)]
        params = _extract_params(safe, cat)
        cols, rows, as_of = cached_run(cat.engine, sql_exec, params, conn_env=cat.connection_env, display_sql=safe,
                                       tool="pasted", user=user, refresh=refresh, scope_key=scope.key(),
                                       database=getattr(cat, "connection_database", None))
        log_generation(user=user, kind="pasted", considered=considered, database_used=cat.database, rows=len(rows),
                       raw_reply=sql)
        return QueryResult(database=cat.database, engine=cat.engine, views=views, sql=safe, sql_executed=sql_exec,
                           params=params, tool="pasted", columns=cols, rows=rows, key_columns=_keys_for(views, cat),
                           as_of=as_of, databases_considered=considered)
    except Exception as e:
        err = f"{e.__class__.__name__}: {e}"
        log_generation(user=user, kind="guard" if isinstance(e, GuardError) else "error", considered=considered,
                       database_used=cat.database, guard_error=err if isinstance(e, GuardError) else "",
                       db_error="" if isinstance(e, GuardError) else err, raw_reply=sql)
        return QueryResult(database=cat.database, engine=cat.engine, views=[], sql=sql, columns=[], rows=[],
                           tool="pasted", error=err[:300], databases_considered=considered)


def _with_agent_catalog(question: str, picked: dict[str, Catalog], cats: dict[str, Catalog], agent: Any,
                        qvec: Optional[list[float]]) -> dict[str, Catalog]:
    """Make sure the SQL model sees at least one of the agent's databases: when the ranking picked none,
    the agent's best database replaces the last pick. The ranking still decides otherwise, so a question
    the agent's keywords caught by mistake keeps the database its own words point to."""
    own = {n: cats[n] for n in getattr(agent, "catalogs", ()) if n in cats}
    if not own or set(own) & set(picked):
        return picked
    best = pick_catalogs(question, own, qvec, k=1)
    keep = list(picked.items())[:max(len(picked) - 1, 0)]
    return dict(keep + list(best.items()))


def answer_from_data(question: str, user: str = "", refresh: bool = False, *, scope: Scope) -> list[QueryResult]:
    """Try fixed tools across all catalogs; else pick the catalogs the question is about (db_router)
    and generate SQL, letting the model name the database among them. refresh=True skips the SQL
    result and question->SQL caches and reads live. Every statement is limited to the user's scope
    (data/virtual.py). Each result names the domain agent that answered (Phase K, domain_agents)."""
    from ..domain_agents import pick_agent
    with trace.span("data"):
        cats = load_catalogs()
        if not cats:
            return []
        if _STATEMENT.search(question):
            return [run_pasted(question, cats, user, refresh, scope=scope)]
        # Phase Q: a question that names only factories this user may not see is refused here, for every data
        # path. A fixed tool already did this (run_fixed_tool); model-written SQL just came back with no rows,
        # so on 11 Oct a TAL question asked with the Factory pill on BGL spent two SQL calls, a document search
        # and an answer call to say nothing. A mixed question ("TAL and RHL") still runs, filtered by the views.
        from .entities import question_entities
        named = question_entities(question).factories
        if named and not scope.all_factories and not any(scope.allows_factory(f) for f in named):
            return [QueryResult(database="", engine="sqlserver", views=[], sql="", columns=[], rows=[],
                                denied=True, error=f"outside the user's scope: factory {', '.join(named)}")]
        hit = match_fixed_tool(question, load_fixed_tools())
        if hit:
            tool, params = hit
            if tool["database"] in cats:
                result = run_fixed_tool(tool, params, cats, user, refresh, scope=scope)
                a = pick_agent(question, tool)
                result.agent = a.name if a else ""
                extra = run_companion(tool, result, cats, user, refresh, scope=scope)
                return [result] + ([extra] if extra is not None else [])
        with trace.span("data.embed"):
            qvec = _query_vector(question)
        with trace.span("data.pick"):
            picked = pick_catalogs(question, cats, qvec)
            # the catalogs are picked first so a keyword tie between two agents can be broken by them (Phase Q)
            agent = pick_agent(question, prefer_catalogs=list(picked))
            if agent is not None:
                picked = _with_agent_catalog(question, picked, cats, agent, qvec)
        r = generate_and_run(question, picked, user, refresh, scope=scope, qvec=qvec, agent=agent.name if agent else "")
        return [r, *r.lookups]       # the DISTINCT lookups a repair ran are sources too: [D2]… (data/empty_retry.py)
