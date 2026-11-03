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

import re
from dataclasses import asdict, dataclass, field
from datetime import date
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
    last_day: str = ""             # the newest inspection day in the window (YYYY-MM-DD); "" when unknown

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
SELECT Factory, FileNo, COUNT(DISTINCT InspectionDate) AS Days, MAX(InspectionDate) AS LastDay,
       SUM(Checked) AS Checked, SUM(Passed) AS Passed, SUM(DefectivePcs) AS DefectivePcs,
       SUM(Rectified) AS Rectified, SUM(Rejected) AS Rejected, SUM(Defects) AS Defects
FROM rag.vw_PlanQualityDay
WHERE InspectionDate >= DATEADD(day, -{days}, {as_of}) AND FileNo IS NOT NULL
GROUP BY Factory, FileNo
"""
# A subject still running: VistaQ holds a plan for it on one of the last `PLAN_GRACE_DAYS` days before the
# newest inspection day VistaQ ITSELF holds — never the wall clock, or a feed outage would look like every
# file finishing at once.
#
# Until 2026-11-03 this read "any plan in the last 30 days", so a file stayed "running" for a month after
# its last plan, while its outcome is read from a 7-day window: by the time it left the set, nothing was left
# in the window to settle it against, and it never settled. The backtest of 3 Nov found 97 files stuck that
# way and 1 usable settled file in 21 days. Measured that day (read-only, 382 files): VistaQ holds no plan
# dated after today, and 98% of the gaps between a running file's plan days are 1 or 2 days (the Friday);
# 3 days covers a Friday plus a holiday. A file that pauses longer settles, is predicted again when it
# resumes, and the backtest leaves it out as "predicted again after its outcome".
PLAN_GRACE_DAYS = 3
RUNNING_SQL = """
SELECT Factory, FileNo, MAX(PlanDate) AS LastPlan
FROM rag.vw_PlanDay
WHERE PlanHours > 0 AND FileNo IS NOT NULL AND PlanDate >= DATEADD(day, -{days}, {as_of})
GROUP BY Factory, FileNo
"""
NEWEST_DAY_SQL = "SELECT MAX(InspectionDate) AS NewestDay FROM rag.vw_LineDayQuality"
NEWEST_PLAN_SQL = "SELECT MAX(PlanDate) AS NewestPlan FROM rag.vw_PlanDay WHERE PlanHours > 0"
# A finished subject's outcome: its own last `window_days` of inspections, ending on ITS last inspection day,
# whatever today is. The trailing window of the run would hold none of it once the file stopped a week ago.
FINAL_SQL = """
SELECT Factory, FileNo, MAX(LastDay) AS LastDay, COUNT(DISTINCT InspectionDate) AS Days,
       SUM(Checked) AS Checked, SUM(Passed) AS Passed, SUM(DefectivePcs) AS DefectivePcs,
       SUM(Rectified) AS Rectified, SUM(Rejected) AS Rejected, SUM(Defects) AS Defects
FROM (SELECT Factory, FileNo, InspectionDate, Checked, Passed, DefectivePcs, Rectified, Rejected, Defects,
             MAX(InspectionDate) OVER (PARTITION BY Factory, FileNo) AS LastDay
      FROM rag.vw_PlanQualityDay WHERE FileNo IN ({files})) AS q
