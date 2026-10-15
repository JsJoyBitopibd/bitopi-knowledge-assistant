"""Self-correction after a model-written statement returns nothing (Phase P2.2).

On 8 Oct 2026 the SQL model filtered `SLolcation = 'mantainance store'` — the user's spelling — and the NULL sum
became "System doesn't have the data." A careful analyst would have looked at the stored values and tried again.
This module does that, within the owner's bounds (at most `data.empty_retry.max_model_calls` extra model calls,
only after an empty first try, `max_seconds` from the first statement):

(a) text literals: for each `Column = 'text'` the statement filtered on (single-table statements, up to
    `lookup_columns` of them, never a sensitive column, never an all-digit literal), one guarded
    `SELECT DISTINCT TOP (n) Column … WHERE <the other filters>` — no model call, its own [D#] card. An exact
    (case-insensitive) hit means the zero is real. A unique close match (difflib) means the spelling was wrong:
    the literal is replaced by the recorded value and the statement runs again, and the answer carries a cited
    note saying so. Nothing is substituted when two recorded values are equally close.
(b) identifier filters only (codes, ids) and other tables were listed for the question: one re-ask naming them.
(c) text literals with recorded values but no close match: one re-ask showing those values (≤ 50 in all,
    counted as rows sent to the provider).
(d) still nothing: the statement's filters and the recorded values found are returned for the not-found
    sentence (the user's own words) and the follow-up chips; `checked` tells the orchestrator the documents
    cannot rescue this question.

Every statement here passes the same guard, scope and row cap as the model's (tools._run_guarded); nothing is
regenerated from a parse tree — the lookup is built from the names sqlglot located, the correction is a textual
replacement of one quoted literal.

A provider failure during a re-ask (LLMError: quota, rate limit, timeout) is raised, never kept as a note: the
orchestrator turns it into the answer's error_kind and its `llm <kind>:` warning, which the eval leaves unscored.
"""
from __future__ import annotations

import contextvars
import difflib
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

import sqlglot
from sqlglot import exp

from ..config import prompt, settings
from ..llm.base import LLMError
from ..models import QueryResult
from .catalog import Catalog
from .sensitive import is_sensitive

_ALLOWED: contextvars.ContextVar[bool] = contextvars.ContextVar("ragbot_empty_retry", default=True)
_CALLS = threading.Semaphore(2)          # re-asks across users: the free tier's per-minute limit, 4 cores
_ID_COLUMN = re.compile(r"(?:id|code|no|number|ref|sl)$", re.I)


@contextmanager
def allowed(flag: bool) -> Iterator[None]:
    """The orchestrator turns the repair off for the `both` route (a document search already runs there)."""
    token = _ALLOWED.set(flag)
    try:
        yield
    finally:
        _ALLOWED.reset(token)


def is_allowed() -> bool:
    return _ALLOWED.get() and bool(settings().get("data.empty_retry.enabled", True))


@dataclass
class Filter:
    column: str
    literal: str
    conjunct: exp.Expression

    @property
    def is_identifier(self) -> bool:
        return self.literal.strip().isdigit() or bool(_ID_COLUMN.search(self.column))


@dataclass
class Shape:
    """What the statement filters on, and whether a lookup on it is well-defined (one table, no join)."""
    filters: list[Filter] = field(default_factory=list)
    table: str = ""                      # 'schema.name' when the statement reads exactly one table or view
    conjuncts: list[exp.Expression] = field(default_factory=list)

    @property
    def text_filters(self) -> list[Filter]:
        return [f for f in self.filters if not f.is_identifier and not is_sensitive(f.column)]

    @property
    def identifier_filters(self) -> list[Filter]:
        return [f for f in self.filters if f.is_identifier]


