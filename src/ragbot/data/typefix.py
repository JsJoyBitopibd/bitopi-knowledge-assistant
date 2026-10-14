"""Quote a bare number compared to a text column, before the statement runs (Phase Q).

On 2026-10-11 the SQL model wrote, correctly in every other way:

    SELECT TOP (1) Style FROM rag.vw_PlanDay
    WHERE Factory = 'TAL' AND Line = 2102 AND PlanDate >= '2026-10-11' AND PlanDate < '2026-10-12'

`Line` holds `dbo.Workspace.WorkspaceName` (nvarchar): SQL Server converted the COLUMN to int and failed on
'Remi Holdings Ltd.' with 22018/245. The model's second attempt moved to another database and hit the same
thing on 'Pilot 1'. Two provider calls, a 10 s document search and an answer call later, the user was told
"System doesn't have the data." about a line that was running a style all day.

This module fixes that deterministically, with no model call:

- `fix()` locates `column <op> 2102`, `column IN (…)` and `column BETWEEN a AND b` with sqlglot, resolves the
  column to a view or table through the statement's aliases, asks the catalog for its kind, and — when that
  kind is text — adds quotes by replacing the text OUTSIDE string literals (the technique of
  connectors._positional). sqlglot locates; nothing is regenerated from the parse tree, so the statement the
  user is shown stays byte-identical to the model's apart from the quotes (virtual.py's rule).
- mode "known" runs before every generated statement; mode "aggressive" also quotes literals on columns whose
  kind nothing records, and runs once after a conversion error. It is safe in T-SQL: a quoted numeric literal
  compared to a numeric column converts the literal, not the column, so an int column still seeks.
- plain `'…'` quotes, never `N'…'`: an nvarchar literal against a varchar column converts the column side and
  defeats the index (connectors._bind_varchar, measured 45 s -> 1.3 s on 2026-10-09).

A pasted statement is never touched: it runs as written (docs/DATA_ACCESS.md).
"""
from __future__ import annotations

import re
from typing import Any, Optional

import sqlglot
from sqlglot import exp

# 22018 = conversion failed, 245 = text -> int, 8114 = varchar -> numeric, 241 = text -> datetime
_CONVERSION = re.compile(r"\b(?:22018|245|8114|241)\b|conversion failed|error converting data type", re.I)
_INT = re.compile(r"^\d+$")


def is_conversion_error(message: str) -> bool:
    """Whether a database error says a value could not be converted — the error this module repairs."""
    return bool(_CONVERSION.search(message or ""))


def _sources(select: exp.Select, ctes: set[str]) -> dict[str, str]:
    """alias (and bare name) -> canonical object name for one SELECT's FROM and JOINs. A CTE or a derived
    table maps to "" : its columns cannot be resolved to a catalog object, so nothing is changed for them."""
    out: dict[str, str] = {}
    for t in select.find_all(exp.Table):
        if t.find_ancestor(exp.Select) is not select:
            continue
        name = ".".join(p for p in [t.catalog, t.db, t.name] if p)
        canonical = "" if t.name.lower() in ctes else name
        out[t.alias_or_name.lower()] = canonical
        out[t.name.lower()] = canonical
    for sub in select.find_all(exp.Subquery):
        if sub.find_ancestor(exp.Select) is select and sub.alias:
            out[sub.alias.lower()] = ""
    return out


def _kind(column: exp.Column, select: Optional[exp.Select], ctes: set[str], cat: Any,
          siblings: Optional[dict[str, Any]] = None) -> Optional[str]:
    """The kind of the column this comparison reads, or None when it cannot be resolved with certainty."""
    if select is None:
        return None
    sources = _sources(select, ctes)
    name = column.name
    if column.table:
        obj = sources.get(column.table.lower())
        return cat.column_kind(obj, name) if obj else None
    objs = [o for o in dict.fromkeys(sources.values()) if o]
    if len(objs) != len(set(sources.values())):      # a CTE or derived table is among the sources
        return None
    kinds = {cat.column_kind(o, name) for o in objs}
    kinds.discard(None)
    if len(objs) == 1:
        return cat.column_kind(objs[0], name)
    return kinds.pop() if len(kinds) == 1 else None   # several sources must agree


