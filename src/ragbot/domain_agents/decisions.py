"""Stage-3 recommendations and decisions (Phase N3a): two tables in data/index/signals.db, beside the
predictions they answer. Never a production database (non-negotiable #7).

NOTHING IN THE APP CALLS THIS MODULE. The owner kept the ladder gate (docs/AGENTS_DESIGN.md §4): no
recommendation reaches staff until the Stage-2 backtest has passed and real cost data exists, and the router
still refuses "approve" in chat (prompts/router.txt). What is built is the storage and its refusals, so that
the day the gate opens the rules are already enforced in code rather than in a prompt.

- A recommendation is written only from a stored prediction that its playbook's pattern matches, by an
  approved playbook that ready() clears. Its cost and effect are computed HERE (playbook.compute) — the caller
  passes the chosen parameters and the cost file, never a number — and it stores the playbook's SHA-256 and
  what it cites (the prediction's source, the playbook page, each cost source's owner and date), so a
  recommendation read a year later still says where every number came from.
- A decision is append-only. A later decision on the same recommendation supersedes the earlier one for
  every reader, and both are kept; triggers refuse UPDATE and DELETE on both tables. The shared `open` user
  (auth.provider: none) may not decide — nobody would be accountable — and nobody may decide for a factory
  outside their scope.
- decision_outcomes() is the record the Stage-3 gate reads: each decision joined to what then happened to
  its subject, from the outcome pass Phase M already runs.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from ..auth.models import Scope
from ..auth.providers import OPEN_USER
from . import signals
from .playbook import Playbook, compute, pattern_of, ready

VERDICTS = ("approve", "reject")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS recommendation (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created TEXT NOT NULL,
  prediction_id INTEGER NOT NULL REFERENCES prediction(id),
  dimension TEXT NOT NULL, subject TEXT NOT NULL, factory TEXT NOT NULL,
  playbook TEXT NOT NULL, playbook_sha TEXT NOT NULL, pattern TEXT NOT NULL, action TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}', cost TEXT NOT NULL DEFAULT '{}', effect TEXT NOT NULL DEFAULT '{}',
  cites TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS decision (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  recommendation_id INTEGER NOT NULL REFERENCES recommendation(id),
  decided_by TEXT NOT NULL, decided_at TEXT NOT NULL,
  verdict TEXT NOT NULL CHECK (verdict IN ('approve', 'reject')),
  reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_recommendation_prediction ON recommendation(prediction_id);
CREATE INDEX IF NOT EXISTS ix_decision_recommendation ON decision(recommendation_id);
CREATE TRIGGER IF NOT EXISTS recommendation_no_update BEFORE UPDATE ON recommendation
  BEGIN SELECT RAISE(ABORT, 'recommendation is append-only'); END;
CREATE TRIGGER IF NOT EXISTS recommendation_no_delete BEFORE DELETE ON recommendation
  BEGIN SELECT RAISE(ABORT, 'recommendation is append-only'); END;
CREATE TRIGGER IF NOT EXISTS decision_no_update BEFORE UPDATE ON decision
  BEGIN SELECT RAISE(ABORT, 'decision is append-only: write a new decision to change one'); END;
CREATE TRIGGER IF NOT EXISTS decision_no_delete BEFORE DELETE ON decision
  BEGIN SELECT RAISE(ABORT, 'decision is append-only: write a new decision to change one'); END;
"""


class Refused(ValueError):
    """A recommendation or decision this module will not write, and why."""


