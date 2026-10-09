"""How much of the day's provider budget is spent (Phase P0).

The free Gemini tier allows 500 requests per day per model, and this deployment uses one model id for
routing, SQL, answers and the eval judge, so every call draws on one bucket. Nothing used to show how
much was left: an eval run could spend a third of the day before staff asked their first question.
`calls_today()` counts today's rows in logs/calls.csv (every ChatModel call writes one, llm/base.py);
the admin caption shows it, and scripts/eval.py --budget refuses a run that would overspend.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Optional

from .config import log_dir, settings


def calls_today(folder: Optional[Path] = None, today: Optional[date] = None) -> int:
    """Rows in logs/calls.csv (and today's rotated copies) stamped with today's date."""
    folder = folder or log_dir()
    prefix = (today or date.today()).isoformat()
    n = 0
    for f in sorted(folder.glob("calls*.csv")):
        if f.name != "calls.csv" and not f.name.startswith("calls."):
            continue
        try:
            with open(f, newline="", encoding="utf-8") as fh:
                n += sum(1 for r in csv.DictReader(fh) if (r.get("ts") or "").startswith(prefix))
        except OSError:
            continue
    return n


def daily_budget() -> int:
    return int(settings().get("llm.daily_budget", 500))


def gauge(folder: Optional[Path] = None) -> str:
    """'calls today 123/500' for the admin caption."""
    return f"calls today {calls_today(folder)}/{daily_budget()}"


def can_spend(planned: int, folder: Optional[Path] = None) -> tuple[bool, str]:
    """Whether `planned` more calls fit in today's budget, with the reason when they do not."""
    used, budget = calls_today(folder), daily_budget()
    if used + planned > budget:
        return False, f"{used} calls already made today; {planned} more would exceed the budget of {budget}"
    return True, f"{used} calls made today; {planned} planned of {budget}"
