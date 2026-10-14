"""Stage-2 risk levels (Phase M): rules loaded from config/rules/<dimension>.yaml, validated like watch
rules and scored by the same function.

A level is computed per subject — for Quality, the VistaQ order file (FR number) over a trailing window —
and is deliberately NOT a second way of expressing a level: `watch.level()` does the comparison, and the
rules reuse its `amber` / `red` / `direction` keys, so there is one vocabulary and one place where a
threshold means something.

Two things this module is careful about.

Amber is the factory's own target, read live from the database (`amber_source` names the column it mirrors),
not a number this project chose. The copy in the YAML exists so a level is computable from config alone;
`drift()` compares the two so a target changed in VistaQ cannot silently stop matching the file.

Red is provisional. `red_caveat` travels with every rule that declares `red_provisional`, is stored on the
row the evaluation produces and is shown wherever the level is shown — an unvalidated threshold presented
as a judgement would be the dishonest part of this feature.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from ..config import ROOT

MEASURES = ("dhu", "reject_pct", "ftp_pct", "defective_pct", "rectified_pct")


@dataclass
class RuleSet:
    """One dimension's rules: config/rules/<dimension>.yaml."""
    dimension: str
    window_days: int
    red_caveat: str
    rules: list[dict[str, Any]] = field(default_factory=list)

    def rule(self, name: str) -> Optional[dict[str, Any]]:
        return next((r for r in self.rules if r.get("name") == name), None)


def rules_dir() -> Path:
    return ROOT / "config" / "rules"


def _stamp(folder: Path) -> tuple:
    return tuple(sorted((f.name, f.stat().st_mtime_ns) for f in folder.glob("*.yaml"))) if folder.exists() else ()


def load_rule_sets(folder: Optional[Path] = None) -> dict[str, RuleSet]:
    """{dimension: RuleSet}. Cached on the folder's file mtimes, like the catalogs and the agents, so an
    edited threshold is picked up without a restart (the compose mount, not the image, carries it)."""
    folder = folder or rules_dir()
    return dict(_load(folder, _stamp(folder)))


@lru_cache(maxsize=4)
def _load(folder: Path, _stamp: tuple) -> tuple[tuple[str, RuleSet], ...]:
    out: list[tuple[str, RuleSet]] = []
    if not folder.exists():
        return ()
    for f in sorted(folder.glob("*.yaml")):
        if f.name.endswith(".example.yaml"):
            continue
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        name = str(d.get("dimension") or f.stem)
        out.append((name, RuleSet(dimension=name,
                                  window_days=int(d.get("window_days") or 7),
                                  red_caveat=str(d.get("red_caveat") or "").strip(),
                                  rules=list(d.get("rules") or []))))
    return tuple(out)


def validate(sets: dict[str, RuleSet]) -> list[str]:
    """Problems in the risk rules, in the shape watch.validate() returns so check_catalog prints them the
    same way: a unique name, the keys a level needs, a known measure, thresholds that are both present and
    in the right order, and a caveat whenever a red level is declared provisional."""
    problems, seen = [], set()
    for dim, rs in sorted(sets.items()):
        if not rs.rules:
            problems.append(f"risk {dim}: no rules")
        for r in rs.rules:
            key = f"{dim}.{r.get('name')}"
            if not r.get("name") or key in seen:
                problems.append(f"risk {key}: needs a unique name")
            seen.add(key)
            for k in ("title", "measure"):
                if not r.get(k):
                    problems.append(f"risk {key}: missing {k}")
            if r.get("measure") and r["measure"] not in MEASURES:
                problems.append(f"risk {key}: measure {r['measure']!r} is not one of {', '.join(MEASURES)}")
            a, red = r.get("amber"), r.get("red")
            if (a is None) != (red is None):
                problems.append(f"risk {key}: give both amber and red, or neither (information only)")
            elif a is not None:
                below = r.get("direction") == "below"
                if (float(a) > float(red)) if not below else (float(a) < float(red)):
                    problems.append(f"risk {key}: amber and red are in the wrong order for direction "
                                    f"{'below' if below else 'above'}")
            # An unvalidated red shown as a judgement is the one dishonest thing this feature could do.
            if r.get("red_provisional") and not rs.red_caveat:
                problems.append(f"risk {key}: red is provisional, so the rule set needs a red_caveat")
            if r.get("amber") is not None and not r.get("amber_source"):
                problems.append(f"risk {key}: amber must name the database column it mirrors (amber_source)")
            if r.get("min_checked") is not None and int(r["min_checked"]) < 0:
                problems.append(f"risk {key}: min_checked cannot be negative")
    return problems


