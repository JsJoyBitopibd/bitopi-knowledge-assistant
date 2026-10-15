"""Stage-2 backtest (gate M✓): the stored daily risk levels compared with what actually happened.

Read-only over data/index/signals.db (`risk_run` + `prediction`, Phase M4). Nothing here writes to the
store, to a production database or to the model: `load()` opens the file read-only, `compute()`,
`verdict()` and `render_markdown()` are pure, and scripts/backtest.py writes the report to logs/.

The order of the steps in compute() is part of the method, not an implementation detail:

1. One level per day. A day with several runs (14 Oct 2026 had four, run by hand) keeps only its LAST
   run; the daily series is what `watch.py --risk-daily` produces from then on.
2. No lead time, no evidence. A settled row whose outcome was written by the run that stored it (the
   latest run started at or before `outcome_at` is the row's own run) recorded the present, not a
   forecast. Runs 1 and 2 did exactly that for seven files each, before `risk.predictable()` existed.
   Those rows go first, and are counted.
3. Only then, files whose outcome was not final: settled more than once (a paused file that resumed gets
   a SECOND settled row with new ids — `revised` never rises in production), `revised > 0`, or predicted
   again after the outcome was written. The whole file is left out, and counted. Doing this before step 2
   would have counted the seven files of 14 Oct as paused-and-resumed.
4. The claim the levels make is about the DHU target, so the outcome is read from the `dhu` measure
   stored with it — not from `outcome_level`, which is the worse of DHU and reject rate. `enough` false
   means too few pieces were checked at the end: neither a hit nor a miss.
5. Every kept daily prediction of a usable file is joined to that file's outcome by days ahead. The
   settled row alone would only ever say what the level was the day before the file finished.
"""
from __future__ import annotations

import json
import math
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from . import signals
from .risk import ratio

LEVELS = ("red", "amber", "green", "info")
OVER = ("amber", "red")                  # an outcome level that is over a target

# Minimum evidence before any fact about the levels is stated. Constants on purpose, with no command-line
# override: a gate whose bar can be lowered from the shell on the day it is read is not a gate.
MIN_DATES = 28                           # dates in the daily series
MIN_FILES = 60                           # usable settled files whose DHU is known at the end
MIN_PER_LEVEL = 15                       # judged files at each of red, amber, green a window or more ahead

INSUFFICIENT = "INSUFFICIENT EVIDENCE"
SUFFICIENT = "SUFFICIENT EVIDENCE"

# Days-ahead buckets of the lead-time grid: (lowest, highest or None for open-ended).
BUCKETS: tuple[tuple[int, Optional[int]], ...] = ((1, 7), (8, 13), (14, 20), (21, 27), (28, None))
Z95 = 1.959963984540054

Key = tuple[str, str]                    # (factory, subject): the same file can run in two factories


# ---------------------------------------------------------------- reading the store
_RUN_SQL = ("SELECT id, started, finished, dimension, predictions, errors FROM risk_run "
            "WHERE dimension = ? ORDER BY started, id")
_ROW_SQL = ("SELECT id, run_id, dimension, subject, factory, level, window_days, days, totals, measures, "
            "caveat, database, views, as_of, outcome_at, outcome_level, outcome_totals, outcome_measures, "
            "revised FROM prediction WHERE dimension = ? ORDER BY id")


