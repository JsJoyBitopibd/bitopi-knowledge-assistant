"""P0: the day's provider-call budget is visible and the eval refuses to overspend it.

The provider's day runs from midnight Pacific — 13:00 in Dhaka under US daylight time, 14:00 after it ends
(1 Nov 2026) — not from midnight in Dhaka. Every time below is Dhaka time, as the log stamps are.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from ragbot.logs import append_row
from ragbot.llm.base import CALLS_HEADER
from ragbot.quota import calls_today, can_spend, gauge, window_start

DHAKA = ZoneInfo("Asia/Dhaka")


def _at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=DHAKA)


def _row(ts, purpose="sql"):
    return [ts, "u", purpose, "fake", "m", 10, 5, "1.0", "stop", 0, 0, "", "rid"]


def test_the_window_opens_at_midnight_pacific_not_midnight_in_dhaka():
    assert window_start(_at(2026, 10, 14, 14, 44)) == datetime(2026, 10, 14, 13, 0)
    assert window_start(_at(2026, 10, 14, 13, 0)) == datetime(2026, 10, 14, 13, 0)
    # a morning belongs to the bucket that opened the afternoon before
    assert window_start(_at(2026, 10, 14, 9, 35)) == datetime(2026, 10, 13, 13, 0)
    assert window_start(_at(2026, 10, 14, 12, 59)) == datetime(2026, 10, 13, 13, 0)


def test_the_window_moves_to_14_00_when_us_daylight_time_ends():
    assert window_start(_at(2026, 11, 2, 15, 0)) == datetime(2026, 11, 2, 14, 0)
    assert window_start(_at(2026, 11, 2, 14, 0)) == datetime(2026, 11, 2, 14, 0)
    assert window_start(_at(2026, 11, 2, 13, 59)) == datetime(2026, 11, 1, 13, 0)
    # daylight time ends at 02:00 on 1 Nov, so that day's midnight was still 13:00 in Dhaka
    assert window_start(_at(2026, 11, 2, 13, 30)) == datetime(2026, 11, 1, 13, 0)


def test_a_morning_counts_the_afternoon_before(tmp_path):
    """The 14 Oct case: the calls made that morning were the tail of the bucket opened at 13:00 on 13 Oct."""
    append_row("calls.csv", CALLS_HEADER, _row("2026-10-13T12:59:59"), folder=tmp_path)      # the bucket before
    append_row("calls.csv", CALLS_HEADER, _row("2026-10-13T16:44:00"), folder=tmp_path)
    append_row("calls.csv", CALLS_HEADER, _row("2026-10-14T09:22:32", "answer"), folder=tmp_path)
    assert calls_today(tmp_path, _at(2026, 10, 14, 9, 35)) == 2


def test_calls_are_counted_since_the_window_opened_across_rotated_files(tmp_path):
    append_row("calls.csv", CALLS_HEADER, _row("2026-10-14T09:22:32", "answer"), folder=tmp_path)
    append_row("calls.csv", CALLS_HEADER, _row("2026-10-14T12:59:59"), folder=tmp_path)
    append_row("calls.csv", CALLS_HEADER, _row("2026-10-14T13:00:00"), folder=tmp_path)      # the new bucket
    append_row("calls.csv", CALLS_HEADER, _row("2026-10-14T16:10:00", "judge"), folder=tmp_path)
    append_row("calls.20261001T000000.csv", CALLS_HEADER, _row("2026-10-14T13:05:00"), folder=tmp_path)
    append_row("calls_other.csv", CALLS_HEADER, _row("2026-10-14T13:05:00"), folder=tmp_path)   # not calls.csv
    assert calls_today(tmp_path, _at(2026, 10, 14, 16, 30)) == 3     # 13:00, 16:10 and the rotated 13:05
    assert calls_today(tmp_path / "missing", _at(2026, 10, 14, 16, 30)) == 0


def test_can_spend_and_gauge(tmp_path, monkeypatch):
    import ragbot.quota as q
    monkeypatch.setattr(q, "daily_budget", lambda: 10)
    monkeypatch.setattr(q, "calls_today", lambda folder=None, now=None: 8)
    now = _at(2026, 10, 14, 14, 44)
    ok, why = can_spend(2, tmp_path, now)
    assert ok and "8 calls" in why and "14 Oct 13:00" in why
    ok, why = can_spend(3, tmp_path, now)
    assert not ok and "exceed" in why
    assert gauge(tmp_path, now) == "calls since 13:00 8/10"
