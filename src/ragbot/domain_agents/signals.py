"""Signals store (Phase L): data/index/signals.db, SQLite, local to this server. Written only by
scripts/watch.py, read by the app's morning brief. Never a production database (non-negotiable #7).

Every run is kept (table `signal` keyed by run id): Phase M compares stored levels with what happened.

Phase M adds `risk_run` + `prediction` beside `watch_run` + `signal`, deliberately as SIBLING tables and
not as another kind of watch run: `latest()` takes the newest row of `watch_run`, so a risk run written
there would silently become "the morning brief" and replace it. A prediction also carries its outcome,
filled in later when the subject settles — the Stage-2 gate is "several weeks of stored levels compared
with what actually happened", and the counts behind a level cannot be recovered once the window moves on.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import date, datetime
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

CREATE TABLE IF NOT EXISTS risk_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started TEXT NOT NULL, finished TEXT NOT NULL, dimension TEXT NOT NULL,
  predictions INTEGER NOT NULL, errors TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS prediction (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL REFERENCES risk_run(id),
  dimension TEXT NOT NULL, subject TEXT NOT NULL, factory TEXT NOT NULL,
  level TEXT NOT NULL, window_days INTEGER NOT NULL, days INTEGER NOT NULL DEFAULT 0,
  totals TEXT NOT NULL DEFAULT '{}', measures TEXT NOT NULL DEFAULT '[]',
  caveat TEXT NOT NULL DEFAULT '',
  database TEXT NOT NULL DEFAULT '', views TEXT NOT NULL DEFAULT '[]', sql TEXT NOT NULL DEFAULT '',
  as_of TEXT NOT NULL,
  outcome_at TEXT, outcome_level TEXT, outcome_totals TEXT, outcome_measures TEXT,
  revised INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_prediction_run ON prediction(run_id);
CREATE INDEX IF NOT EXISTS ix_prediction_subject ON prediction(dimension, subject, as_of);
CREATE INDEX IF NOT EXISTS ix_prediction_open ON prediction(dimension, outcome_at);
"""