def _json(raw: Optional[str], default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _run_dict(r: tuple) -> dict[str, Any]:
    return dict(id=int(r[0]), started=r[1], finished=r[2], dimension=r[3], predictions=int(r[4] or 0),
                errors=_json(r[5], []))


def _row_dict(r: tuple) -> dict[str, Any]:
    return dict(id=int(r[0]), run_id=int(r[1]), dimension=r[2], subject=r[3], factory=r[4], level=r[5],
                window_days=int(r[6] or 7), days=int(r[7] or 0), totals=_json(r[8], {}),
                measures=_json(r[9], []), caveat=r[10] or "", database=r[11] or "", views=_json(r[12], []),
                as_of=r[13], outcome_at=r[14], outcome_level=r[15], outcome_totals=_json(r[16], {}),
                outcome_measures=_json(r[17], []), revised=int(r[18] or 0))


def load(path: Optional[Path] = None, dimension: str = "quality",
         until: Optional[date] = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(runs, rows) for one dimension, read-only. `until` ignores runs started and outcomes written after
    that date, so an earlier baseline can be reproduced from today's store.

    Raises FileNotFoundError when there is no store; ([], []) when the store has no risk tables yet.
    """
    path = Path(path) if path else signals.db_path()
    if not path.exists():
        raise FileNotFoundError(str(path))
    con = signals._connect(path, readonly=True)
    try:
        runs = [_run_dict(r) for r in con.execute(_RUN_SQL, (dimension,)).fetchall()]
        rows = [_row_dict(r) for r in con.execute(_ROW_SQL, (dimension,)).fetchall()]
    except sqlite3.OperationalError as e:
        if "no such table" in str(e):
            return [], []                  # a store from before Phase M: nothing to backtest yet
        raise
    finally:
        con.close()
    return cut(runs, rows, until) if until else (runs, rows)


def cut(runs: list[dict[str, Any]], rows: list[dict[str, Any]],
        until: date) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The store as it stood at the end of `until`. Pure.

    An outcome written after the cut reads as not yet written. A revision written after the cut cannot be
    undone (the store keeps only the latest outcome), so such a row reads as open too.
    """
    keep = [r for r in runs if _day(r["started"]) <= until]
    ids = {r["id"] for r in keep}
    out = []
    for p in rows:
        if p["run_id"] not in ids:
            continue
        if p.get("outcome_at") and _day(p["outcome_at"]) > until:
            p = {**p, "outcome_at": None, "outcome_level": None, "outcome_totals": {},
                 "outcome_measures": [], "revised": 0}
        out.append(p)
    return keep, out


def dhu_rule(dimension: str = "quality") -> Optional[dict[str, Any]]:
    """The dimension's DHU rule from config/rules/, for outcomes stored without measures. None if absent."""
    from .risk import load_rule_sets

    try:
        rs = load_rule_sets().get(dimension)
    except Exception:
        return None
    if rs is None:
        return None
    return next((r for r in rs.rules if r.get("measure") == "dhu"), None)


# ---------------------------------------------------------------- the result
def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _day(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def _key(p: dict[str, Any]) -> Key:
    return (str(p["factory"]), str(p["subject"]))


def wilson(k: int, n: int, z: float = Z95) -> Optional[tuple[float, float]]:
    """95% Wilson score interval for k of n. None when n is zero."""
    if n <= 0:
        return None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class FileOutcome:
    """One usable settled file: what happened at the end, and every daily level stored before it."""
    factory: str
    subject: str
    outcome_at: str
    outcome_level: str
    over: Optional[bool]              # over the DHU target at the end; None = neither hit nor miss
    why: str = ""                     # "too_few" or "unknown" when `over` is None
    dhu: Optional[float] = None
    amber: Optional[float] = None
    recomputed: bool = False          # DHU recomputed from outcome_totals: the outcome stored no measures
    settled_level: str = ""           # the last daily level: the row the outcome was written onto
    settled_ahead: int = 0
    predictions: list[tuple[int, str]] = field(default_factory=list)   # (days ahead, level), kept runs only
    headline_level: Optional[str] = None
    headline_ahead: Optional[int] = None


@dataclass
class Tally:
    """One row of a hit-rate table."""
    label: str
    files: int = 0
    judged: int = 0                   # DHU known at the end
    over: int = 0                     # over the DHU target at the end
    either: int = 0                   # over either target at the end (outcome_level amber or red)
    too_few: int = 0                  # too few checked at the end: neither hit nor miss
    unknown: int = 0                  # no DHU measure and nothing to recompute it from

    def add(self, f: FileOutcome) -> None:
        self.files += 1
        if f.over is None:
            if f.why == "too_few":
                self.too_few += 1
            else:
                self.unknown += 1
            return
        self.judged += 1
        self.over += int(f.over)
        self.either += int(f.outcome_level in OVER)

    @property
    def share(self) -> Optional[float]:
        return self.over / self.judged if self.judged else None

    @property
    def either_share(self) -> Optional[float]:
        return self.either / self.judged if self.judged else None

    @property
    def interval(self) -> Optional[tuple[float, float]]:
        return wilson(self.over, self.judged)


@dataclass
class Result:
    dimension: str
    window_days: int = 7
    runs: list[dict[str, Any]] = field(default_factory=list)          # every run read, by start time
    by_date: dict[str, list[int]] = field(default_factory=dict)       # date -> run ids; the last is kept
    kept: list[int] = field(default_factory=list)
    missing_dates: list[str] = field(default_factory=list)            # no run at all that day
    run_errors: list[tuple[int, str, list[str]]] = field(default_factory=list)
    predictions: int = 0                                              # rows read
    settled_rows: int = 0
    same_pass_rows: int = 0                                           # settled by the run that stored them
    same_pass_files: int = 0
    paused: list[Key] = field(default_factory=list)                   # settled more than once
    revised: list[Key] = field(default_factory=list)
    resumed: list[Key] = field(default_factory=list)                  # predicted again after the outcome
    open_files: int = 0
    reopened: int = 0                                                 # open again after a no-lead-time settle
    stuck: list[tuple[str, str, str]] = field(default_factory=list)  # open, not in the newest run
    not_ahead: int = 0                                                # kept predictions not before the outcome
    files: list[FileOutcome] = field(default_factory=list)
    headline: dict[str, Tally] = field(default_factory=dict)
    headline_base: Tally = field(default_factory=lambda: Tally("all levels (base rate)"))
    last_daily: dict[str, Tally] = field(default_factory=dict)
    last_daily_base: Tally = field(default_factory=lambda: Tally("all levels (base rate)"))
    by_factory: dict[str, tuple[dict[str, Tally], Tally]] = field(default_factory=dict)
    lead: dict[str, dict[str, list[int]]] = field(default_factory=dict)   # bucket -> level -> [over, judged]
    store: str = ""                                                   # set by the caller, for the report
    until: Optional[date] = None

    @property
    def too_few(self) -> int:
        return sum(1 for f in self.files if f.over is None and f.why == "too_few")

    @property
    def unknown(self) -> int:
        return sum(1 for f in self.files if f.over is None and f.why != "too_few")

    @property
    def recomputed(self) -> int:
        return sum(1 for f in self.files if f.recomputed)

    @property
    def judged(self) -> int:
        return sum(1 for f in self.files if f.over is not None)

    @property
    def no_headline(self) -> int:
        return sum(1 for f in self.files if f.headline_level is None)


# ---------------------------------------------------------------- compute: the five steps
def _gaps(days: list[str]) -> list[str]:
    """Dates between the first and last run date on which no run started at all."""
    if not days:
        return []
    have, out = set(days), []
    d, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    while d <= last:
        if d.isoformat() not in have:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def settling_run(outcome_at: str, starts: list[tuple[datetime, int]]) -> Optional[int]:
    """The run that wrote an outcome: the latest run started at or before `outcome_at`.
    `starts` is [(started, run id)] sorted by start time."""
    i = bisect_right([s for s, _ in starts], _ts(outcome_at))
    return starts[i - 1][1] if i else None


def dhu_outcome(p: dict[str, Any], rule: Optional[dict[str, Any]] = None
                ) -> tuple[Optional[bool], str, Optional[float], Optional[float], bool]:
    """(over, why, dhu, amber, recomputed) for one settled row.

    Over = the DHU measure stored with the outcome is at or above the amber stored with it — VistaQ's own
    target, which is the same thing as that measure scoring amber or red. When the outcome stored no
    measures, DHU is recomputed from its totals against the rule's amber (or the amber the prediction
    itself carried) and the row is flagged.
    """
    m = next((m for m in p.get("outcome_measures") or [] if m.get("measure") == "dhu"), None)
    if m is not None:
        value, amber = m.get("value"), m.get("amber")
        if not m.get("enough", True) or value is None:
            return None, "too_few", value, amber, False
        if amber is None:
            return None, "unknown", value, None, False
        return float(value) >= float(amber), "", float(value), float(amber), False

    totals = p.get("outcome_totals") or {}
    amber = (rule or {}).get("amber")
    if amber is None:
        stored = next((m for m in p.get("measures") or [] if m.get("measure") == "dhu"), None)
        amber = (stored or {}).get("amber")
    if not totals:
        return None, "unknown", None, amber, True
    value = ratio("dhu", totals)
    need = float((rule or {}).get("min_checked") or 0)
    if value is None or float(totals.get("Checked") or 0) < need:
        return None, "too_few", value, amber, True
    if amber is None:
        return None, "unknown", value, None, True
    return value >= float(amber), "", value, float(amber), True


def _file(p: dict[str, Any], daily: list[dict[str, Any]], rule: Optional[dict[str, Any]]
          ) -> tuple[FileOutcome, int]:
    """A usable file joined to every kept daily prediction of it. Returns (file, predictions not ahead)."""
    over, why, value, amber, recomputed = dhu_outcome(p, rule)
    end = _day(p["outcome_at"])
    joined = sorted(((end - _day(q["as_of"])).days, str(q["level"]), int(q.get("window_days") or 7))
                    for q in daily)
    ahead = [(d, lvl, w) for d, lvl, w in joined if d >= 1]
    f = FileOutcome(factory=str(p["factory"]), subject=str(p["subject"]), outcome_at=p["outcome_at"],
                    outcome_level=str(p.get("outcome_level") or ""), over=over, why=why, dhu=value,
                    amber=amber, recomputed=recomputed, settled_level=str(p["level"]),
                    settled_ahead=(end - _day(p["as_of"])).days,
                    predictions=[(d, lvl) for d, lvl, _w in sorted(ahead, reverse=True)])
    window = [(d, lvl) for d, lvl, w in ahead if d >= w]
    if window:
        f.headline_ahead, f.headline_level = min(window)       # the closest to one window ahead, never under
    return f, len(joined) - len(ahead)


def _tables(files: list[FileOutcome], level_of: Callable[[FileOutcome], Optional[str]]
            ) -> tuple[dict[str, Tally], Tally]:
    rows = {lvl: Tally(lvl) for lvl in LEVELS}
    base = Tally("all levels (base rate)")
    for f in files:
        lvl = level_of(f)
        if lvl is None:
            continue
        rows.setdefault(lvl, Tally(lvl)).add(f)
        base.add(f)
    return rows, base


def bucket(days: int) -> str:
    for lo, hi in BUCKETS:
        if days >= lo and (hi is None or days <= hi):
            return f"{lo}+" if hi is None else f"{lo}–{hi}"
    return "under 1"


def _lead(files: list[FileOutcome]) -> dict[str, dict[str, list[int]]]:
    grid = {bucket(lo): {lvl: [0, 0] for lvl in LEVELS} for lo, _hi in BUCKETS}
    for f in files:
        if f.over is None:
            continue                     # neither a hit nor a miss at any lead time
        for d, lvl in f.predictions:
            cell = grid[bucket(d)].setdefault(lvl, [0, 0])
            cell[0] += int(f.over)
            cell[1] += 1
    return grid


def compute(runs: list[dict[str, Any]], rows: list[dict[str, Any]], dimension: str = "quality",
            rule: Optional[dict[str, Any]] = None) -> Result:
    """The backtest over (runs, rows) as load() returns them. Pure: no store, no clock, no settings.
    `rule` is the dimension's DHU rule, used only for an outcome stored without measures."""
    runs = sorted((r for r in runs if r.get("dimension", dimension) == dimension),
                  key=lambda r: (_ts(r["started"]), r["id"]))
    rows = [p for p in rows if p.get("dimension", dimension) == dimension]
    windows = [int(p.get("window_days") or 7) for p in rows]
    res = Result(dimension=dimension, window_days=max(set(windows), key=windows.count) if windows else 7,
                 runs=runs, predictions=len(rows))

    # (1) one level per day: the last run of each calendar date
    for r in runs:
        res.by_date.setdefault(r["started"][:10], []).append(r["id"])
    res.kept = [ids[-1] for ids in res.by_date.values()]
    res.missing_dates = _gaps(sorted(res.by_date))
    res.run_errors = [(r["id"], r["started"], list(r.get("errors") or [])) for r in runs if r.get("errors")]
    kept = set(res.kept)

    # (2) rows settled by the run that stored them: no lead time
    settled = [p for p in rows if p.get("outcome_at")]
    res.settled_rows = len(settled)
    starts = [(_ts(r["started"]), r["id"]) for r in runs]
    remaining: dict[Key, list[dict[str, Any]]] = {}
    same: set[Key] = set()
    for p in settled:
        if settling_run(p["outcome_at"], starts) == p["run_id"]:
            res.same_pass_rows += 1
            same.add(_key(p))
        else:
            remaining.setdefault(_key(p), []).append(p)
    res.same_pass_files = len(same)

    # (3) only now: files whose outcome was not final
    every: dict[Key, list[dict[str, Any]]] = {}
    for p in rows:
        every.setdefault(_key(p), []).append(p)
    usable: dict[Key, dict[str, Any]] = {}
    for key, ps in sorted(remaining.items()):
        if len(ps) > 1:
            res.paused.append(key)
            continue
        p = ps[0]
        if p.get("revised", 0) > 0:
            res.revised.append(key)
            continue
        if any(_ts(q["as_of"]) > _ts(p["outcome_at"]) for q in every[key]):
            res.resumed.append(key)
            continue
        usable[key] = p

    # Files with no outcome yet: no settled row left after step 2, and a level still unsettled. That includes
    # a file settled only by the run that stored it which is running again (four of the seven of 14 Oct were
    # back in production on 15 Oct). Stuck = open, but the newest run no longer predicts it.
    newest = runs[-1]["id"] if runs else None
    in_newest = {_key(p) for p in rows if p["run_id"] == newest}
    for key in sorted(every):
        if key in remaining or all(q.get("outcome_at") for q in every[key]):
            continue
        res.open_files += 1
        res.reopened += int(key in same)
        if key not in in_newest:
            res.stuck.append((key[0], key[1], max(q["as_of"] for q in every[key])))

    # (4) the outcome on DHU, and (5) every kept daily prediction joined to it
    for key, p in usable.items():
        daily = [q for q in every[key] if q["run_id"] in kept]
        f, not_ahead = _file(p, daily, rule)
        res.not_ahead += not_ahead
        res.files.append(f)

    res.headline, res.headline_base = _tables(res.files, lambda f: f.headline_level)
    res.last_daily, res.last_daily_base = _tables(res.files, lambda f: f.settled_level)
    for factory in sorted({f.factory for f in res.files}):
        res.by_factory[factory] = _tables([f for f in res.files if f.factory == factory],
                                          lambda f: f.headline_level)
    res.lead = _lead(res.files)
    return res


# ---------------------------------------------------------------- the verdict: facts, never a judgement
@dataclass
class Verdict:
    status: str
    sufficient: bool
    shortfalls: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    ordered: Optional[bool] = None
    separated: Optional[bool] = None
    lift: Optional[float] = None          # red share minus the base rate, in percentage points


def verdict(result: Result) -> Verdict:
    """INSUFFICIENT EVIDENCE with every shortfall, or three mechanical facts about the headline: ordered,
    separated, lift. Whether those facts are good enough is for a person to decide, so no wording here
    says it is."""
    w = result.window_days
    short = []
    dates = len(result.kept)
    if dates < MIN_DATES:
        short.append(f"daily series: {dates} date(s), {MIN_DATES} needed")
    if result.judged < MIN_FILES:
        short.append(f"usable settled files with DHU known at the end: {result.judged}, {MIN_FILES} needed")
    for lvl in ("red", "amber", "green"):
        n = result.headline.get(lvl, Tally(lvl)).judged
        if n < MIN_PER_LEVEL:
            short.append(f"{lvl} files judged {w} or more days ahead: {n}, {MIN_PER_LEVEL} needed")
    if short:
        return Verdict(status=INSUFFICIENT, sufficient=False, shortfalls=short)

    red, amber, green = (result.headline[lvl] for lvl in ("red", "amber", "green"))
    rs, as_, gs = red.share or 0.0, amber.share or 0.0, green.share or 0.0
    ordered = rs > as_ > gs
    r_lo, g_hi = (red.interval or (0.0, 0.0))[0], (green.interval or (0.0, 0.0))[1]
    separated = r_lo > g_hi
    base = result.headline_base.share or 0.0
    lift = round(100 * (rs - base), 1)
    facts = [
        f"ordered: {'yes' if ordered else 'no'} — share over the DHU target, red {_pct(rs)}, amber {_pct(as_)}, "
        f"green {_pct(gs)} (red > amber > green {'holds' if ordered else 'does not hold'})",
        f"separated: {'yes' if separated else 'no'} — red's 95% interval starts at {_pct(r_lo)}, "
        f"green's ends at {_pct(g_hi)}",
        f"lift: red is {lift:+.1f} points against the base rate of {_pct(base)}",
    ]
    return Verdict(status=SUFFICIENT, sufficient=True, facts=facts, ordered=ordered, separated=separated,
                   lift=lift)


def verdict_lines(v: Verdict) -> list[str]:
    """The verdict block, as the script prints it and the report opens with it."""
    out = [v.status]
    out += [f"  - {s}" for s in (v.shortfalls if not v.sufficient else v.facts)]
    out.append("No fact about the levels is stated until every minimum is met." if not v.sufficient else
               "These are measurements of the stored levels; reading them against the gate is a person's call.")
    return out


# ---------------------------------------------------------------- the report
def _pct(x: Optional[float]) -> str:
    return "–" if x is None else f"{100 * x:.1f}%"


def _ci(t: Tally) -> str:
    ci = t.interval
    return "–" if ci is None else f"{100 * ci[0]:.1f}–{100 * ci[1]:.1f}%"


def _date(s: str) -> str:
    d = _day(s)
    return f"{d.day} {d:%b %Y}"


def _when(s: str) -> str:
    t = _ts(s)
    return f"{t.day} {t:%b %Y %H:%M}"


def _files(keys: list[Key], limit: int = 30) -> str:
    if not keys:
        return ""
    shown = ", ".join(f"{s} ({f})" for f, s in keys[:limit])
    return shown + (f", and {len(keys) - limit} more" if len(keys) > limit else "")


_HEAD = ("| Level | Files | Judged | Over DHU target | Share | 95% interval | Over either target "
         "| Too few checked at the end |")
_RULE = "|---|---:|---:|---:|---:|---|---:|---:|"


def _row(label: str, t: Tally) -> str:
    return (f"| {label} | {t.files} | {t.judged} | {t.over} | {_pct(t.share)} | {_ci(t)} | "
            f"{t.either} ({_pct(t.either_share)}) | {t.too_few} |")


def _table(levels: dict[str, Tally], base: Tally) -> list[str]:
    out = [_HEAD, _RULE]
    out += [_row(lvl, levels[lvl]) for lvl in LEVELS if lvl in levels]
    out += [_row(f"**{base.label}**", base)]
    return out


def render_markdown(result: Result, v: Verdict, generated: datetime) -> str:
    """The report. Pure. File numbers appear in it: it goes to logs/, which git ignores."""
    r, w = result, result.window_days
    L: list[str] = [f"# Stage-2 backtest — {r.dimension}", "",
                    f"Generated {generated.day} {generated:%b %Y %H:%M}"
                    + (f" · store {r.store}" if r.store else "")
                    + (f" · cut at the end of {_date(r.until.isoformat())}" if r.until else ""), ""]

    # -- verdict
    L += ["## Verdict", "", "```", *verdict_lines(v), "```", ""]

    # -- what was read
    L += ["## What was read", ""]
    if r.runs:
        L.append(f"{len(r.runs)} run(s) from {_when(r.runs[0]['started'])} to {_when(r.runs[-1]['started'])}; "
                 f"{len(r.kept)} kept, one per day — the last run of the day is that day's level.")
    else:
        L.append("No run.")
    L += ["", "| Day | Runs | Kept (last of the day) | Dropped |", "|---|---:|---|---|"]
    starts = {x["id"]: x for x in r.runs}
    for day, ids in r.by_date.items():
        fmt = (lambda i: f"run {i} ({_ts(starts[i]['started']):%H:%M}, {starts[i]['predictions']} levels)")
        L.append(f"| {_date(day)} | {len(ids)} | {fmt(ids[-1])} | "
                 f"{', '.join(fmt(i) for i in ids[:-1]) or '–'} |")
    L += ["", f"Days with no run: {', '.join(_date(d) for d in r.missing_dates) or 'none'}.", ""]
    if r.run_errors:
        L.append("Runs with errors:")
        L += [f"- run {i} ({_when(s)}): {'; '.join(errs)}" for i, s, errs in r.run_errors]
    else:
        L.append("Runs with errors: none.")
    L += ["", f"{r.predictions} stored level(s) read, {r.settled_rows} of them carrying an outcome.", "",
          "| Counted | Number | What it means |", "|---|---:|---|",
          f"| Settled by the run that stored them | {r.same_pass_rows} rows ({r.same_pass_files} files) | "
          "no lead time: a record of the present, left out first |",
          f"| Settled more than once | {len(r.paused)} files | paused and resumed; the whole file is left out |",
          f"| Outcome revised after it was written | {len(r.revised)} files | the whole file is left out |",
          f"| Predicted again after its outcome | {len(r.resumed)} files | resumed and not settled again yet; "
          "the whole file is left out |",
          f"| Usable settled files | {len(r.files)} | {r.judged} with DHU known at the end |",
          f"| Too few checked at the end | {r.too_few} files | neither a hit nor a miss |",
          f"| No DHU at the end | {r.unknown} files | no measure and nothing to recompute it from |",
          f"| DHU recomputed from the totals | {r.recomputed} files | the outcome stored no measures; still used |",
          f"| No level {w} or more days ahead | {r.no_headline} files | settled too soon after its first level; "
          "not in the headline |",
          f"| Levels not ahead of their outcome | {r.not_ahead} | stored on or after the outcome day; not used |",
          f"| Files still open | {r.open_files} | no outcome yet"
          + (f"; {r.reopened} of them settled earlier by the run that stored them and running again"
             if r.reopened else "") + " |",
          f"| Open but no longer predicted (stuck) | {len(r.stuck)} | not in the newest run, never settled |",
          ""]
    lists = [f"- {label}: {_files(keys)}" for label, keys in (
        ("Settled more than once", r.paused), ("Revised", r.revised),
        ("Predicted again after its outcome", r.resumed)) if keys]
    if r.stuck:
        lists.append("- Stuck (last level stored): "
                     + ", ".join(f"{s} ({f}, {_date(a)})" for f, s, a in r.stuck[:30])
                     + (f", and {len(r.stuck) - 30} more" if len(r.stuck) > 30 else ""))
    L += lists + ([""] if lists else [])

    # -- headline
    L += [f"## Headline: the level {w} or more days ahead", "",
          f"Per file, the level stored closest to {w} days before the file finished, and never closer. "
          "Share = files over the DHU target at the end / files judged.", "",
          *_table(r.headline, r.headline_base), ""]

    # -- last daily level
    ahead: dict[int, int] = {}
    for f in r.files:
        ahead[f.settled_ahead] = ahead.get(f.settled_ahead, 0) + 1
    L += ["## Last daily level", "",
          "The level on the row the outcome was written onto: the file's last stored level, made just before "
          "it finished. It describes the end more than it forecasts it.", "",
          *_table(r.last_daily, r.last_daily_base), "",
          "Days between the last daily level and the outcome: "
          + (", ".join(f"{d} day(s) for {n} file(s)" for d, n in sorted(ahead.items())) or "no file") + ".", ""]

    # -- by factory
    L += ["## By factory", "", f"The headline ({w} or more days ahead), split by the file's factory.", ""]
    if r.by_factory:
        L += ["| Factory " + _HEAD, "|---" + _RULE]
        for factory, (levels, base) in r.by_factory.items():
            L += [f"| {factory} " + _row(lvl, levels[lvl]) for lvl in LEVELS if levels[lvl].files]
            L += [f"| {factory} " + _row(f"**{base.label}**", base)]
    else:
        L.append("No usable settled file yet.")
    L.append("")

    # -- lead time
    L += ["## Lead time", "",
          "Every kept daily level of a usable file, joined to that file's outcome. Cell = over the DHU target "
          "at the end / levels judged (share).", "",
          "| Days ahead | " + " | ".join(LEVELS) + " | Note |", "|---|" + "---:|" * len(LEVELS) + "---|"]
    for lo, _hi in BUCKETS:
        b = bucket(lo)
        cells = []
        for lvl in LEVELS:
            k, n = r.lead.get(b, {}).get(lvl, [0, 0])
            cells.append(f"{k}/{n} ({_pct(k / n)})" if n else "–")
        note = "window overlaps outcome" if lo < w else ""
        L.append(f"| {b} | " + " | ".join(cells) + f" | {note} |")
    L.append("")

    # -- method notes
    L += ["## Method notes", "",
          "- Source: the `risk_run` and `prediction` tables of signals.db, opened read-only. Nothing is written.",
          "- One level per day: when a day has more than one run, only its last run counts; the others are "
          "listed above as dropped.",
          "- Settled by the run that stored it: the run that wrote an outcome is the latest run started at or "
          "before `outcome_at`. When that is the row's own run, the level had no lead time, so the row is left "
          "out — and this is done before looking for files settled more than once, or the files of 14 Oct "
          "2026 would read as paused and resumed.",
          "- Not final: a file settled more than once, a row with `revised` above zero, or a file predicted "
          "again after its outcome was written is left out whole.",
          "- Over the DHU target: the `dhu` measure stored with the outcome, at or above the amber stored on "
          "the same row (VistaQ's own target). The outcome's level is the worse of DHU and reject rate, so it "
          "is reported apart, as *over either target*.",
          "- The outcome is the settling run's own window: the file's final "
          f"{w} days, scored by the same rules as the levels.",
          "- Too few checked at the end: the final window checked fewer pieces than the rule needs to judge. "
          "Such a file is neither a hit nor a miss and is in no share's denominator.",
          "- Recomputed: an outcome stored without measures is judged on DHU recomputed from its totals "
          "(100 × Defects / Checked) against the rule's amber, and counted above.",
          "- Days ahead = outcome date − date the level was stored. A level fewer than "
          f"{w} days ahead shares days with the outcome window, so it partly describes the outcome.",
          "- 95% interval: Wilson score interval on over / judged.",
          f"- Minimum evidence before any fact is stated: {MIN_DATES} dates in the daily series, {MIN_FILES} "
          f"usable settled files with DHU known at the end, and {MIN_PER_LEVEL} judged files each at red, amber "
          f"and green {w} or more days ahead. Constants in the code, with no command-line override.",
          "- Base rate: every file in the table, whatever its level."]
    if r.until:
        L.append(f"- Cut at the end of {_date(r.until.isoformat())}: runs started later are ignored and outcomes "
                 "written later read as not yet written. A revision written after the cut cannot be undone from "
                 "the store, so such a row reads as open.")
    L.append("")
    return "\n".join(L)
