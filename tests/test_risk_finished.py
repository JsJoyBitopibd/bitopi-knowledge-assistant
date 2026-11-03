"""Phase M, found by the backtest of 3 Nov 2026: files that finished never settled.

"Still running" read "any plan in the last 30 days" while an outcome was read from the run's trailing 7-day
window, so a file stayed "running" for a month after its last plan and, by the time it left the set, nothing
was left in the window to settle it against. 97 files were stuck that way and 1 usable settled file came out
of 21 days. These tests pin the fix: a file is running only while it was planned in the last 3 days before
VistaQ's newest inspection day; a finished file is settled against its own last window; the outcome records
the day the file really finished, and the backtest counts days ahead from it.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest

from ragbot.domain_agents import backtest, risk, signals

SRC = {"database": "VistaQ_Default_RHL", "views": ["rag.vw_PlanQualityDay"], "sql": "SELECT 1"}


def _rs():
    return risk.RuleSet(dimension="quality", window_days=7, red_caveat="Red is provisional.",
                        rules=[dict(name="file_dhu", title="t", measure="dhu", amber=3.85,
                                    amber_source="DHUThreshold", red=6, red_provisional=True,
                                    min_checked=200)])


def _assess(subject="FR-A", factory="TAL", checked=4000, defects=300, last_day=""):
    rows = [dict(InspectionDate="2026-10-14", Checked=checked, Defects=defects, Rejected=0,
                 Passed=checked - defects, DefectivePcs=0, Rectified=0)]
    a = risk.evaluate(_rs(), subject, factory, rows)
    a.last_day = last_day
    return a


class _Reads:
    """Stands in for risk._read: records every statement and answers from a canned table."""

    def __init__(self, cols=(), rows=()):
        self.sql: list[str] = []
        self.cols, self.rows = list(cols), [list(r) for r in rows]

    def __call__(self, sql, cat, *, timeout, tool):
        self.sql.append(sql)
        return self.cols, self.rows, ["rag.vw_PlanQualityDay"]


# ---------------------------------------------------------------- "still running"
def test_running_is_anchored_on_vistaqs_newest_day_with_a_three_day_grace(monkeypatch):
    reads = _Reads(["Factory", "FileNo", "LastPlan"], [["TAL", "FR-A", "2026-11-02"]])
    monkeypatch.setattr(risk, "_read", reads)
    assert risk.still_running(object(), newest="2026-11-03") == {("TAL", "FR-A")}
    sql = reads.sql[0]
    assert "DATEADD(day, -3, CAST('2026-11-03' AS date))" in sql and "GETDATE" not in sql


def test_a_newest_day_that_is_not_a_date_never_reaches_the_sql(monkeypatch):
    reads = _Reads()
    monkeypatch.setattr(risk, "_read", reads)
    with pytest.raises(ValueError):
        risk.still_running(object(), newest="2026-11-03'); DROP TABLE x; --")
    assert reads.sql == []


def test_without_a_newest_day_running_falls_back_to_the_clock_with_the_same_grace(monkeypatch):
    reads = _Reads()
    monkeypatch.setattr(risk, "_read", reads)
    risk.still_running(object())
    assert "DATEADD(day, -3, CAST(GETDATE() AS date))" in reads.sql[0]


@pytest.mark.parametrize("newest, plan, stalled", [
    ("2026-11-03", "2026-11-03", False),
    ("2026-11-03", "2026-10-31", False),     # three days: a Friday plus a holiday
    ("2026-11-03", "2026-10-30", True),      # four days without a single plan anywhere: the plans stopped
    ("2026-11-03", None, True),
    (None, None, False),                     # settle() already does nothing without a newest day
])
def test_a_stalled_plan_feed_is_named_and_stops_settling(newest, plan, stalled):
    why = risk.plan_feed_stalled(newest, plan)
    assert bool(why) is stalled
    if stalled:
        assert "not settling" in why


# ---------------------------------------------------------------- the final window
def test_a_finished_file_is_read_over_its_own_last_week(monkeypatch):
    reads = _Reads(["Factory", "FileNo", "LastDay", "Days", "Checked", "Passed", "DefectivePcs", "Rectified",
                    "Rejected", "Defects"],
                   [["BGL", "FR-25-1508", date(2026, 10, 8), 7, 7826, 7668, 156, 156, 2, 233],
                    ["TAL", "FR-25-1508", date(2026, 10, 1), 3, 900, 880, 20, 20, 0, 30]])
    monkeypatch.setattr(risk, "_read", reads)
    out = risk.final_windows(_rs(), object(), {("BGL", "FR-25-1508")})
    assert list(out) == [("BGL", "FR-25-1508")]             # the TAL file of the same number did not finish
    a = out[("BGL", "FR-25-1508")]
    assert a.last_day == "2026-10-08" and a.days == 7 and a.totals["Checked"] == 7826.0
    assert a.measures[0].value == round(100 * 233 / 7826, 2)  # recomputed from the sums, never averaged
    sql = reads.sql[0]
    assert "FileNo IN ('FR-25-1508')" in sql and "DATEADD(day, -7, LastDay)" in sql


def test_a_file_number_the_safe_pattern_refuses_is_skipped_never_quoted(monkeypatch):
    reads = _Reads(["Factory", "FileNo"], [])
    monkeypatch.setattr(risk, "_read", reads)
    risk.final_windows(_rs(), object(), {("TAL", "FR-A"), ("TAL", "FR-B'); --"), ("TAL", "")})
    assert len(reads.sql) == 1 and "'FR-A'" in reads.sql[0] and "FR-B" not in reads.sql[0]


def test_nothing_to_read_runs_no_query(monkeypatch):
    reads = _Reads()
    monkeypatch.setattr(risk, "_read", reads)
    assert risk.final_windows(_rs(), object(), set()) == {} and reads.sql == []


def test_many_finished_files_are_read_in_batches(monkeypatch):
    reads = _Reads(["Factory", "FileNo"], [])
    monkeypatch.setattr(risk, "_read", reads)
    risk.final_windows(_rs(), object(), {("TAL", f"FR-{i:04d}") for i in range(450)})
    assert len(reads.sql) == 3 and all(s.count("'FR-") <= 200 for s in reads.sql)


def test_collect_keeps_each_subjects_newest_inspection_day(monkeypatch):
    reads = _Reads(["Factory", "FileNo", "Days", "LastDay", "Checked", "Passed", "DefectivePcs", "Rectified",
                    "Rejected", "Defects"], [["TAL", "FR-A", 5, date(2026, 11, 2), 4000, 3900, 0, 0, 0, 100]])
    monkeypatch.setattr(risk, "_read", reads)
    out, errors, _src = risk.collect(_rs(), type("C", (), {"database": "VistaQ_Default_RHL"})())
    assert errors == [] and out[0].last_day == "2026-11-02"


# ---------------------------------------------------------------- settling
def test_a_file_that_left_the_window_settles_against_its_final_window(tmp_path):
    """The stuck case: not planned any more, and the run's trailing window holds none of it."""
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess("FR-A")], datetime.now(), [], source=SRC, path=db)
    assert risk.finished("quality", running=set(), path=db) == {("TAL", "FR-A")}
    final = {("TAL", "FR-A"): _assess("FR-A", defects=100, last_day="2026-10-08")}
    assert risk.settle("quality", [], running=set(), newest="2026-11-03", path=db, finals=final) == 1
    con = sqlite3.connect(db)
    row = con.execute("SELECT outcome_level, outcome_day, outcome_at FROM prediction").fetchone()
    con.close()
    assert row[0] == "green" and row[1] == "2026-10-08" and row[2][:10] == date.today().isoformat()