def drift(rs: RuleSet, targets: dict[str, Any]) -> list[str]:
    """Where the YAML's amber no longer matches the target the database holds. `targets` is one row of the
    source table keyed by column name. A changed target must not silently stop matching this file."""
    out = []
    for r in rs.rules:
        col = r.get("amber_source")
        if not col or r.get("amber") is None or col not in targets or targets[col] is None:
            continue
        if abs(float(targets[col]) - float(r["amber"])) > 1e-6:
            out.append(f"risk {rs.dimension}.{r['name']}: amber {r['amber']} no longer matches "
                       f"{col} = {targets[col]} in the database")
    return out


# The counts a measure is computed from. Named here so the ratio is never taken from a ratio column:
# the Quality charter is explicit that DHU is 100 x SUM(Defects) / SUM(Checked) over the sums, and that
# averaging the DHU column is wrong — a line that checked 40 pieces would otherwise weigh as much as one
# that checked 4,000.
_SUMS = ("Checked", "Passed", "DefectivePcs", "Rectified", "Rejected", "Defects")
_RATIOS = {
    "dhu": ("Defects", "Checked"),
    "reject_pct": ("Rejected", "Checked"),
    "ftp_pct": ("Passed", "Checked"),
    "defective_pct": ("DefectivePcs", "Checked"),
    "rectified_pct": ("Rectified", "DefectivePcs"),
}
_WORST = ("red", "amber", "green", "info")


@dataclass
class Measured:
    """One rule's measure over the window, and the level it scores."""
    rule: str
    measure: str
    value: Optional[float]
    level: str
    amber: Optional[float] = None
    red: Optional[float] = None
    red_provisional: bool = False
    enough: bool = True            # False when the window checked fewer pieces than the rule asks for
    reason: str = ""


@dataclass
class Assessment:
    """A subject's risk over a window: the level, how it was reached, and the counts it came from.

    Everything a backtest will need later is on this object, because the counts behind a level cannot be
    recovered once the window has moved on."""
    dimension: str
    subject: str                   # the VistaQ file (FR number) for Quality
    factory: str                   # never blank: the scope filter is applied on this
    window_days: int
    level: str
    totals: dict[str, float] = field(default_factory=dict)
    measures: list[Measured] = field(default_factory=list)
    caveat: str = ""               # travels with a provisional red so a later reader cannot lose it
    days: int = 0

    @property
    def provisional(self) -> bool:
        """Whether this level rests on a threshold nobody has validated."""
        return self.level == "red" and any(m.level == "red" and m.red_provisional for m in self.measures)


