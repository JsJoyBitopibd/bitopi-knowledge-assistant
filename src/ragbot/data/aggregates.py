"""Pre-computed aggregates (Phase C6, docs/ROADMAP.md): heavy results copied into a local SQLite file.

Some curated queries cannot run fast on the server without DDL we may not do — e.g. PCD history reads
dbo.ExportOrderBack (4.2M rows, no index on ExportOrderID; ~10 s and timeouts per question, measured
2026-09-27). scripts/refresh_aggregates.py copies such a result once (nightly, `--at 02:00`) into
data/index/aggregates.db, indexed for the lookups the fixed tools make; a fixed tool that names an
`aggregate:` then answers from the copy in milliseconds, and its [D#] reference shows the copy's
as-of time. "Refresh data" (refresh=True) or a missing copy falls back to the live query.

Write barrier unchanged: the server side is read by connectors.stream_sqlserver (read-only, rolled
back) after guard.assert_read_only; only the local file is ever written.
"""
from __future__ import annotations

import re
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml

from ..config import settings
from .connectors import _positional

META = "_aggregate_meta"


def db_path() -> Path:
    return settings().path("index_dir") / "aggregates.db"


def load_specs(path: Optional[Path] = None) -> list[dict[str, Any]]:
    path = path or settings().path("fixed_tools").parent / "aggregates.yaml"
    return (yaml.safe_load(path.read_text(encoding="utf-8")) or []) if path.exists() else []


