"""Pre-computed aggregates (Phase C6, docs/ROADMAP.md): heavy results copied into a local SQLite file.

Some curated queries cannot run fast on the server without DDL we may not do — e.g. PCD history reads
dbo.ExportOrderBack (4.2M rows, no index on ExportOrderID; ~10 s and timeouts per question, measured
2026-09-27). scripts/refresh_aggregates.py copies such a result once (nightly) into
data/index/aggregates.db, indexed for the lookups the fixed tools make; a fixed tool that names an
`aggregate:` then answers from the copy in milliseconds, and its [D#] reference shows the copy's
as-of time. "Refresh data" (refresh=True) or a missing copy falls back to the live query.

Write barrier unchanged: the server side is read by connectors.stream_sqlserver (read-only, rolled
back) after guard.assert_read_only; only the local file is ever written.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

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