def shape_of(sql: str, dialect: str) -> Shape:
    """The equality filters of a statement and the one table it reads (sqlglot locates; nothing is rewritten)."""
    read = "tsql" if dialect == "tsql" else "mysql"
    try:
        tree = sqlglot.parse_one(sql, read=read)
    except Exception:
        return Shape()
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = [t for t in tree.find_all(exp.Table) if t.name.lower() not in ctes]
    single = ""
    if len(tables) == 1 and tree.find(exp.Join) is None:
        t = tables[0]
        single = ".".join(p for p in [t.db, t.name] if p)
    where = tree.find(exp.Where)
    out = Shape(table=single)
    if where is None:
        return out
    cond = where.this
    conjuncts = list(cond.flatten()) if isinstance(cond, exp.And) else [cond]
    out.conjuncts = conjuncts
    for c in conjuncts:
        if isinstance(c, exp.EQ) and isinstance(c.left, exp.Column) and isinstance(c.right, exp.Literal) \
                and c.right.is_string:
            out.filters.append(Filter(c.left.name, c.right.this, c))
    return out


def lookup_sql(shape: Shape, f: Filter, dialect: str, top: int) -> str:
    """`SELECT DISTINCT TOP (20) Column FROM table WHERE <the other filters>`: the recorded values of the
    column, among the rows the other filters keep (the stores that hold THIS item at THIS company)."""
    read = "tsql" if dialect == "tsql" else "mysql"
    others = [c.sql(dialect=read) for c in shape.conjuncts if c is not f.conjunct]
    where = f" WHERE {' AND '.join(others)}" if others else ""
    col = f"[{f.column}]" if dialect == "tsql" else f"`{f.column}`"
    if dialect == "tsql":
        return f"SELECT DISTINCT TOP ({top}) {col} FROM {shape.table}{where} ORDER BY {col}"
    return f"SELECT DISTINCT {col} FROM {shape.table}{where} ORDER BY {col} LIMIT {top}"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9&]+", " ", str(s).lower()).strip()


