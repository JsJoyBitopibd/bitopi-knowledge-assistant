"""How much of the day's provider budget is spent (Phase P0).

The free Gemini tier allows 500 requests per day per model, and this deployment uses one model id for
routing, SQL, answers and the eval judge, so every call draws on one bucket. Nothing used to show how
much was left: an eval run could spend a third of the day before staff asked their first question.
`calls_today()` counts the rows in logs/calls.csv (every ChatModel call writes one, llm/base.py) since
the provider's day began; the admin caption shows it, and scripts/eval.py --budget refuses a run that
would overspend.

**The provider's day is not ours.** Google resets the free tier at midnight Pacific time — 13:00 in
Dhaka while the US keeps daylight time, 14:00 otherwise — so the bucket that is empty at 13:00 is the
one spent by the next morning. Counting by the Dhaka date called a morning "fresh" when it was the tail
of the previous afternoon's bucket: on 2026-10-14 the 500 calls ran out at 09:35 after 163 calls "today"
(338 had been made since 13:00 the day before). Failed calls are counted too; that errs on the side of
refusing a run.
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from .config import log_dir, settings


def window_start(now: Optional[datetime] = None) -> datetime:
    """When the provider's current day began, as a naive time in the zone the log stamps are written in.

    `now` naive = this machine's local time (the container runs with TZ=Asia/Dhaka, as the logs do);
    an aware `now` is kept in its own zone, which is what makes the tests independent of the machine.
    """
    reset_tz = ZoneInfo(str(settings().get("llm.quota_reset_tz", "America/Los_Angeles")))
    local = now if (now is not None and now.tzinfo is not None) else (now or datetime.now()).astimezone()
    midnight = local.astimezone(reset_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(local.tzinfo).replace(tzinfo=None)


def calls_today(folder: Optional[Path] = None, now: Optional[datetime] = None) -> int:
    """Rows in logs/calls.csv (and its rotated copies) made since the provider's day began."""
    folder = folder or log_dir()
    since = window_start(now).isoformat(timespec="seconds")
    n = 0
    for f in sorted(folder.glob("calls*.csv")):
        if f.name != "calls.csv" and not f.name.startswith("calls."):
            continue
        try:
            with open(f, newline="", encoding="utf-8") as fh:
                n += sum(1 for r in csv.DictReader(fh) if (r.get("ts") or "") >= since)
        except OSError:
            continue
    return n


def daily_budget() -> int:
    return int(settings().get("llm.daily_budget", 500))


def gauge(folder: Optional[Path] = None, now: Optional[datetime] = None) -> str:
    """'calls since 13:00 123/500' for the admin caption: the hour says when the bucket was refilled."""
    return f"calls since {window_start(now):%H:%M} {calls_today(folder, now)}/{daily_budget()}"


def can_spend(planned: int, folder: Optional[Path] = None, now: Optional[datetime] = None) -> tuple[bool, str]:
    """Whether `planned` more calls fit in the provider's day, with the reason when they do not."""
    used, budget, since = calls_today(folder, now), daily_budget(), window_start(now)
    if used + planned > budget:
        return False, (f"{used} calls already made since {since:%d %b %H:%M}; {planned} more would exceed "
                       f"the budget of {budget}")
    return True, f"{used} calls made since {since:%d %b %H:%M}; {planned} planned of {budget}"