def test_the_final_window_wins_over_the_runs_trailing_window(tmp_path):
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess("FR-A")], datetime.now(), [], source=SRC, path=db)
    trailing = _assess("FR-A", defects=300, last_day="2026-10-30")     # red: only the tail of the file
    final = {("TAL", "FR-A"): _assess("FR-A", defects=100, last_day="2026-10-30")}
    risk.settle("quality", [trailing], running=set(), newest="2026-11-03", path=db, finals=final)
    assert signals.settled(path=db)[0]["outcome_level"] == "green"


def test_without_a_final_window_the_trailing_window_still_settles_and_dates_it(tmp_path):
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess("FR-A")], datetime.now(), [], source=SRC, path=db)
    a = _assess("FR-A", last_day="2026-11-01")
    assert risk.settle("quality", [a], running=set(), newest="2026-11-03", path=db) == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT outcome_day FROM prediction").fetchone()[0] == "2026-11-01"
    con.close()


def test_a_running_file_is_not_settled_even_with_a_final_window(tmp_path):
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess("FR-A")], datetime.now(), [], source=SRC, path=db)
    final = {("TAL", "FR-A"): _assess("FR-A", last_day="2026-11-02")}
    assert risk.settle("quality", [], running={("TAL", "FR-A")}, newest="2026-11-03", path=db, finals=final) == 0
    assert risk.finished("quality", running={("TAL", "FR-A")}, path=db) == set()