WHERE InspectionDate >= DATEADD(day, -{days}, LastDay)
GROUP BY Factory, FileNo
"""
# File numbers go into FINAL_SQL as literals. They come from the store, which took them from VistaQ, but a
# literal is only ever built from a value this pattern accepts; anything else is skipped, never escaped.
_SAFE_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._/-]{0,63}")
_FINAL_BATCH = 200


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?")


def _sql_day(day: Any) -> str:
    """A day (`YYYY-MM-DD`, optionally with a time, or a date) as a T-SQL date literal. Raises ValueError
    for anything else — the whole value is checked, so nothing after a valid date is ever dropped quietly."""
    text = day.isoformat() if isinstance(day, date) else str(day)
    if not _DAY.fullmatch(text):
        raise ValueError(f"not a day: {text[:40]!r}")
    return f"CAST('{date.fromisoformat(text[:10]).isoformat()}' AS date)"


def _day_text(v: Any) -> str:
    return str(v)[:10] if v is not None else ""


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
        a.last_day = _day_text(d.get("LastDay"))
        out.append(a)
    return out, errors, {"database": cat.database, "views": views, "sql": sql}


def still_running(cat: Any, *, newest: Optional[str] = None, days: int = PLAN_GRACE_DAYS,
                  as_of: str = "CAST(GETDATE() AS date)", timeout: int = 180) -> set[tuple[str, str]]:
    """(factory, subject) pairs VistaQ still plans to run. A subject not in this set has finished.

    Planned = a plan on one of the last `days` days before `newest` (VistaQ's own newest inspection day);
    without `newest`, before `as_of`."""
    anchor = _sql_day(newest) if newest else as_of
    cols, rows, _ = _read(RUNNING_SQL.format(days=int(days), as_of=anchor), cat, timeout=timeout,
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


def newest_plan_day(cat: Any, *, timeout: int = 60) -> Optional[str]:
    """The newest day VistaQ holds a plan for. Paired with `plan_feed_stalled()`: "running" is read from the
    plans, so plans that stop being written would make every file look finished."""
    cols, rows, _ = _read(NEWEST_PLAN_SQL, cat, timeout=timeout, tool="risk:newest_plan")
    if not rows or rows[0][0] is None:
        return None
    return str(rows[0][0])[:10]


def plan_feed_stalled(newest: Optional[str], newest_plan: Optional[str],
                      grace_days: int = PLAN_GRACE_DAYS) -> Optional[str]:
    """Why settling must wait, or None. The plans are the evidence that a file is still running; when the
    newest plan is older than the grace before the newest inspection day, that evidence is missing for every
    file at once — an outage of the plans, not every file finishing on the same day."""
    if not newest:
        return None                       # settle() already does nothing without a newest inspection day
    if not newest_plan:
        return "VistaQ holds no plans: not settling, the plans are how a finished file is told apart"
    gap = (date.fromisoformat(newest[:10]) - date.fromisoformat(newest_plan[:10])).days
    if gap > grace_days:
        return (f"VistaQ's newest plan ({newest_plan[:10]}) is {gap} days older than its newest inspection "
                f"day ({newest[:10]}): not settling until the plans are current again")
    return None


def final_windows(rs: RuleSet, cat: Any, subjects: set[tuple[str, str]], *,
                  timeout: int = 180) -> dict[tuple[str, str], Assessment]:
    """Each finished subject scored over its OWN last `window_days` of inspections, ending on its last
    inspection day. Subjects with no inspection at all are absent; a file number the safe pattern refuses
    is skipped (it waits, it is never quoted)."""
    files = sorted({s for _f, s in subjects if _SAFE_FILE.fullmatch(s)})
    out: dict[tuple[str, str], Assessment] = {}
    for i in range(0, len(files), _FINAL_BATCH):
        part = ", ".join(f"'{f}'" for f in files[i:i + _FINAL_BATCH])
        sql = FINAL_SQL.format(files=part, days=int(rs.window_days))
        cols, rows, _ = _read(sql, cat, timeout=timeout, tool=f"risk:{rs.dimension}:final")
        for r in rows:
            d = dict(zip(cols, r))
            key = (str(d.get("Factory") or "").strip(), str(d.get("FileNo") or "").strip())
            if key not in subjects:
                continue                  # the same file number at another factory: not the one that finished
            a = evaluate(rs, key[1], key[0], [d], days=rs.window_days)
            a.days = int(d.get("Days") or 0)
            a.last_day = _day_text(d.get("LastDay"))
            out[key] = a
    return out


def finished(dimension: str, running: set[tuple[str, str]], path: Optional[Any] = None) -> set[tuple[str, str]]:
    """The open subjects the plans no longer carry: the ones `final_windows()` has to read."""
    from .signals import open_predictions
    return {(p["factory"], p["subject"]) for p in open_predictions(dimension, path=path)
            if (p["factory"], p["subject"]) not in running}


def settle(dimension: str, assessments: list[Assessment], running: set[tuple[str, str]],
           newest: Optional[str], path: Optional[Any] = None, *,
           finals: Optional[dict[tuple[str, str], Assessment]] = None) -> int:
    """Record what actually happened for every subject that has finished running. Returns how many settled.

    A finished subject is settled against `finals` — its own last window, ending on its last inspection
    day (`final_windows()`) — and the outcome carries that day. Without an entry there, the window this run
    scored is used when the subject is still in it (the behaviour before 2026-11-03).

    A subject settles only when the plans no longer carry it AND the feed itself has moved on. `newest` is
    the newest inspection day VistaQ holds, never the wall clock — a stopped feed would otherwise make
    every open subject look as though it finished on the same day, and the whole backtest would be built
    on outcomes recorded during an outage.
    """
    from .signals import open_predictions, write_outcome

    if not newest:
        return 0
    final = {(a.factory, a.subject): a for a in assessments}
    finals = finals or {}
    done = 0
    for p in open_predictions(dimension, path=path):
        key = (p["factory"], p["subject"])
        if key in running:
            continue                      # still planned: not finished
        a = finals.get(key) or final.get(key)
        if a is None:
            continue                      # no inspection to settle against; try again tomorrow
        if write_outcome(p["id"], a.level, a.totals, [asdict(m) for m in a.measures], path=path,
                         outcome_day=a.last_day or None):
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


LEVEL_WORD = {"red": "critical", "amber": "watch", "green": "on target", "info": "not enough checked"}
# How a measure is named to a person. The column names are the vocabulary of config/fixed_tools.yaml
# (&vq_measures) and belong in a table header, not in a sentence a factory manager reads.
MEASURE_WORD = {"dhu": "DHU", "reject_pct": "reject rate", "ftp_pct": "first pass",
                "defective_pct": "defective pieces", "rectified_pct": "rectified"}
PERCENT = ("reject_pct", "ftp_pct", "defective_pct", "rectified_pct")


def note_source(a: Assessment) -> Any:
    """The stored level as a citable result.

    Every number the note states is in these rows, so the sentence obeys non-negotiable #2 the same way
    an aggregates answer does: `engine="local"` is the project's existing way to cite a pre-computed copy
    (data/aggregates.py), and the copy here was computed in Python from VistaQ rows, never by the model.
    """
    from ..models import QueryResult

    cols = ["Measure", "Value", "Target", "Critical at", "Level", "Pieces checked", "Days"]
    checked = float(a.totals.get("Checked", 0) or 0)
    rows = [[m.measure, m.value, m.amber, m.red, m.level, checked, a.days] for m in a.measures]
    return QueryResult(
        database="signals.db", engine="local", views=["prediction"],
        sql=f"-- Stage-2 quality risk for {a.subject} ({a.factory}), stored by scripts/watch.py --risk",
        tool="risk", columns=cols, rows=rows, key_columns=["Measure"], agent="quality")


def note(a: Assessment, marker: str = "D1", as_of_day: str = "") -> str:
    """One sentence for an answer about this subject, loaded from prompts/ (never inlined, CLAUDE.md)."""
    from ..config import prompt

    scored = [m for m in a.measures if m.value is not None and m.enough]
    if scored:
        detail = ", ".join(
            f"{MEASURE_WORD.get(m.measure, m.measure)} {m.value}{'%' if m.measure in PERCENT else ''}"
            f" against a target of {m.amber}{'%' if m.measure in PERCENT else ''}"
            for m in scored)
    else:
        detail = (f"only {float(a.totals.get('Checked', 0) or 0):,.0f} pieces were checked, too few to judge")
    return prompt("risk_note").strip().format(
        subject=a.subject, level_word=LEVEL_WORD.get(a.level, a.level), detail=detail,
        window_days=a.window_days, as_of_day=as_of_day or "the latest inspection day",
        marker=f"[{marker}]", caveat=a.caveat).strip()


def for_subjects(subjects: set[str], scope: Any, path: Optional[Any] = None) -> dict[str, Assessment]:
    """The newest stored level for each named subject the viewer may see. {} when nothing is stored yet,
    so an answer is never blocked by the absence of a risk run."""
    from .signals import risk_latest

    try:
        _run, preds = risk_latest(scope, "quality", path=path)
    except Exception:
        return {}
    return {a.subject: a for a in preds if a.subject in subjects}
