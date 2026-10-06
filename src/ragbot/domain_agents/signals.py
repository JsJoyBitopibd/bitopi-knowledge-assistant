"""Signals store (Phase L): data/index/signals.db, SQLite, local to this server. Written only by
scripts/watch.py, read by the app's morning brief. Never a production database (non-negotiable #7).

Every run is kept (table `signal` keyed by run id): Phase M compares stored levels with what happened.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..auth.models import Scope
from ..config import settings
from .watch import Signal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started TEXT NOT NULL, finished TEXT NOT NULL,
  signals INTEGER NOT NULL, errors TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS signal (
  run_id INTEGER NOT NULL REFERENCES watch_run(id),
  agent TEXT NOT NULL, rule TEXT NOT NULL, factory TEXT NOT NULL, level TEXT NOT NULL,
  value REAL, title TEXT NOT NULL, question TEXT NOT NULL, weight REAL NOT NULL, metric TEXT NOT NULL,
  database TEXT NOT NULL, views TEXT NOT NULL, sql TEXT NOT NULL, as_of TEXT NOT NULL, vals TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'ops'
);
CREATE INDEX IF NOT EXISTS ix_signal_run ON signal(run_id);
"""


def _migrate(con: sqlite3.Connection) -> None:
    """Columns added after a store was first created (kind: 2026-10-06)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(signal)")}
    if "kind" not in cols:
        con.execute("ALTER TABLE signal ADD COLUMN kind TEXT NOT NULL DEFAULT 'ops'")


def db_path() -> Path:
    return settings().path("index_dir") / "signals.db"


def write_run(signals: list[Signal], started: datetime, errors: list[str], path: Optional[Path] = None) -> int:
    """Store one watcher run in a single transaction; the app never sees half a run. Returns the run id."""
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript(_SCHEMA)
        _migrate(con)
        with con:
            cur = con.execute("INSERT INTO watch_run (started, finished, signals, errors) VALUES (?, ?, ?, ?)",
                              (started.isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds"),
                               len(signals), json.dumps(errors)))
            run_id = int(cur.lastrowid)
            con.executemany(
                "INSERT INTO signal (run_id, agent, rule, factory, level, value, title, question, weight, metric, "
                "database, views, sql, as_of, vals, kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, s.agent, s.rule, s.factory, s.level, s.value, s.title, s.question, s.weight, s.metric,
                  s.database, json.dumps(s.views), s.sql, s.as_of, json.dumps(s.values, default=str), s.kind)
                 for s in signals])
        return run_id
    finally:
        con.close()


def latest(scope: Scope, path: Optional[Path] = None) -> tuple[Optional[dict[str, Any]], list[Signal]]:
    """The newest run and its signals for the factories `scope` may see; (None, []) before the first run."""
    path = path or db_path()
    if not path.exists():
        return None, []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT id, started, finished, signals, errors FROM watch_run ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None, []
        run = {"id": row[0], "started": row[1], "finished": row[2], "signals": row[3], "errors": json.loads(row[4])}
        cols = {r[1] for r in con.execute("PRAGMA table_info(signal)")}
        kind = "kind" if "kind" in cols else "'ops'"      # read-only: a store from before `kind` is not migrated here
        rows = con.execute("SELECT agent, rule, factory, level, value, title, question, weight, metric, database, "
                           f"views, sql, as_of, vals, {kind} FROM signal WHERE run_id = ?", (row[0],)).fetchall()
    except sqlite3.Error:
        return None, []
    finally:
        con.close()
    out = []
    for r in rows:
        if not scope.allows_factory(r[2]):
            continue                    # the brief shows only the viewer's factories (PRD FR-4)
        out.append(Signal(agent=r[0], rule=r[1], factory=r[2], level=r[3], value=r[4], title=r[5], question=r[6],
                          weight=r[7], metric=r[8], database=r[9], views=json.loads(r[10]), sql=r[11], as_of=r[12],
                          values=json.loads(r[13]), kind=r[14]))
    return run, out


def as_dict(s: Signal) -> dict[str, Any]:
    return asdict(s)