def _quote_outside_strings(sql: str, pattern: re.Pattern, repl) -> tuple[str, int]:
    """Apply a substitution only outside single-quoted literals (connectors._positional's technique)."""
    parts = sql.split("'")
    n = 0
    for i in range(0, len(parts), 2):
        parts[i], k = pattern.subn(repl, parts[i])
        n += k
    return "'".join(parts), n


def fix(sql: str, cat: Any, *, mode: str = "known",
        siblings: Optional[dict[str, Any]] = None) -> tuple[str, list[str]]:
    """Return (sql, notes). `notes` is empty when nothing was changed."""
    try:
        tree = sqlglot.parse_one(sql, read="tsql" if getattr(cat, "dialect", "tsql") == "tsql" else "mysql")
    except Exception:
        return sql, []
    if tree is None:
        return sql, []
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    wanted: list[tuple[str, str]] = []          # (column name as written, literal)

    def want(column: exp.Column, literal: exp.Literal) -> None:
        if not literal.is_number or not _INT.match(str(literal.this)):
            return
        kind = _kind(column, column.find_ancestor(exp.Select), ctes, cat, siblings)
        if kind == "text" or (mode == "aggressive" and kind is None):
            wanted.append((column.sql(dialect="tsql"), str(literal.this)))

    for node in tree.find_all(exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        left, right = node.left, node.right
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            want(left, right)
        elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            want(right, left)
    for node in tree.find_all(exp.In):
        if isinstance(node.this, exp.Column):
            for e in node.expressions:
                if isinstance(e, exp.Literal):
                    want(node.this, e)
    for node in tree.find_all(exp.Between):
        if isinstance(node.this, exp.Column):
            for side in (node.args.get("low"), node.args.get("high")):
                if isinstance(side, exp.Literal):
                    want(node.this, side)

    out, notes = sql, []
    for written, value in dict.fromkeys(wanted):
        # the column as the statement writes it (alias and brackets kept), then the operator, then the number
        col = r"\[?".join(re.escape(p) for p in written.split("."))
        col = col.replace(r"\[?", r"\s*\.\s*\[?", 1) if "." in written else col
        anchor = rf"(?P<lhs>\[?{col}\]?)\s*(?P<op>=|<>|!=|>=|<=|>|<)\s*(?P<lit>{re.escape(value)})(?![\d.\w])"
        new, n = _quote_outside_strings(out, re.compile(anchor, re.I), lambda m: f"{m['lhs']} {m['op']} '{m['lit']}'")
        if not n:   # IN (…) / BETWEEN: quote the number wherever it sits beside this column's clause
            anchor2 = rf"(?<![\w'.]){re.escape(value)}(?![\w'.])"
            new, n = _quote_outside_strings(out, re.compile(anchor2), lambda m: f"'{m.group(0)}'")
        if n:
            out, _ = new, notes.append(f"{written} = {value} -> {written} = '{value}'")
    if not notes:
        return sql, []
    try:                       # never hand the guard something that no longer parses
        sqlglot.parse_one(out, read="tsql" if getattr(cat, "dialect", "tsql") == "tsql" else "mysql")
    except Exception:
        return sql, []
    return out, notes


def kinds_line(sql: str, cat: Any, limit: int = 12) -> str:
    """"rag.vw_PlanDay: Line (text), PlanDate (date), DayTarget (int)" — what the model is told after a
    failure, so its correction compares the right things."""
    try:
        tree = sqlglot.parse_one(sql, read="tsql" if getattr(cat, "dialect", "tsql") == "tsql" else "mysql")
    except Exception:
        return ""
    if tree is None:
        return ""
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out: list[str] = []
    for t in tree.find_all(exp.Table):
        name = ".".join(p for p in [t.db, t.name] if p)
        if t.name.lower() in ctes or not name:
            continue
        kinds = cat.column_kinds_of(name)
        if not kinds:
            continue
        shown = [f"{c} ({k})" for c, k in list(kinds.items())[:limit]]
        line = f"{name}: {', '.join(shown)}"
        if line not in out:
            out.append(line)
    return "\n".join(out)
