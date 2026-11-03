"""How far the data on this server goes (Phase V3).

The SQL Server the app reads is a copy restored from backups, database by database (checked 2026-11-03 in
msdb.dbo.restorehistory: Production restored on 30 Oct 2026, so its newest rows are from 29 Oct). A question about
"yesterday" is then empty by construction, and until v1.18.0 the reply was a generic "System doesn't have the data."
with a model-written guess about where the data lives. Now an empty dated answer says what was checked, for which
day, and the newest record this server holds:

    "I checked Production (PPM & PPC) on 2 Nov 2026 and found no records; the newest record on this server is
     from 29 Oct 2026, so ask about that day or earlier."

newest() reads MAX(<date column>) of a curated view, only for the columns the view declares in
`freshness_columns:` (a MAX over an unindexed column of a multi-million-row view is not free), through the same
virtual-view expansion, scope filter and result cache as every other statement (logged to sql.csv as
tool "freshness"; kept for data.freshness_ttl_seconds, default an hour; Refresh data bypasses it). A user limited
to TAL is told TAL's newest record. Any failure returns None: the sentence is left out, the answer is not.

date_window() reads the date range a model-written statement filtered on (`col >= '2026-11-02' AND col <
'2026-11-03'`), so an empty result over one view can be reported the same way. No model call anywhere here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

from ..auth.models import Scope
from ..config import settings


def as_date(value: Any) -> Optional[date]:
    """A date from a DB value: datetime, date or an ISO string ('2026-10-29', '2026-10-29 18:34:01'); else None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def show(d: date) -> str:
    return f"{d.day} {d:%b %Y}"           # CLAUDE.md: dates display as 12 Oct 2026


def newest(database: str, view: str, column: str = "", *, scope: Scope, user: str = "", refresh: bool = False,
           cats: Optional[dict] = None) -> Optional[date]:
    """The newest value of `column` (default: the view's first `freshness_columns` entry) in `view` as `scope` may
    see it, or None when the view declares no such column or the read fails."""
    try:
        from .catalog import load_catalogs
        cats = load_catalogs() if cats is None else cats
        cat = cats.get(database)
        v = cat.view(view) if cat is not None else None
        if v is None or not v.freshness_columns:
            return None
        col = next((c for c in v.freshness_columns if c.lower() == (column or v.freshness_columns[0]).lower()), None)
        if col is None:
            return None
        from .cache import cached_run
        from .virtual import rewrite_virtual
        quoted = f"[{col}]" if cat.dialect == "tsql" else f"`{col}`"
        sql = f"SELECT MAX({quoted}) AS Newest FROM {v.name}"
        sql_exec, _ = rewrite_virtual(sql, cat, scope=scope)
        _cols, rows, _as_of = cached_run(cat.engine, sql_exec, {}, conn_env=cat.connection_env, display_sql=sql,
                                         tool="freshness", user=user, refresh=refresh, scope_key=scope.key(),
                                         database=getattr(cat, "connection_database", None),
                                         ttl=int(settings().get("data.freshness_ttl_seconds", 3600)))
        return as_date(rows[0][0]) if rows and rows[0] else None
    except Exception:
        return None


def view_with_freshness(sql: str, cat: Any, column: str = "") -> Optional[tuple[str, str]]:
    """(view, column) of the first curated view the statement reads that declares `freshness_columns:` — `column`
    when the view declares it, else the view's first one; None when no view read declares any."""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="tsql" if cat.dialect == "tsql" else "mysql")
        names = [f"{t.db}.{t.name}" for t in tree.find_all(exp.Table) if t.db]
    except Exception:
        return None
    for n in names:
        v = cat.view(n)
        if v is None or not v.freshness_columns:
            continue
        want = (column or "").lower()
        col = next((c for c in v.freshness_columns if c.lower() == want), None) if want else None
        return v.name, col or v.freshness_columns[0]
    return None


@dataclass
class DateWindow:
    table: str                 # the one table or view the statement reads ('rag.vw_FabricRoll')
    column: str                # the column the range is on
    start: date
    end: Optional[date]        # exclusive; None when only a lower bound was given

    @property
    def label(self) -> str:
        """'on 2 Nov 2026', 'from 1 Nov 2026 to 2 Nov 2026', 'from 1 Nov 2026' — read after "I checked X"."""
        if self.end is None:
            return f"from {show(self.start)}"
        last = self.end - timedelta(days=1)
        return f"on {show(self.start)}" if last <= self.start else f"from {show(self.start)} to {show(last)}"


def date_window(sql: str, dialect: str) -> Optional[DateWindow]:
    """The date range a statement over ONE table or view filters on — `col >= 'd1' AND col < 'd2'`, `col BETWEEN
    'd1' AND 'd2'`, `col = 'd'` with literal dates — or None (a join, no literal date range, ranges on two columns)."""
    from sqlglot import exp
    from .empty_retry import shape_of
    shape = shape_of(sql, dialect)
    if not shape.table or not shape.conjuncts:
        return None
    lo: dict[str, date] = {}
    hi: dict[str, date] = {}

    def lit(node: exp.Expression) -> Optional[date]:
        return as_date(node.this) if isinstance(node, exp.Literal) and node.is_string else None

    for c in shape.conjuncts:
        if isinstance(c, exp.Between) and isinstance(c.this, exp.Column):
            a, b = lit(c.args.get("low")), lit(c.args.get("high"))
            if a and b:
                lo[c.this.name], hi[c.this.name] = a, b + timedelta(days=1)
            continue
        if not isinstance(c, (exp.GTE, exp.GT, exp.LT, exp.LTE, exp.EQ)):
            continue
        col, d, op = None, None, type(c)
        if isinstance(c.left, exp.Column) and lit(c.right):
            col, d = c.left.name, lit(c.right)
        elif isinstance(c.right, exp.Column) and lit(c.left):      # '2026-11-02' <= col: flip the operator
            col, d = c.right.name, lit(c.left)
            op = {exp.GTE: exp.LTE, exp.GT: exp.LT, exp.LT: exp.GT, exp.LTE: exp.GTE}.get(op, op)
        if col is None or d is None:
            continue
        if op is exp.EQ:
            lo[col], hi[col] = d, d + timedelta(days=1)
        elif op is exp.GTE:
            lo[col] = d
        elif op is exp.GT:
            lo[col] = d + timedelta(days=1)
        elif op is exp.LT:
            hi[col] = d
        elif op is exp.LTE:
            hi[col] = d + timedelta(days=1)
    cols = set(lo) | set(hi)
    if len(cols) != 1:
        return None
    col = cols.pop()
    if col not in lo:
        return None
    return DateWindow(shape.table, col, lo[col], hi.get(col))
