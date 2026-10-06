"""Morning brief (Phase L): the health score, the numbers at the top and the attention list, computed in
Python from the viewer's signals (docs/AGENTS_DESIGN.md §4). No model call, no database call."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .watch import Signal

_LEVEL_RANK = {"red": 0, "amber": 1, "green": 2, "info": 3}


@dataclass
class Health:
    score: Optional[int]       # None when no signal carries a level yet
    red: int
    amber: int
    green: int
    how: str                   # the calculation in words, shown under the score


@dataclass
class Brief:
    health: Health
    metrics: list[tuple[str, float]] = field(default_factory=list)   # (label, total over the viewer's factories)
    attention: list[Signal] = field(default_factory=list)
    data_warnings: list[Signal] = field(default_factory=list)       # amber/red checks on the data itself


def health(signals: list[Signal]) -> Health:
    """100 × (1 − (red + 0.5 × amber) / signals with a level). Stage 1 scores the watch signals about the
    factory (one per rule and factory; checks on the data itself are left out); from Stage 2 the same
    formula runs over per-order levels (AGENTS_DESIGN.md §4)."""
    leveled = [s for s in signals if s.kind == "ops" and s.level in ("green", "amber", "red")]
    red = sum(s.level == "red" for s in leveled)
    amber = sum(s.level == "amber" for s in leveled)
    green = len(leveled) - red - amber
    if not leveled:
        return Health(None, 0, 0, 0, "No watch signal has a level yet.")
    score = round(100 * (1 - (red + 0.5 * amber) / len(leveled)))
    how = (f"100 × (1 − ({red} red + 0.5 × {amber} amber) ÷ {len(leveled)} checks) = {score}. "
           "A check is one watch rule for one factory; its level comes from provisional thresholds in "
           "config/agents, to be set with the staff interviews. Checks on the data's age are not counted.")
    return Health(score, red, amber, green, how)


def attention(signals: list[Signal], limit: int = 5) -> list[Signal]:
    """Red before amber; within a level the rule's weight (consequence for shipment), then the larger value."""
    hot = [s for s in signals if s.kind == "ops" and s.level in ("red", "amber")]
    hot.sort(key=lambda s: (_LEVEL_RANK[s.level], -s.weight, -(s.value or 0), s.factory))
    return hot[:limit]


def data_warnings(signals: list[Signal]) -> list[Signal]:
    """Checks on the data itself (kind: data) that are amber or red, worst first."""
    bad = [s for s in signals if s.kind == "data" and s.level in ("red", "amber")]
    return sorted(bad, key=lambda s: (_LEVEL_RANK[s.level], s.rule, s.factory))


def metrics(signals: list[Signal]) -> list[tuple[str, float]]:
    """Rules with a `metric:` label, summed over the viewer's factories, in first-seen order."""
    totals: dict[str, float] = {}
    for s in signals:
        if s.metric and s.value is not None:
            totals[s.metric] = totals.get(s.metric, 0) + s.value
    return list(totals.items())


def build(signals: list[Signal], limit: int = 5) -> Brief:
    return Brief(health=health(signals), metrics=metrics(signals), attention=attention(signals, limit),
                 data_warnings=data_warnings(signals))


def greeting(now_utc: datetime, utc_offset_hours: float = 6) -> str:
    """Good morning / afternoon / evening in factory time (Bangladesh is UTC+6, no daylight saving)."""
    local = now_utc.astimezone(timezone(timedelta(hours=utc_offset_hours)))
    return "Good morning" if local.hour < 12 else "Good afternoon" if local.hour < 17 else "Good evening"


def is_stale(finished_iso: str, now: datetime, max_minutes: int) -> bool:
    try:
        return now - datetime.fromisoformat(finished_iso) > timedelta(minutes=max_minutes)
    except ValueError:
        return True