def _base(s: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", str(s)).strip() or str(s)


def closest(literal: str, candidates: list[str], cutoff: float) -> tuple[str, Optional[str]]:
    """('exact', value) when the literal is a recorded value (case apart); ('match', value) when exactly one
    recorded value — by its full text or its name without the bracketed company — is close enough and clearly
    closer than the next; ('none', None) otherwise. Two values equally close (the same store at two companies)
    are never guessed between."""
    lit = _norm(literal)
    if not lit:
        return "none", None
    for c in candidates:
        if _norm(c) == lit:
            return "exact", c
    scored: list[tuple[float, str]] = []
    for c in candidates:
        best = max(difflib.SequenceMatcher(None, lit, _norm(c)).ratio(),
                   difflib.SequenceMatcher(None, lit, _norm(_base(c))).ratio())
        if best >= cutoff:
            scored.append((best, c))
    if not scored:
        return "none", None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.02:
        return "none", None              # a tie: the model is asked with the candidates instead (step c)
    return "match", scored[0][1]


def substitute(sql: str, literal: str, value: str) -> str:
    """The first occurrence of the quoted literal replaced by the quoted recorded value; nothing else changes."""
    old = "'" + literal.replace("'", "''") + "'"
    new = "'" + value.replace("'", "''") + "'"
    i = sql.find(old)
    return sql if i < 0 else sql[:i] + new + sql[i + len(old):]


@dataclass
class Outcome:
    result: QueryResult
    lookups: list[QueryResult] = field(default_factory=list)
    extra_calls: int = 0
    note: str = ""                      # what happened, for the admin caption / chat.csv warnings


Executor = Callable[[str, str], QueryResult]


def repair(*, question: str, cat: Catalog, scope_all: bool, sql: str, result: QueryResult, shown_tables: list[str],
           execute: Executor, ask: Callable[[str, int], tuple[str, Executor]],
           started: float, is_blank: Callable[[QueryResult], bool], extra_used: int = 0) -> Outcome:
    """Try to turn a blank result into a rowed one within the bounds. `execute(sql, tool)` guards and runs a
    statement against the catalog the model first chose (raises on a guard or database error);
    `ask(instruction, rows_sent)` re-asks the model once with the conversation so far and returns the statement
    it answered with and an executor bound to the catalog it named (its own guard allow-list)."""
    s = settings()
    max_calls = int(s.get("data.empty_retry.max_model_calls", 2))
    max_seconds = float(s.get("data.empty_retry.max_seconds", 20))
    top = int(s.get("data.empty_retry.lookup_values", 20))
    max_cols = int(s.get("data.empty_retry.lookup_columns", 3))
    cutoff = float(s.get("data.values.fuzzy_cutoff", 0.85))
    shape = shape_of(sql, cat.dialect)
    out = Outcome(result)
    result.filters = {f.column: f.literal for f in shape.filters}
    result.checked = bool(shape.identifier_filters)

    def budget_left() -> bool:
        # Phase Q: the owner's cap is "at most 2 extra model calls" for the question, so a retry the SQL
        # generator already spent (data.sql_retries) counts against the same budget, not a second one.
        return out.extra_calls + extra_used < max_calls and (time.perf_counter() - started) < max_seconds

    candidates: dict[str, list[str]] = {}
    # (a) text literals: look the recorded values up, correct a misspelling without a model call
    if shape.table and shape.text_filters:
        for f in shape.text_filters[:max_cols]:
            try:
                look = execute(lookup_sql(shape, f, cat.dialect, top), "lookup")
            except Exception:
                continue                  # e.g. a big table whose only WHERE was this literal: the guard says no
            vals = [str(r[0]) for r in look.rows if r and r[0] is not None]
            out.lookups.append(look)
            result.checked = True
            if not vals:
                continue
            kind, value = closest(f.literal, vals, cutoff)
            if kind == "exact":
                out.note = f"'{f.literal}' is a recorded {f.column}: the empty result stands"
                continue
            if kind == "match" and value is not None:
                fixed = substitute(sql, f.literal, value)
                try:
                    corrected = execute(fixed, "generated")
                except Exception:
                    candidates[f.column] = vals
                    continue
                if not is_blank(corrected):
                    corrected.filters = dict(result.filters, **{f.column: value})
                    corrected.corrections = [[f.column, f.literal, value]]
                    corrected.checked = True
                    out.result = corrected
                    out.note = f"corrected {f.column} '{f.literal}' -> '{value}' from the recorded values"
                    return out
                candidates[f.column] = vals
            else:
                candidates[f.column] = vals
    # (c) recorded values exist but none is close: show them to the model once
    if candidates and budget_left():
        lines, n = [], 0
        for col, vals in candidates.items():
            keep = vals[: max(1, 50 - n)]
            n += len(keep)
            lines.append(f"{col}: " + ", ".join(f"'{v}'" for v in keep))
            if n >= 50:
                break
        try:
            with _CALLS:
                sql2, execute2 = ask(prompt("sql_retry_empty").replace("{lookups}", "\n".join(lines)), n)
            out.extra_calls += 1
            again = execute2(sql2, "generated")
            if not is_blank(again):
                again.checked = True
                out.result = again
                out.note = "answered on the second try with the recorded values shown"
                return out
        except LLMError:
            raise                         # a provider failure (quota, rate, timeout) is the answer's, not a note
        except Exception as e:
            out.note = f"informed retry failed: {e.__class__.__name__}"
    # (b) identifiers only, and other tables were listed: ask once for the right table
    elif not shape.text_filters and shape.identifier_filters and budget_left():
        others = [t for t in shown_tables if t.lower() != shape.table.lower()]
        if others:
            try:
                with _CALLS:
                    sql2, execute2 = ask(prompt("sql_retry_table").replace("{tables}", "\n".join(f"- {t}" for t in others)), 0)
                out.extra_calls += 1
                again = execute2(sql2, "generated")
                if not is_blank(again):
                    again.checked = True
                    out.result = again
                    out.note = "answered on the second try against another listed table"
                    return out
            except LLMError:
                raise
            except Exception as e:
                out.note = f"table retry failed: {e.__class__.__name__}"
    # (d) nothing: hand the user's filters and the recorded values to the not-found reply
    result.candidates = [v for vals in candidates.values() for v in vals][:10]
    return out
