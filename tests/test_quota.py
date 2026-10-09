"""P0: the day's provider-call budget is visible and the eval refuses to overspend it."""
from datetime import date

from ragbot.logs import append_row
from ragbot.llm.base import CALLS_HEADER
from ragbot.quota import calls_today, can_spend, gauge


def _row(ts, purpose="sql"):
    return [ts, "u", purpose, "fake", "m", 10, 5, "1.0", "stop", 0, 0, "", "rid"]


def test_calls_today_counts_only_todays_rows_across_rotated_files(tmp_path):
    today = date(2026, 10, 9)
    append_row("calls.csv", CALLS_HEADER, _row("2026-10-09T08:00:00"), folder=tmp_path)
    append_row("calls.csv", CALLS_HEADER, _row("2026-10-09T09:00:00", "answer"), folder=tmp_path)
    append_row("calls.csv", CALLS_HEADER, _row("2026-10-08T23:59:59"), folder=tmp_path)
    append_row("calls.20261001T000000.csv", CALLS_HEADER, _row("2026-10-09T01:00:00"), folder=tmp_path)
    append_row("calls_other.csv", CALLS_HEADER, _row("2026-10-09T01:00:00"), folder=tmp_path)   # not calls.csv
    assert calls_today(tmp_path, today) == 3
    assert calls_today(tmp_path / "missing", today) == 0


def test_can_spend_and_gauge(tmp_path, monkeypatch):
    import ragbot.quota as q
    monkeypatch.setattr(q, "daily_budget", lambda: 10)
    monkeypatch.setattr(q, "calls_today", lambda folder=None, today=None: 8)
    ok, why = can_spend(2, tmp_path)
    assert ok and "8 calls" in why
    ok, why = can_spend(3, tmp_path)
    assert not ok and "exceed" in why
    assert gauge(tmp_path) == "calls today 8/10"