def _write_connect(path: Path) -> sqlite3.Connection:
    """A write connection with every table in place: signals' own schema first (unchanged, so the brief and
    the risk readers see exactly what they saw before), then these two tables. Foreign keys are on, so a
    recommendation cannot name a prediction that does not exist."""
    con = signals._connect(path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(signals._SCHEMA)
    signals._migrate(con)
    con.executescript(_SCHEMA)
    return con


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- writing
def write_recommendation(prediction_id: int, playbook: Playbook, action: str, params: dict[str, Any],
                         costs: dict[str, Any], path: Optional[Path] = None, *,
                         today: Optional[date] = None) -> int:
    """Store one recommendation and return its id. Raises Refused unless the prediction exists, the
    playbook's pattern matches it, the playbook is approved and ready() has nothing to say.

    The cost and effect are computed here from `params` (what a person chose) and `costs` (the cost file):
    there is deliberately no argument through which a caller could pass a number.
    """
    path = path or signals.db_path()
    con = _write_connect(path)
    try:
        row = con.execute(
            "SELECT dimension, subject, factory, window_days, level, totals, measures, caveat, days, "
            "database, views, sql, as_of FROM prediction WHERE id = ?", (prediction_id,)).fetchone()
        if row is None:
            raise Refused(f"prediction {prediction_id} does not exist")
        a = signals._assessment(row[:9])
        pattern = pattern_of(playbook, a)
        if pattern is None:
            raise Refused(f"playbook {playbook.id} does not match prediction {prediction_id} "
                          f"({a.dimension} {a.subject}, {a.factory})")
        if playbook.status != "approved":
            raise Refused(f"playbook {playbook.id} is {playbook.status or 'without a status'}, not approved")
        reasons = ready(playbook, costs, today)
        if reasons:
            raise Refused(f"playbook {playbook.id} is not ready: " + "; ".join(reasons))
        act = playbook.action(action)
        if act is None:
            raise Refused(f"playbook {playbook.id} has no action {action!r}")
        try:
            result = compute(act, params, costs, a)
        except ValueError as e:
            raise Refused(str(e)) from e

        cites: list[dict[str, Any]] = [
            {"kind": "prediction", "id": prediction_id, "database": row[9], "views": json.loads(row[10] or "[]"),
             "sql": row[11], "as_of": row[12], "level": a.level, "caveat": a.caveat},
            {"kind": "playbook", "playbook": playbook.id, "action": act.id, "sha": playbook.sha,
             "source_pdf": playbook.source_pdf, "page": act.pdf_page,
             "approved_by": playbook.approved_by,
             "approved_on": playbook.approved_on.isoformat() if playbook.approved_on else None},
        ]
        for block in (result["cost"], result["effect"]):
            for used in block["inputs"].values():
                if str(used.get("ref", "")).startswith("cost."):
                    cites.append({"kind": "cost", "source": used["ref"][len("cost."):], "owner": used.get("owner"),
                                  "from": used.get("source"), "as_of": used.get("as_of"), "unit": used.get("unit")})
        with con:
            cur = con.execute(
                "INSERT INTO recommendation (created, prediction_id, dimension, subject, factory, playbook, "
                "playbook_sha, pattern, action, params, cost, effect, cites) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_now(), prediction_id, a.dimension, a.subject, a.factory, playbook.id, playbook.sha, pattern,
                 act.id, json.dumps(params or {}, sort_keys=True), json.dumps(result["cost"]),
                 json.dumps(result["effect"]), json.dumps(cites)))
            return int(cur.lastrowid)
    finally:
        con.close()


def write_decision(recommendation_id: int, user: Any, verdict: str, reason: str = "",
                   path: Optional[Path] = None) -> int:
    """Append one decision and return its id. A later decision supersedes this one; neither is ever changed.

    `user` is the signed-in auth.models.User. Refused: no user or a blank name; the shared `open` user, who
    is everyone and so no one; a verdict other than approve or reject; a reject without a reason; an unknown
    recommendation; a recommendation for a factory outside the user's scope.
    """
    name = str(getattr(user, "name", "") or "").strip()
    if not name:
        raise Refused("a decision needs a named, signed-in user")
    if name.lower() == OPEN_USER:
        raise Refused("the shared open user cannot decide: with sign-in off nobody would be accountable for it")
    if verdict not in VERDICTS:
        raise Refused(f"verdict {verdict!r} is not one of {', '.join(VERDICTS)}")
    reason = str(reason or "").strip()
    if verdict == "reject" and not reason:
        raise Refused("a reject needs a reason: it is what calibrates the playbook")
    scope = getattr(user, "scope", None)
    if not isinstance(scope, Scope):
        raise Refused("a decision needs the user's scope")
    path = path or signals.db_path()
    con = _write_connect(path)
    try:
        row = con.execute("SELECT factory FROM recommendation WHERE id = ?", (recommendation_id,)).fetchone()
        if row is None:
            raise Refused(f"recommendation {recommendation_id} does not exist")
        if not scope.allows_factory(row[0]):
            raise Refused(f"recommendation {recommendation_id} is for {row[0]}, outside {name}'s factories")
        with con:
            cur = con.execute("INSERT INTO decision (recommendation_id, decided_by, decided_at, verdict, reason) "
                              "VALUES (?,?,?,?,?)", (recommendation_id, name, _now(), verdict, reason))
            return int(cur.lastrowid)
    finally:
        con.close()


# ---------------------------------------------------------------- reading
def _read(path: Path, sql: str, args: tuple = ()) -> list[tuple]:
    """Rows from the store, or [] when it, or these tables, do not exist yet (a store from before N3)."""
    if not path.exists():
        return []
    con = signals._connect(path, readonly=True)
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