def test_a_store_from_before_the_fix_gains_the_column_and_keeps_its_outcomes(tmp_path):
    db = tmp_path / "signals.db"
    con = sqlite3.connect(db)
    con.executescript(signals._SCHEMA)               # the schema as it was: no outcome_day column
    con.close()
    signals.write_risk_run("quality", [_assess("FR-A"), _assess("FR-B")], datetime.now(), [], source=SRC, path=db)
    risk.settle("quality", [_assess("FR-A")], running={("TAL", "FR-B")}, newest="2026-11-03", path=db)
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(prediction)")}
    con.close()
    assert "outcome_day" in cols and len(signals.settled(path=db)) == 1


def test_the_backtest_reads_a_store_that_has_no_outcome_day_column(tmp_path):
    db = tmp_path / "signals.db"
    con = sqlite3.connect(db)
    con.executescript(signals._SCHEMA)
    con.close()
    runs, rows = backtest.load(db)
    assert runs == [] and rows == []


# ---------------------------------------------------------------- the backtest dates the outcome by the file
def _row(rid, run_id, subject, level, as_of, **outcome):
    base = dict(id=rid, run_id=run_id, dimension="quality", subject=subject, factory="TAL", level=level,
                window_days=7, days=7, totals={"Checked": 4000.0}, measures=[], caveat="",
                database="VistaQ_Default_RHL", views=[], as_of=as_of, outcome_at=None, outcome_level=None,
                outcome_totals={}, outcome_measures=[], revised=0, outcome_day=None)
    base.update(outcome)
    return base


def test_days_ahead_count_from_the_files_last_inspection_not_from_when_it_was_settled():
    d0 = datetime(2026, 10, 14, 0, 5)
    runs = [dict(id=i + 1, started=(d0 + timedelta(days=i)).isoformat(timespec="seconds"),
                 finished=(d0 + timedelta(days=i, seconds=3)).isoformat(timespec="seconds"),
                 dimension="quality", predictions=1, errors=[]) for i in range(21)]
    final = _assess("FR-A", defects=300)                          # red at the end: over the DHU target
    outcome = dict(outcome_at=(d0 + timedelta(days=20, seconds=4)).isoformat(timespec="seconds"),
                   outcome_level=final.level, outcome_totals=final.totals,
                   outcome_measures=[m.__dict__ for m in final.measures], outcome_day="2026-10-24")
    rows = []
    for i in range(14):                                           # levels stored 14 Oct .. 27 Oct
        as_of = (d0 + timedelta(days=i, seconds=3)).isoformat(timespec="seconds")
        rows.append(_row(i + 1, i + 1, "FR-A", "red", as_of, **(outcome if i == 13 else {})))
    r = backtest.compute(runs, rows, "quality")
    f = r.files[0]
    # the file finished on 24 Oct: the levels of 24-27 Oct are not ahead of it, 14-23 Oct are (10 to 1 days)
    assert r.not_ahead == 4
    assert [d for d, _ in f.predictions] == list(range(10, 0, -1))
    assert f.headline_ahead == 7 and f.headline_level == "red"
    assert backtest.end_day(rows[13]) == date(2026, 10, 24)


def test_an_outcome_without_a_day_is_dated_when_it_was_written():
    p = _row(1, 1, "FR-A", "red", "2026-10-14T00:05:03", outcome_at="2026-10-20T00:05:04")
    assert backtest.end_day(p) == date(2026, 10, 20)
