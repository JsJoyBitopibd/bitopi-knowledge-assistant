"""What a model-written query looked for, said in one sentence above its table (2026-10-26).

A templated list opened with "Found 100 matching rows [D1]:" and nothing else, so nobody could tell what the
question had been taken to mean: "Which Shipments are at risk?" ran `WHERE ShipDate < @today AND InvoicedQty IS
NULL ORDER BY ShipDate` under TOP (100), and the answer said neither that "at risk" had become "past its ship date
and not invoiced" nor that 100 was the query's cap rather than the total. describe() states the outer SELECT's
WHERE, GROUP BY and ORDER BY and whether the row cap was reached. It is written by Python from the parsed
statement the [D#] card shows — never by a model — so it can only say what the SQL did.

The literal values it states come back as `params`: the caller adds them to the result, as the one-value answers'
filters are (templated.filter_params), so citations.verify() licenses them and the card's Filter line shows them.
A condition it cannot put in words is not paraphrased: the sentence says other conditions apply and points at the
query on the card.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlglot import exp

from ..config import settings
from ..data.empty_retry import MAX_IN_VALUES, _and_terms, _parse
from ..models import QueryResult

MAX_CONDITIONS = 5       # more than this and the sentence stops being short: the rest are "other conditions"
OTHER = "other conditions shown under Show query"

# the date parameters data/tools._extract_params resolves, as a reader would say them
_PARAM_WORDS = {"today": "today", "tomorrow": "tomorrow", "thisMon": "this Monday", "nextMon": "next Monday",
                "nextMon2": "the Monday after next", "monthStart": "the first of this month"}
_UNITS = {"day": "day", "dd": "day", "d": "day", "week": "week", "wk": "week", "ww": "week",
          "month": "month", "mm": "month", "m": "month", "year": "year", "yy": "year", "yyyy": "year"}
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?$")
_CMP = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)
_FLIP = {exp.LT: exp.GT, exp.GT: exp.LT, exp.LTE: exp.GTE, exp.GTE: exp.LTE, exp.EQ: exp.EQ, exp.NEQ: exp.NEQ}


@dataclass
class Description:
    where: list[str] = field(default_factory=list)     # one phrase per condition: "ShipDate is before today (…)"
    other: bool = False                                 # conditions the sentence does not state
    group: list[str] = field(default_factory=list)     # GROUP BY columns: one row per …
    order: str = ""                                     # "sorted by ShipDate (earliest first), then ExportOrderID"
    capped: bool = False                                # the rows reached the query's TOP/LIMIT or data.max_rows
    params: dict[str, str] = field(default_factory=dict)  # column -> the literal values stated, for verify()

    def __bool__(self) -> bool:
        return bool(self.where or self.other or self.group or self.order or self.capped)

    def conditions(self) -> str:
        """'A, B and C' (plus the pointer to the query when some are not stated); '' when there are none."""
        parts = self.where + ([OTHER] if self.other else [])
        return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1] if parts else ""


@dataclass
class _Val:
    text: str
    is_date: bool = False


def _fmt(v: Any) -> str:
    from .templated import fmt              # lazy: templated imports this module
    return fmt(v)


def _strip(node: exp.Expression) -> exp.Expression:
    # TsOrDsToDate: sqlglot's reading of T-SQL YEAR(ShipDate) wraps the column in it
    while isinstance(node, (exp.Paren, exp.Cast, exp.TryCast, exp.TsOrDsToDate)):
        node = node.this
    return node


def _column(node: exp.Expression) -> Optional[str]:
    """The column a condition is about; CAST(ShipDate AS date) is still ShipDate."""
    node = _strip(node)
    return node.name if isinstance(node, exp.Column) and node.name else None


def _part(node: exp.Expression) -> Optional[tuple[str, str]]:
    """YEAR(ShipDate) -> ('ShipDate', 'year'); MONTH and DAY alike. None for anything else."""
    node = _strip(node)
    for cls, word in ((exp.Year, "year"), (exp.Month, "month"), (exp.Day, "day")):
        if isinstance(node, cls):
            col = _column(node.this)
            return (col, word) if col else None
    return None


def _kind(v: Any) -> str:
    if isinstance(v, (date, datetime)):
        return "date"
    if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
        return "number"
    return "text"


def _shift(d: date, n: int, unit: str) -> date:
    if unit == "day":
        return d + timedelta(days=n)
    if unit == "week":
        return d + timedelta(weeks=n)
    y, m = divmod(d.month - 1 + n * (12 if unit == "year" else 1), 12)
    y, m = d.year + y, m + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _int(node: exp.Expression) -> Optional[int]:
    node = _strip(node)
    if isinstance(node, exp.Neg):
        n = _int(node.this)
        return None if n is None else -n
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return int(str(node.this))
        except ValueError:
            return None
    return None


class _Ctx:
    def __init__(self, r: QueryResult):
        self.r = r
        self.kinds: dict[str, str] = {}
        for i, col in enumerate(r.columns):
            v = next((row[i] for row in r.rows if i < len(row) and row[i] is not None), None)
            if v is not None:
                self.kinds[col.lower()] = _kind(v)
        self.licensed: dict[str, list[str]] = {}

    def kind(self, col: str) -> str:
        return self.kinds.get(col.lower(), "")

    def _license(self, col: str, text: str) -> None:
        vals = self.licensed.setdefault(col, [])
        if text not in vals:
            vals.append(text)

    def _date(self, node: exp.Expression) -> Optional[tuple[date, str]]:
        """A date the statement names: one of the known parameters or the server's today."""
        node = _strip(node)
        if isinstance(node, (exp.Parameter, exp.Placeholder)):
            name = node.name
            v = self.r.params.get(name)
            if isinstance(v, datetime):
                v = v.date()
            return (v, _PARAM_WORDS.get(name, name)) if isinstance(v, date) else None
        if isinstance(node, (exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentDatetime)):
            return date.today(), "today"
        return None

    def value(self, node: exp.Expression, col: str) -> Optional[_Val]:
        """The text a value is stated with, or None when it cannot be stated exactly. A literal, or a date Python
        computes (DATEADD on a known date), is licensed under `col`; a parameter is already in the result's params."""
        node = _strip_paren(node)
        if isinstance(node, (exp.CurrentTimestamp, exp.CurrentDatetime)):
            return _Val("now", True)                # GETDATE() keeps the time of day; CAST(… AS date) is today
        node = _strip(node)
        if isinstance(node, (exp.CurrentTimestamp, exp.CurrentDate, exp.CurrentDatetime)):
            return _Val("today", True)
        known = self._date(node)
        if known is not None:                       # a parameter: its date is in the result's params already
            d, words = known
            return _Val(f"{words} ({_fmt(d)})", True)
        if isinstance(node, exp.DateAdd):
            base, n = self._date(node.this), _int(node.expression)
            unit = _UNITS.get(str(node.unit.name if node.unit is not None else "day").lower())
            if base is None or n is None or unit is None:
                return None
            text = _fmt(_shift(base[0], n, unit))
            self._license(col, text)
            return _Val(text, True)
        n = _int(node)
        if n is not None and isinstance(node, exp.Neg):
            self._license(col, str(n))
            return _Val(str(n))
        if isinstance(node, (exp.Literal, exp.National)):
            text = str(node.this).strip()
            if not text:
                return None
            self._license(col, text)
            return _Val(text, bool(_ISO_DATE.match(text)))
        return None