_CURRENT = "d.id = (SELECT MAX(e.id) FROM decision e WHERE e.recommendation_id = d.recommendation_id)"


def recommendations(scope: Scope, dimension: Optional[str] = None, *, subject: Optional[str] = None,
                    path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Recommendations for the factories `scope` may see, oldest first, each with its current decision
    (the newest one) or None."""
    path = path or signals.db_path()
    filters = [(col, val) for col, val in (("dimension", dimension), ("subject", subject)) if val]
    where = " WHERE " + " AND ".join(f"{col} = ?" for col, _ in filters) if filters else ""
    rows = _read(path, "SELECT id, created, prediction_id, dimension, subject, factory, playbook, playbook_sha, "
                       f"pattern, action, params, cost, effect, cites FROM recommendation{where} ORDER BY id",
                 tuple(val for _, val in filters))
    current = {r[0]: dict(id=r[1], decided_by=r[2], decided_at=r[3], verdict=r[4], reason=r[5])
               for r in _read(path, "SELECT d.recommendation_id, d.id, d.decided_by, d.decided_at, d.verdict, "
                                    f"d.reason FROM decision d WHERE {_CURRENT}")}
    return [dict(id=r[0], created=r[1], prediction_id=r[2], dimension=r[3], subject=r[4], factory=r[5],
                 playbook=r[6], playbook_sha=r[7], pattern=r[8], action=r[9], params=json.loads(r[10]),
                 cost=json.loads(r[11]), effect=json.loads(r[12]), cites=json.loads(r[13]),
                 decision=current.get(r[0]))
            for r in rows if scope.allows_factory(r[5])]


def decisions(scope: Scope, recommendation_id: Optional[int] = None,
              path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Every decision (superseded ones included, marked `current: False`) on recommendations for the
    factories `scope` may see, oldest first."""
    path = path or signals.db_path()
    rows = _read(path, "SELECT d.id, d.recommendation_id, d.decided_by, d.decided_at, d.verdict, d.reason, "
                       f"r.factory, {_CURRENT} FROM decision d JOIN recommendation r ON r.id = d.recommendation_id"
                       + (" WHERE d.recommendation_id = ?" if recommendation_id is not None else "")
                       + " ORDER BY d.id", (recommendation_id,) if recommendation_id is not None else ())
    return [dict(id=r[0], recommendation_id=r[1], decided_by=r[2], decided_at=r[3], verdict=r[4], reason=r[5],
                 factory=r[6], current=bool(r[7]))
            for r in rows if scope.allows_factory(r[6])]


def decision_outcomes(dimension: str = "quality", path: Optional[Path] = None) -> list[dict[str, Any]]:
    """Each recommendation's current decision joined to what then happened to its subject: the record the
    Stage-3 gate reads ("accepted recommendations show a record of good outcomes"). Unscoped, like
    signals.settled(): an admin and backtest read, never shown to staff as it stands.

    The outcome is the subject's newest SETTLED prediction at or after the one recommended on — the outcome
    pass settles the newest prediction per subject, which is usually a later day's than the recommendation's.
    `settled` is False while the subject is still running."""
    path = path or signals.db_path()
    rows = _read(path, (
        "SELECT r.id, r.playbook, r.action, r.pattern, r.subject, r.factory, r.prediction_id, p.level, "
        "       d.id, d.verdict, d.decided_by, d.decided_at, d.reason, "
        "       o.id, o.outcome_at, o.outcome_level, o.outcome_totals, o.revised "
        "FROM decision d "
        "JOIN recommendation r ON r.id = d.recommendation_id "
        "JOIN prediction p ON p.id = r.prediction_id "
        "LEFT JOIN prediction o ON o.id = (SELECT MAX(q.id) FROM prediction q "
        "     WHERE q.dimension = r.dimension AND q.subject = r.subject AND q.factory = r.factory "
        "       AND q.outcome_at IS NOT NULL AND q.id >= r.prediction_id) "
        f"WHERE r.dimension = ? AND {_CURRENT} ORDER BY d.id"), (dimension,))
    return [dict(recommendation_id=r[0], playbook=r[1], action=r[2], pattern=r[3], subject=r[4], factory=r[5],
                 prediction_id=r[6], level=r[7], decision_id=r[8], verdict=r[9], decided_by=r[10],
                 decided_at=r[11], reason=r[12], outcome_prediction_id=r[13], outcome_at=r[14],
                 outcome_level=r[15], outcome_totals=json.loads(r[16]) if r[16] else {},
                 revised=int(r[17] or 0), settled=r[14] is not None)
            for r in rows]