def _migrate(con: sqlite3.Connection) -> None:
    """Columns added after a store was first created (kind: 2026-10-06)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(signal)")}
    if "kind" not in cols:
        con.execute("ALTER TABLE signal ADD COLUMN kind TEXT NOT NULL DEFAULT 'ops'")


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """Every open of this store goes through here.

    WAL because two writers now exist — the hourly watcher and the daily risk job — and the app reads
    while they write; without it one of them meets "database is locked". busy_timeout makes a concurrent
    writer wait rather than raise. `journal_mode` is a property of the file, so setting it on any write
    connection is enough, and it cannot be set on a read-only one.
    """
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA busy_timeout = 5000")
        if not readonly:
            con.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        pass            # a store on a filesystem that refuses WAL still works, just without it
    return con


def db_path() -> Path:
    return settings().path("index_dir") / "signals.db"


def write_run(signals: list[Signal], started: datetime, errors: list[str], path: Optional[Path] = None) -> int:
    """Store one watcher run in a single transaction; the app never sees half a run. Returns the run id."""
    path = path or db_path()
    con = _connect(path)
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
    con = _connect(path, readonly=True)
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


# ---------------------------------------------------------------- Phase M: predictions and outcomes
def write_risk_run(dimension: str, assessments: list[Any], started: datetime, errors: list[str],
                   source: Optional[dict[str, Any]] = None, path: Optional[Path] = None) -> int:
    """Store one risk run — its own tables, never `watch_run`, which `latest()` would treat as the brief.

    `source` is the database, views and SQL the assessments were read from; it is stored per row so a
    level read back months later can still say where its numbers came from.
    """
    path = path or db_path()
    src = source or {}
    con = _connect(path)
    try:
        con.executescript(_SCHEMA)
        _migrate(con)
        now = datetime.now().isoformat(timespec="seconds")
        with con:
            cur = con.execute(
                "INSERT INTO risk_run (started, finished, dimension, predictions, errors) VALUES (?,?,?,?,?)",
                (started.isoformat(timespec="seconds"), now, dimension, len(assessments), json.dumps(errors)))
            run_id = int(cur.lastrowid)
            con.executemany(
                "INSERT INTO prediction (run_id, dimension, subject, factory, level, window_days, days, "
                "totals, measures, caveat, database, views, sql, as_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, a.dimension, a.subject, a.factory, a.level, a.window_days, a.days,
                  json.dumps(a.totals), json.dumps([asdict(m) for m in a.measures]), a.caveat,
                  str(src.get("database", "")), json.dumps(src.get("views", [])), str(src.get("sql", "")), now)
                 for a in assessments])
        return run_id
    finally:
        con.close()


def _assessment(row: tuple) -> Any:
    """Rebuild an Assessment from a stored row, so readers use the same object the engine produces."""
    from .risk import Assessment, Measured
    a = Assessment(dimension=row[0], subject=row[1], factory=row[2], window_days=int(row[3]),
                   level=row[4], totals=json.loads(row[5]), caveat=row[7], days=int(row[8]))
    a.measures = [Measured(**m) for m in json.loads(row[6])]
    return a


def risk_due(today: Optional[date] = None, path: Optional[Path] = None) -> bool:
    """Whether today's risk run is still to do: no `risk_run` row started on `today` (the factory's date).

    The levels are a daily series — one prediction per running file per day is what the backtest reads — so
    the hourly watcher scores risk on its first tick of the day and not again (`watch.py --risk-daily`).
    A pass that raised writes no row, so a failed night is simply due again on the next tick.

    Store-wide, not per dimension: right for today's single dimension (quality). With a second one, a night
    on which one dimension wrote its row and the other raised would count as done and the failed one would
    wait a day — give this a `dimension` argument and run only the dimensions still due before adding one.
    """
    path = path or db_path()
    if not path.exists():
        return True
    con = _connect(path, readonly=True)
    try:
        row = con.execute("SELECT 1 FROM risk_run WHERE started LIKE ? LIMIT 1",
                          (f"{(today or date.today()).isoformat()}%",)).fetchone()
    except sqlite3.Error:          # a store from before Phase M has no risk_run table yet
        return True
    finally:
        con.close()
    return row is None


def risk_latest(scope: Scope, dimension: str = "quality",
                path: Optional[Path] = None) -> tuple[Optional[dict[str, Any]], list[Any]]:
    """The newest risk run for a dimension and its predictions, for the factories `scope` may see.

    Filtered on `factory` exactly as latest() is: a per-file prediction carries its factory precisely so
    this filter can be applied — without it a file-level row could not be scoped and would reach everyone.
    """
    path = path or db_path()
    if not path.exists():
        return None, []
    con = _connect(path, readonly=True)
    try:
        row = con.execute("SELECT id, started, finished, dimension, predictions, errors FROM risk_run "
                          "WHERE dimension = ? ORDER BY id DESC LIMIT 1", (dimension,)).fetchone()
        if row is None:
            return None, []
        run = {"id": row[0], "started": row[1], "finished": row[2], "dimension": row[3],
               "predictions": row[4], "errors": json.loads(row[5])}
        rows = con.execute(
            "SELECT dimension, subject, factory, window_days, level, totals, measures, caveat, days "
            "FROM prediction WHERE run_id = ?", (row[0],)).fetchall()
    except sqlite3.Error:
        return None, []
    finally:
        con.close()
    return run, [_assessment(r) for r in rows if scope.allows_factory(r[2])]


def open_predictions(dimension: str = "quality", path: Optional[Path] = None) -> list[dict[str, Any]]:
    """The newest prediction per subject that has no outcome yet — what the outcome pass has to settle.

    One row per subject, not one per run: a subject is predicted every day, and settling it once is the
    point. `ORDER BY id DESC` inside the group takes the most recent prediction for that subject.
    """
    path = path or db_path()
    if not path.exists():
        return []
    con = _connect(path, readonly=True)
    try:
        rows = con.execute(
            "SELECT p.id, p.subject, p.factory, p.level, p.as_of, p.window_days FROM prediction p "
            "WHERE p.dimension = ? AND p.outcome_at IS NULL "
            "  AND p.id = (SELECT MAX(q.id) FROM prediction q "
            "              WHERE q.dimension = p.dimension AND q.subject = p.subject AND q.factory = p.factory) "
            "ORDER BY p.subject", (dimension,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [dict(id=r[0], subject=r[1], factory=r[2], level=r[3], as_of=r[4], window_days=r[5]) for r in rows]


def write_outcome(prediction_id: int, level: str, totals: dict[str, Any], measures: list[dict[str, Any]],
                  path: Optional[Path] = None) -> bool:
    """Record what actually happened for one prediction. Returns True when it was written.

    An outcome already recorded is not overwritten blindly: if it is written again with different numbers
    the row's `revised` counter goes up, so a backtest can exclude subjects that were settled early and
    then moved. (A file can pause and resume.)
    """
    path = path or db_path()
    con = _connect(path)
    try:
        con.executescript(_SCHEMA)
        now = datetime.now().isoformat(timespec="seconds")
        with con:
            row = con.execute("SELECT outcome_at, outcome_level, outcome_totals FROM prediction WHERE id = ?",
                              (prediction_id,)).fetchone()
            if row is None:
                return False
            if row[0] is not None:
                changed = row[1] != level or row[2] != json.dumps(totals)
                con.execute("UPDATE prediction SET outcome_at = ?, outcome_level = ?, outcome_totals = ?, "
                            "outcome_measures = ?, revised = revised + ? WHERE id = ?",
                            (now, level, json.dumps(totals), json.dumps(measures), 1 if changed else 0,
                             prediction_id))
                return True
            con.execute("UPDATE prediction SET outcome_at = ?, outcome_level = ?, outcome_totals = ?, "
                        "outcome_measures = ? WHERE id = ?",
                        (now, level, json.dumps(totals), json.dumps(measures), prediction_id))
        return True
    finally:
        con.close()


def settled(dimension: str = "quality", path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Every prediction that has an outcome: the rows a backtest reads. `revised` marks the ones that were
    settled and then moved, so they can be excluded."""
    path = path or db_path()
    if not path.exists():
        return []
    con = _connect(path, readonly=True)
    try:
        rows = con.execute(
            "SELECT subject, factory, level, as_of, outcome_at, outcome_level, outcome_totals, revised, totals "
            "FROM prediction WHERE dimension = ? AND outcome_at IS NOT NULL ORDER BY outcome_at, subject",
            (dimension,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [dict(subject=r[0], factory=r[1], level=r[2], as_of=r[3], outcome_at=r[4], outcome_level=r[5],
                 outcome_totals=json.loads(r[6]) if r[6] else {}, revised=int(r[7]),
                 totals=json.loads(r[8]) if r[8] else {}) for r in rows]
