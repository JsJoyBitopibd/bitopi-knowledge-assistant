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

from dataclasses import dataclass, field
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