def _cell(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def write(name: str, batches, indexes: list[str], views: list[str], path: Optional[Path] = None) -> int:
    """Replace table `name` with the streamed rows, atomically: build `<name>__new`, then swap inside one
    transaction, so a reader sees the old copy or the new one, never half of one. Returns the row count."""
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    t0, total, new = time.perf_counter(), 0, f"{name}__new"
    # isolation_level=None: transactions are explicit. (Python's implicit mode does not cover DDL, so the
    # DROP + RENAME swap would not be atomic.) WAL lets the app keep reading the old copy meanwhile.
    con = sqlite3.connect(path, isolation_level=None)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(f"CREATE TABLE IF NOT EXISTS {META} (name TEXT PRIMARY KEY, as_of TEXT, rows INTEGER, "
                    "seconds REAL, views TEXT)")
        con.execute(f"DROP TABLE IF EXISTS {_q(new)}")
        cols: list[str] = []
        for c, rows in batches:
            con.execute("BEGIN")
            if not cols:
                cols = list(c)
                # COLLATE NOCASE: SQL Server's default collation compares text case-insensitively, and the
                # data relies on it — 'tal-22-523-81' and 'TAL-22-523-81' are the same order there (a live
                # lookup returned 9 rows, a case-sensitive copy 7; found 2026-09-27). No declared type, so
                # numbers and text are stored exactly as read.
                con.execute(f"CREATE TABLE {_q(new)} ({', '.join(_q(x) + ' COLLATE NOCASE' for x in cols)})")
            con.executemany(f"INSERT INTO {_q(new)} VALUES ({', '.join('?' * len(cols))})",
                            [[_cell(v) for v in r] for r in rows])
            con.execute("COMMIT")
            total += len(rows)
        if not cols:
            raise RuntimeError(f"aggregate {name}: the query returned no columns")
        for ix in indexes:
            # unique name: the live copy's index (same column) still exists until the swap below
            con.execute(f"CREATE INDEX {_q(f'ix_{name}_{ix}_{uuid.uuid4().hex[:8]}')} ON {_q(new)} ({_q(ix)})")
        con.execute("BEGIN IMMEDIATE")       # the swap: readers see the old table or the new one
        try:
            con.execute(f"DROP TABLE IF EXISTS {_q(name)}")
            con.execute(f"ALTER TABLE {_q(new)} RENAME TO {_q(name)}")
            con.execute(f"INSERT OR REPLACE INTO {META} VALUES (?, ?, ?, ?, ?)",
                        (name, datetime.now().isoformat(timespec="seconds"), total,
                         round(time.perf_counter() - t0, 1), ",".join(views)))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    except Exception:
        try:
            con.execute(f"DROP TABLE IF EXISTS {_q(new)}")   # never leave a half-built copy behind
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()
    return total


def available(name: str, path: Optional[Path] = None) -> Optional[datetime]:
    """When the aggregate was last refreshed, or None if it has never been built."""
    path = path or db_path()
    if not path.exists():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute(f"SELECT as_of FROM {META} WHERE name = ?", (name,)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        con.close()
    return datetime.fromisoformat(row[0]) if row else None


def missing(specs: Iterable[dict[str, Any]], path: Optional[Path] = None) -> list[str]:
    """Names of the specs whose local copy has never been built (available() is None), in spec order —
    what `refresh_aggregates.py --at` builds at start-up instead of re-reading every copy."""
    return [s["name"] for s in specs if available(s["name"], path) is None]


# ---------------------------------------------------------------- the refresh schedule (--at HH:MM)
_HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def next_run(hhmm: str, now: datetime) -> datetime:
    """The next `hhmm` ("02:00", local time) strictly after `now`: today's if still ahead, else tomorrow's.

    Strictly after, so a refresh that starts at 02:00:00 and asks again when it ends is scheduled for the
    next day rather than run twice. ValueError unless `hhmm` is HH:MM on a 24-hour clock.
    """
    m = _HHMM.fullmatch(hhmm.strip()) if isinstance(hhmm, str) else None
    if m is None:
        raise ValueError(f"not a time of day HH:MM (00:00-23:59): {hhmm!r}")
    at = now.replace(hour=int(m[1]), minute=int(m[2]), second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def seconds_until(hhmm: str, now: datetime) -> float:
    """Seconds from `now` to next_run(hhmm, now); always > 0."""
    return (next_run(hhmm, now) - now).total_seconds()


def sleep_until(when: datetime, chunk: float = 300.0, clock: Callable[[], datetime] = datetime.now,
                sleep: Callable[[float], None] = time.sleep) -> None:
    """Block until the wall clock reaches `when`, sleeping at most `chunk` seconds at a time and reading
    the clock again after each. One sleep of many hours drifts from the wall clock when the host suspends
    or its clock is stepped, and the refresh would start late by however far it drifted."""
    while (left := (when - clock()).total_seconds()) > 0:
        sleep(min(left, chunk))


def has_column(name: str, column: str, path: Optional[Path] = None) -> bool:
    """Whether the local copy `name` has `column` (a copy refreshed before a column was added lacks it)."""
    path = path or db_path()
    if not path.exists():
        return False
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return column.lower() in {r[1].lower() for r in con.execute(f"PRAGMA table_info({_q(name)})")}
    finally:
        con.close()


def scoped_local_sql(sql: str, name: str, column: str, codes: list[str]) -> Optional[str]:
    """`sql` (a fixed tool's local_sql) reading only the rows of `name` whose `column` is one of `codes`
    (validated factory codes, auth.models.Scope). None when the statement does not read `name` in
    exactly one FROM (and no JOIN), or when there are no codes: the caller then goes live, where the
    view applies the scope."""
    from_ = re.compile(rf'\bFROM\s+"?{re.escape(name)}"?(?![\w"])', re.I)
    if not codes or len(from_.findall(sql)) != 1 or re.search(rf'\bJOIN\s+"?{re.escape(name)}\b', sql, re.I):
        return None
    lits = ", ".join(f"'{c}'" for c in codes)
    return from_.sub(f"FROM (SELECT * FROM {_q(name)} WHERE {_q(column)} IN ({lits})) AS {_q(name)}", sql, count=1)


def run_local(sql: str, params: dict[str, Any], max_rows: int = 200,
              path: Optional[Path] = None) -> tuple[list[str], list[list[Any]]]:
    """Run a fixed tool's `local_sql` (trusted config, @name parameters) against the local copy,
    opened read-only."""
    q, names = _positional(sql, "@")
    con = sqlite3.connect(f"file:{path or db_path()}?mode=ro", uri=True)
    try:
        cur = con.execute(q, [_cell(params[n]) for n in names])
        cols = [d[0] for d in cur.description]
        return cols, [list(r) for r in cur.fetchmany(max_rows)]
    finally:
        con.close()


def daily_plan(at: str, specs: list[dict[str, Any]], now: datetime,
               path: Optional[Path] = None) -> tuple[list[str], datetime]:
    """What `refresh_aggregates.py --at` does on start: (the copies to build now — only the missing ones, never
    the empty list that means "all" — and the first scheduled refresh, fixed before that build runs)."""
    return missing(specs, path), next_run(at, now)