def totals(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Sum the counts over the window. Missing and None values count as zero: VistaQ returns no row for a
    measure a line did not record that day, which is a zero, not an unknown."""
    out = {k: 0.0 for k in _SUMS}
    for r in rows:
        for k in _SUMS:
            v = r.get(k)
            if v is not None:
                out[k] += float(v)
    return out


def ratio(measure: str, sums: dict[str, float]) -> Optional[float]:
    """A measure recomputed from the sums, never averaged from a ratio column (the charter's rule).
    None when the denominator is zero — nothing was checked, so the measure does not exist."""
    pair = _RATIOS.get(measure)
    if pair is None:
        return None
    num, den = sums.get(pair[0], 0.0), sums.get(pair[1], 0.0)
    if not den:
        return None
    return round(100.0 * num / den, 2)


def evaluate(rs: RuleSet, subject: str, factory: str, rows: list[dict[str, Any]],
             days: Optional[int] = None) -> Assessment:
    """Score one subject over its window. Pure: no database, no clock, no settings — the caller supplies
    the rows, exactly as data/quality.py evaluate() takes `today` so it can be tested without either.

    A rule whose window checked fewer pieces than `min_checked` scores `info`, not `green`: at 50 pieces a
    single garment with three defects is a DHU of 6, which would read as red on noise. `info` says "not
    enough was checked to judge", which is true, where green would be a claim.
    """
    from .watch import level as level_of

    sums = totals(rows)
    out: list[Measured] = []
    for r in rs.rules:
        measure = str(r.get("measure") or "")
        value = ratio(measure, sums)
        need = float(r.get("min_checked") or 0)
        enough = sums.get("Checked", 0.0) >= need
        if not enough:
            lvl, why = "info", (f"{sums.get('Checked', 0):.0f} pieces checked in {days or rs.window_days} days, "
                                f"fewer than the {need:.0f} this rule needs to judge")
        else:
            lvl = level_of(r, value)
            target = r.get("amber")
            why = (f"{measure} {value} over {days or rs.window_days} days"
                   + (f", target {target}" if target is not None else "")) if value is not None else \
                  "nothing was checked in the window"
        out.append(Measured(rule=str(r.get("name") or ""), measure=measure, value=value, level=lvl,
                            amber=r.get("amber"), red=r.get("red"),
                            red_provisional=bool(r.get("red_provisional")), enough=enough, reason=why))

    worst = next((l for l in _WORST if any(m.level == l for m in out)), "info")
    a = Assessment(dimension=rs.dimension, subject=subject, factory=factory,
                   window_days=days or rs.window_days, level=worst, totals=sums, measures=out,
                   days=len({r.get("InspectionDate") for r in rows if r.get("InspectionDate") is not None}))
    # The caveat is stored on the assessment, not looked up when it is displayed: a row read months later
    # must still carry the reason its red is not a judgement.
    a.caveat = rs.red_caveat if a.provisional else ""
    return a


# ---------------------------------------------------------------- reading the subjects (Phase M5)
# One row per subject, aggregated IN SQL. Two reasons, and they point the same way: the Quality charter
# says to compute a ratio from the sums in the SQL, and grouping by day instead returned exactly 200 rows
# of a true 755 on 2026-10-14 — data.max_rows had truncated it silently and the levels covered a quarter
# of production with no error. This read goes through stream_sqlserver, which is not row-capped (the same
# path scripts/refresh_aggregates.py uses), so the cap cannot bite again; `max_subjects` is a loud stop,
# not a quiet trim.
SUBJECT_SQL = """
SELECT Factory, FileNo, COUNT(DISTINCT InspectionDate) AS Days,
       SUM(Checked) AS Checked, SUM(Passed) AS Passed, SUM(DefectivePcs) AS DefectivePcs,
       SUM(Rectified) AS Rectified, SUM(Rejected) AS Rejected, SUM(Defects) AS Defects
FROM rag.vw_PlanQualityDay
WHERE InspectionDate >= DATEADD(day, -{days}, {as_of}) AND FileNo IS NOT NULL
GROUP BY Factory, FileNo
"""
# A subject still running: VistaQ holds a plan for it dated after its last inspection day. Measured
# against the newest day VistaQ ITSELF holds, never the wall clock — a feed outage would otherwise look
# like every file finishing at once.
RUNNING_SQL = """
SELECT Factory, FileNo, MAX(PlanDate) AS LastPlan
FROM rag.vw_PlanDay
WHERE PlanHours > 0 AND FileNo IS NOT NULL AND PlanDate >= DATEADD(day, -{days}, {as_of})
GROUP BY Factory, FileNo
"""
NEWEST_DAY_SQL = "SELECT MAX(InspectionDate) AS NewestDay FROM rag.vw_LineDayQuality"


def _read(sql: str, cat: Any, *, timeout: int, tool: str) -> tuple[list[str], list[list[Any]], list[str]]:
    """Guard, expand and stream one read. Unrestricted scope: the store keeps every factory and the scope
    filter is applied when a brief or an answer reads it, exactly as the watcher does."""
    from ..auth.models import Scope
    from ..data.connectors import stream_sqlserver
    from ..data.guard import assert_read_only
    from ..data.virtual import rewrite_virtual

    assert_read_only(sql)
    sql_exec, views = rewrite_virtual(sql, cat, scope=Scope.unrestricted())
    cols: list[str] = []
    rows: list[list[Any]] = []
    for batch_cols, batch in stream_sqlserver(sql_exec, conn_env=cat.connection_env, timeout=timeout,
                                              tool=tool, database=getattr(cat, "connection_database", None)):
        cols = cols or list(batch_cols)
        rows.extend(list(r) for r in batch)
    return cols, rows, views


def collect(rs: RuleSet, cat: Any, *, as_of: str = "CAST(GETDATE() AS date)", timeout: int = 180,
            max_subjects: int = 5000) -> tuple[list[Assessment], list[str], dict[str, Any]]:
    """Score every subject in the window. Returns (assessments, errors, source).

    `errors` is stored with the run and shown to admins: a run that could not see all of production says
    so rather than publishing levels for part of it.
    """
    errors: list[str] = []
    sql = SUBJECT_SQL.format(days=rs.window_days, as_of=as_of)
    cols, rows, views = _read(sql, cat, timeout=timeout, tool=f"risk:{rs.dimension}")
    if len(rows) > max_subjects:
        errors.append(f"{len(rows)} subjects is more than max_subjects {max_subjects}: refusing to score a "
                      f"subset — raise the limit or narrow the window")
        return [], errors, {"database": cat.database, "views": views, "sql": sql}

    out = []
    for r in rows:
        d = dict(zip(cols, r))
        factory, subject = str(d.get("Factory") or "").strip(), str(d.get("FileNo") or "").strip()
        if not factory or not subject:
            continue        # without a factory the row could not be scoped on read; without a subject it is nothing
        a = evaluate(rs, subject, factory, [d], days=rs.window_days)
        a.days = int(d.get("Days") or 0)
        out.append(a)
    return out, errors, {"database": cat.database, "views": views, "sql": sql}


def still_running(cat: Any, *, days: int = 30, as_of: str = "CAST(GETDATE() AS date)",
                  timeout: int = 180) -> set[tuple[str, str]]:
    """(factory, subject) pairs VistaQ still plans to run. A subject not in this set has finished."""
    cols, rows, _ = _read(RUNNING_SQL.format(days=days, as_of=as_of), cat, timeout=timeout,
                          tool="risk:running")
    out = set()
    for r in rows:
        d = dict(zip(cols, r))
        out.add((str(d.get("Factory") or "").strip(), str(d.get("FileNo") or "").strip()))
    return out


def newest_day(cat: Any, *, timeout: int = 60) -> Optional[str]:
    """The newest inspection day VistaQ itself holds. Settlement is measured against this, not the clock:
    if the feed stops, every open subject would otherwise look finished on the same day."""
    cols, rows, _ = _read(NEWEST_DAY_SQL, cat, timeout=timeout, tool="risk:newest_day")
    if not rows or rows[0][0] is None:
        return None
    return str(rows[0][0])[:10]


def settle(dimension: str, assessments: list[Assessment], running: set[tuple[str, str]],
           newest: Optional[str], path: Optional[Any] = None) -> int:
    """Record what actually happened for every subject that has finished running. Returns how many settled.

    Takes the assessments the run has already read: a subject's final window is the window that was just
    scored, so re-reading it per subject would be the same query a hundred times over.

    A subject settles only when the plans no longer carry it AND the feed itself has moved on. `newest` is
    the newest inspection day VistaQ holds, never the wall clock — a stopped feed would otherwise make
    every open subject look as though it finished on the same day, and the whole backtest would be built
    on outcomes recorded during an outage.
    """
    from .signals import open_predictions, write_outcome

    if not newest:
        return 0
    final = {(a.factory, a.subject): a for a in assessments}
    done = 0
    for p in open_predictions(dimension, path=path):
        key = (p["factory"], p["subject"])
        if key in running:
            continue                      # still planned: not finished
        a = final.get(key)
        if a is None:
            continue                      # nothing left in the window to settle against; try again tomorrow
        if write_outcome(p["id"], a.level, a.totals, [asdict(m) for m in a.measures], path=path):
            done += 1
    return done


def predictable(assessments: list[Assessment], running: set[tuple[str, str]]) -> list[Assessment]:
    """The subjects worth storing a prediction for: the ones still in production.

    A subject that has already finished would otherwise be predicted and settled in the same run, and a
    prediction with no lead time over its outcome tells a backtest nothing — it is a record of the present
    dressed as a forecast. The finished ones are still scored, because their final window is what the
    outcome is settled against.
    """
    return [a for a in assessments if (a.factory, a.subject) in running]