def _cmp_words(kind: type, is_date: bool) -> str:
    return {exp.EQ: "is", exp.NEQ: "is not",
            exp.LT: "is before" if is_date else "is below",
            exp.LTE: "is on or before" if is_date else "is at most",
            exp.GT: "is after" if is_date else "is above",
            exp.GTE: "is on or after" if is_date else "is at least"}[kind]


def _either(texts: list[str]) -> str:
    return texts[0] if len(texts) == 1 else ", ".join(texts[:-1]) + " or " + texts[-1]


def _like(col: str, pattern: str, negate: bool) -> str:
    core = pattern.strip("%")
    if not core or "%" in core or "_" in core or "[" in core:
        return f"{col} {'does not match' if negate else 'matches'} the pattern {pattern}"
    starts, ends = pattern.startswith("%"), pattern.endswith("%")
    verb = ("contain" if starts and ends else "start with" if ends else "end with" if starts else "")
    if not verb:
        return f"{col} is {'not ' if negate else ''}{core}"
    return f"{col} {'does not ' + verb if negate else verb + 's'} {core}"


def _strip_paren(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _term(t: exp.Expression, ctx: _Ctx) -> Optional[str]:
    """One WHERE condition in words, or None when it cannot be stated exactly."""
    t = _strip_paren(t)
    negate = isinstance(t, exp.Not)
    if negate:
        t = _strip_paren(t.this)
    if isinstance(t, exp.Is) and isinstance(t.expression, exp.Null):
        col = _column(t.this)
        return f"{col} is {'not ' if negate else ''}empty" if col else None
    if isinstance(t, exp.In) and not any(t.args.get(k) for k in ("query", "unnest", "field")):
        col = _column(t.this)
        if not col or not t.expressions:
            return None
        if len(t.expressions) > MAX_IN_VALUES:
            return f"{col} is {'not ' if negate else ''}one of the values listed in the query"
        vals = [ctx.value(v, col) for v in t.expressions]
        if any(v is None for v in vals):
            return None
        return f"{col} is {'not ' if negate else ''}{_either([v.text for v in vals])}"
    if isinstance(t, exp.Like):
        col, pattern = _column(t.this), _strip(t.expression)
        if not col or not isinstance(pattern, (exp.Literal, exp.National)) or not str(pattern.this).strip():
            return None
        ctx._license(col, str(pattern.this).strip("%"))
        return _like(col, str(pattern.this), negate)
    if negate:
        return None                              # NOT (a < b) and the like: not stated, never inverted
    if isinstance(t, _CMP):
        kind, side, other = type(t), t.left, t.right
        if _column(side) is None and _part(side) is None:
            kind, side, other = _FLIP[kind], t.right, t.left
        part = _part(side) if _column(side) is None else None
        col = part[0] if part else _column(side)
        if col is None:
            return None
        v = ctx.value(other, col)
        if v is None:
            return None
        if part:                                 # YEAR(ShipDate) = 2026: a number, so "above", not "after"
            return f"the {part[1]} of {col} {_cmp_words(kind, False)} {v.text}"
        return f"{col} {_cmp_words(kind, v.is_date or ctx.kind(col) == 'date')} {v.text}"
    if isinstance(t, exp.Between):
        col = _column(t.this)
        lo = ctx.value(t.args["low"], col) if col else None
        hi = ctx.value(t.args["high"], col) if col else None
        if lo is None or hi is None:
            return None
        if lo.is_date or hi.is_date or ctx.kind(col) == "date":
            return f"{col} is from {lo.text} to {hi.text}"
        return f"{col} is between {lo.text} and {hi.text}"
    if isinstance(t, exp.Or):
        items = [_strip_paren(x) for x in t.flatten()]
        cols = {(_column(x.left) or "").lower() for x in items if isinstance(x, exp.EQ)}
        if all(isinstance(x, exp.EQ) for x in items) and len(cols) == 1 and "" not in cols \
                and len(items) <= MAX_IN_VALUES:     # Factory = 'TAL' OR Factory = 'RHL': "Factory is TAL or RHL"
            col = _column(items[0].left)
            vals = [ctx.value(x.right, col) for x in items]
            if all(v is not None for v in vals):
                return f"{col} is {_either([v.text for v in vals])}"
        parts = [_term(x, ctx) for x in items]
        if any(p is None for p in parts):
            return None
        return "either " + _either(parts)
    return None


def _order(tree: exp.Select, ctx: _Ctx) -> str:
    order = tree.args.get("order")
    if order is None:
        return ""
    words = {"date": ("earliest first", "latest first"), "number": ("lowest first", "highest first"),
             "text": ("A to Z", "Z to A")}
    out: list[str] = []
    for i, o in enumerate(order.expressions):
        col = _column(o.this)
        if col is None:
            break                                # ORDER BY an expression: say no more than the columns before it
        desc = bool(o.args.get("desc"))
        pair = words.get(ctx.kind(col), ("ascending", "descending"))
        if i == 0:
            out.append(f"sorted by {col} ({pair[desc]})")
        else:
            out.append(f"then {col}" + (f" ({pair[1]})" if desc else ""))
    return ", ".join(out)


def _top(tree: exp.Select) -> Optional[int]:
    limit = tree.args.get("limit")
    return _int(limit.expression) if isinstance(limit, exp.Limit) and limit.expression is not None else None


def describe(r: QueryResult) -> Optional[Description]:
    """The description of a model-written query's result, or None (a fixed tool, whose `answer_list:` lead is
    hand-written; an error; a statement that does not parse or is not one SELECT; nothing worth saying)."""
    if r.tool != "generated" or r.error or not r.sql:
        return None
    tree = _parse(r.sql, "mysql" if r.engine == "mysql" else "tsql")
    if not isinstance(tree, exp.Select):
        return None
    ctx, d = _Ctx(r), Description()
    where = tree.args.get("where")
    if where is not None:
        for term in _and_terms(where.this):
            phrase = _term(term, ctx)
            if phrase is None:
                d.other = True
            elif len(d.where) < MAX_CONDITIONS:
                d.where.append(phrase)
            else:
                d.other = True
    # what narrows the rows outside the outer WHERE: HAVING, a CTE or subquery's own WHERE, a JOIN condition
    # on a value. A plain key join (a.ID = b.ID) is how the tables meet, not a filter, and is not mentioned.
    if tree.args.get("having") is not None or any(s is not tree for s in tree.find_all(exp.Select)):
        d.other = True
    for j in tree.args.get("joins") or []:
        on = j.args.get("on")
        if on is not None and on.find(exp.Literal, exp.National, exp.Parameter, exp.Placeholder, exp.Null) is not None:
            d.other = True
    group = tree.args.get("group")
    if group is not None:
        cols = [_column(e) for e in group.expressions]
        d.group = [c for c in cols if c] if all(cols) else []
    d.order = _order(tree, ctx)
    top, n = _top(tree), len(r.rows)
    d.capped = n > 1 and ((top is not None and n >= top) or n >= int(settings().get("data.max_rows", 200)))
    d.params = {col: ", ".join(vals) for col, vals in ctx.licensed.items()}
    return d if d else None


def described_params(r: QueryResult) -> dict[str, str]:
    """The literal values describe() would state, {column: "v1, v2"}: the orchestrator adds them to a copy of the
    result's params before templating, so verify() licenses them and the card's Filter line shows them."""
    d = describe(r)
    return d.params if d else {}


def lead(d: Description, n: int, marker: str) -> str:
    """'Found 100 rows where ShipDate is before today (2026-10-26) and InvoicedQty is empty, sorted by ShipDate
    (earliest first), then ExportOrderID [D1]. The query returns at most 100 rows, so there may be more.'"""
    text = f"Found {n} row" + ("" if n == 1 else "s")
    if d.group:
        text += " (one per " + (d.group[0] if len(d.group) == 1 else ", ".join(d.group[:-1]) + " and " + d.group[-1]) + ")"
    cond = d.conditions()
    if cond:
        text += f" where {cond}"
    if d.order:
        text += f", {d.order}"
    text += f" [{marker}]."
    if d.capped:
        text += f" The query returns at most {n} rows, so there may be more."
    return text
