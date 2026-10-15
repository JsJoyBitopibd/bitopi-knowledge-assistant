"""Gate M✓: the Stage-2 backtest over the stored daily risk levels (domain_agents/backtest.py).

Most tests build the store's rows in memory with `Store`, which writes them the way watch.py
--risk-daily does: a run's predictions first, then settle() onto the NEWEST unsettled row of each finished
file. The 14 Oct replay, the --until test and the script tests go through the real signals.db code instead
(write_risk_run + risk.settle), moving the wall-clock stamps to simulated days with plain UPDATEs.
"""
import copy
import json
import re
import runpy
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

from ragbot.domain_agents import backtest, risk, signals

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SRC = {"database": "VistaQ_Default_RHL", "views": ["rag.vw_PlanQualityDay"], "sql": "SELECT 1"}
D0 = date(2026, 10, 1)


def _rs():
    """Both production rules, so an outcome's level is the worse of DHU and reject rate as it is live."""
    return risk.RuleSet(dimension="quality", window_days=7, red_caveat="Red is provisional.", rules=[
        dict(name="file_dhu", title="t", measure="dhu", amber=3.85, amber_source="DHUThreshold", red=6,
             red_provisional=True, min_checked=200),
        dict(name="file_reject_pct", title="t", measure="reject_pct", amber=1.0,
             amber_source="RejectRateThreshold", red=2, red_provisional=True, min_checked=200)])


def _final(subject="FR-A", factory="TAL", checked=4000, defects=100, rejected=0):
    rows = [dict(InspectionDate="2026-10-14", Checked=checked, Defects=defects, Rejected=rejected,
                 Passed=checked - defects, DefectivePcs=defects, Rectified=0)]
    return risk.evaluate(_rs(), subject, factory, rows)


UNDER = dict(defects=100)                     # DHU 2.5: under the 3.85 target
OVER = dict(defects=200)                      # DHU 5.0: over it (amber)
REJECTS = dict(defects=100, rejected=100)     # DHU 2.5, reject rate 2.5%: red, but on reject rate only
FEW = dict(checked=100, defects=10)           # fewer than the 200 pieces a rule needs: info


def _at(day: int, hour: int = 0, minute: int = 5) -> datetime:
    return datetime.combine(D0 + timedelta(days=day), datetime.min.time()).replace(hour=hour, minute=minute)


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


class Store:
    """runs and rows in the shape backtest.load() returns, written in the order the daily job writes them."""

    def __init__(self):
        self.runs, self.rows = [], []

    def run(self, when: datetime, levels: dict | None = None, settle: dict | None = None) -> int:
        """`levels` {subject: level} are predicted, then `settle` {subject: counts} settles each file's newest
        unsettled row — this run's own row if it predicted the file too (the 14 Oct mistake)."""
        rid = len(self.runs) + 1
        levels = levels or {}
        self.runs.append(dict(id=rid, started=_iso(when), finished=_iso(when + timedelta(seconds=3)),
                              dimension="quality", predictions=len(levels), errors=[]))
        for name, level in levels.items():
            factory, subject = name.split(":") if ":" in name else ("TAL", name)
            self.rows.append(dict(
                id=len(self.rows) + 1, run_id=rid, dimension="quality", subject=subject, factory=factory,
                level=level, window_days=7, days=7, totals={"Checked": 4000.0}, measures=[], caveat="",
                database="VistaQ_Default_RHL", views=[], as_of=_iso(when + timedelta(seconds=3)),
                outcome_at=None, outcome_level=None, outcome_totals={}, outcome_measures=[], revised=0))
        for name, counts in (settle or {}).items():
            factory, subject = name.split(":") if ":" in name else ("TAL", name)
            p = max((q for q in self.rows if (q["factory"], q["subject"]) == (factory, subject)
                     and q["outcome_at"] is None), key=lambda q: q["id"])
            a = _final(subject, factory, **counts)
            p.update(outcome_at=_iso(when + timedelta(seconds=4)), outcome_level=a.level,
                     outcome_totals=a.totals, outcome_measures=[asdict(m) for m in a.measures])
        return rid

    def compute(self, **kw):
        return backtest.compute(self.runs, self.rows, "quality", **kw)


def _file(r, subject):
    return next(f for f in r.files if f.subject == subject)


def _daily(s: Store, subject: str, first: int, last: int, level: str = "amber") -> None:
    for d in range(first, last + 1):
        s.run(_at(d), {subject: level})


def _enough(outcome=None) -> Store:
    """30 daily runs; 66 files, a third each red, amber and green the whole way, finishing on days 10 to 29.
    Red mostly ends over the DHU target, green mostly under, amber half and half."""
    outcome = outcome or (lambda lvl, i: OVER if (lvl == "red" and i % 5) or (lvl == "amber" and i % 2 == 0)
                          or (lvl == "green" and i % 7 == 0) else UNDER)
    files = {f"FR-{i:03d}": (("red", "amber", "green")[i % 3], 10 + i % 20, i) for i in range(66)}
    s = Store()
    for d in range(30):
        s.run(_at(d), {k: lvl for k, (lvl, end, _i) in files.items() if d < end},
              {k: outcome(lvl, i) for k, (lvl, end, i) in files.items() if d == end})
    return s


# ---------------------------------------------------------------- the real store, simulated days
def _settled_ids(db) -> set[int]:
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute("SELECT id FROM prediction WHERE outcome_at IS NOT NULL")}
    finally:
        con.close()


def _store_run(db, when: datetime, predict, scored, running) -> int:
    """One pass of watch.py's risk_once(), then its wall-clock stamps moved to `when`."""
    rid = signals.write_risk_run("quality", predict, when, [], source=SRC, path=db)
    before = _settled_ids(db)
    risk.settle("quality", scored, running, "2026-10-14", path=db)
    new = _settled_ids(db) - before
    con = sqlite3.connect(db)
    try:
        with con:
            con.execute("UPDATE risk_run SET finished = ? WHERE id = ?", (_iso(when + timedelta(seconds=3)), rid))
            con.execute("UPDATE prediction SET as_of = ? WHERE run_id = ?", (_iso(when + timedelta(seconds=3)), rid))
            con.executemany("UPDATE prediction SET outcome_at = ? WHERE id = ?",
                            [(_iso(when + timedelta(seconds=4)), i) for i in new])
    finally:
        con.close()
    return rid


def _dump(s: Store, db) -> None:
    """Write an in-memory Store into a real signals.db."""
    con = sqlite3.connect(db)
    try:
        con.executescript(signals._SCHEMA)
        with con:
            con.executemany("INSERT INTO risk_run (id, started, finished, dimension, predictions, errors) "
                            "VALUES (?,?,?,?,?,?)",
                            [(r["id"], r["started"], r["finished"], r["dimension"], r["predictions"],
                              json.dumps(r["errors"])) for r in s.runs])
            con.executemany(
                "INSERT INTO prediction (id, run_id, dimension, subject, factory, level, window_days, days, "
                "totals, measures, caveat, database, views, sql, as_of, outcome_at, outcome_level, "
                "outcome_totals, outcome_measures, revised) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(p["id"], p["run_id"], p["dimension"], p["subject"], p["factory"], p["level"], p["window_days"],
                  p["days"], json.dumps(p["totals"]), json.dumps(p["measures"]), p["caveat"], p["database"],
                  json.dumps(p["views"]), "SELECT 1", p["as_of"], p["outcome_at"], p["outcome_level"],
                  json.dumps(p["outcome_totals"]) if p["outcome_at"] else None,
                  json.dumps(p["outcome_measures"]) if p["outcome_at"] else None, p["revised"])
                 for p in s.rows])
    finally:
        con.close()


# ---------------------------------------------------------------- (1) one level per day
def test_the_last_run_of_a_day_is_that_days_level():
    s = Store()
    s.run(_at(0, 9), {"FR-A": "red"})              # a hand-run pass in the morning
    s.run(_at(0, 18), {"FR-A": "green"})           # the last run of the day: that day's level
    _daily(s, "FR-A", 1, 8, "green")
    s.run(_at(9), settle={"FR-A": UNDER})
    r = s.compute()
    assert r.by_date["2026-10-01"] == [1, 2] and 2 in r.kept and 1 not in r.kept
    assert len(r.kept) == 10
    f = r.files[0]
    assert (9, "green") in f.predictions and (9, "red") not in f.predictions
    assert [d for d, _ in f.predictions] == list(range(9, 0, -1))       # one level per day, not two


def test_load_reads_back_what_compute_takes(tmp_path):
    s = _enough()
    _dump(s, tmp_path / "signals.db")
    assert backtest.load(tmp_path / "signals.db") == (s.runs, s.rows)


# ---------------------------------------------------------------- (2) no lead time
def _replay_14_oct(db):
    """The 14 Oct 2026 shape: four hand-run passes. Runs 1 and 2 came before risk.predictable(), so the seven
    finished files were predicted and settled in the same run, twice. Returns (scored, running)."""
    done = [_final(f"FR-D{i}", **UNDER) for i in range(7)]
    live = [_final(f"FR-L{i}", **OVER) for i in range(3)]
    scored, running = done + live, {("TAL", a.subject) for a in live}
    _store_run(db, datetime(2026, 10, 14, 12, 53, 30), scored, scored, running)
    _store_run(db, datetime(2026, 10, 14, 12, 55, 58), scored, scored, running)
    _store_run(db, datetime(2026, 10, 14, 12, 56, 44), risk.predictable(scored, running), scored, running)
    _store_run(db, datetime(2026, 10, 14, 12, 56, 53), risk.predictable(scored, running), scored, running)
    return scored, running


def test_a_row_settled_by_the_pass_that_predicted_it_is_excluded_and_counted(tmp_path):
    """14 rows with no lead time at all: a record of the present, not a forecast."""
    db = tmp_path / "signals.db"
    _replay_14_oct(db)
    runs, rows = backtest.load(db)
    assert sum(1 for p in rows if p["outcome_at"]) == 14
    r = backtest.compute(runs, rows)
    assert r.kept == [4]                                      # 14 Oct uses run 4, by rule
    assert r.by_date["2026-10-14"] == [1, 2, 3, 4]
    assert (r.same_pass_rows, r.same_pass_files) == (14, 7)
    assert r.paused == [] and r.files == []
    assert r.open_files == 3 and r.stuck == []
    text = backtest.render_markdown(r, backtest.verdict(r), datetime(2026, 10, 15, 9, 0))
    assert "| Settled by the run that stored them | 14 rows (7 files) |" in text


def test_a_file_settled_with_no_lead_time_that_runs_again_is_open_and_later_usable(tmp_path):
    """15 Oct 2026: four of the seven files settled on 14 Oct by the run that stored them were back in
    production. They are open, not finished; when one finishes for real, that is the outcome used."""
    db = tmp_path / "signals.db"
    scored, running = _replay_14_oct(db)
    running |= {("TAL", f"FR-D{i}") for i in range(4)}
    _store_run(db, datetime(2026, 10, 15, 0, 5, 36), risk.predictable(scored, running), scored, running)
    r = backtest.compute(*backtest.load(db))
    assert r.kept == [4, 5] and r.same_pass_rows == 14 and r.paused == [] and r.resumed == []
    assert (r.open_files, r.reopened, r.stuck) == (7, 4, [])

    for day in range(16, 24):                       # FR-D0 finishes for real on 23 Oct
        if day == 23:
            running.discard(("TAL", "FR-D0"))
        _store_run(db, datetime(2026, 10, day, 0, 5), risk.predictable(scored, running), scored, running)
    r = backtest.compute(*backtest.load(db))
    assert [f.subject for f in r.files] == ["FR-D0"] and r.paused == [] and r.same_pass_rows == 14
    f = r.files[0]
    assert [d for d, _ in f.predictions] == list(range(8, 0, -1))       # 15 to 22 Oct
    assert (f.headline_ahead, f.over) == (7, False)
    assert (r.open_files, r.reopened) == (6, 3)


def test_same_pass_rows_are_removed_before_pause_resume_is_detected():
    """In the other order both files below would have two settled rows and read as paused and resumed."""
    s = Store()
    s.run(_at(0, 9), {"FR-X": "red", "FR-Y": "amber"}, settle={"FR-X": OVER, "FR-Y": OVER})
    s.run(_at(0, 10), {"FR-X": "red", "FR-Y": "amber"}, settle={"FR-X": OVER})
    _daily(s, "FR-Y", 1, 8)
    s.run(_at(9), settle={"FR-Y": OVER})                      # Y's second settled row, by a later run
    r = s.compute()
    assert r.settled_rows == 4 and r.same_pass_rows == 3
    assert r.paused == [] and r.resumed == []
    assert [f.subject for f in r.files] == ["FR-Y"]


# ---------------------------------------------------------------- (3) outcomes that were not final
def test_a_file_settled_twice_is_excluded_as_paused_and_resumed():
    """`revised` never rises in production: a paused file that resumes gets a second settled row, new ids."""
    s = Store()
    s.run(_at(0), {"FR-P": "red", "FR-Q": "green"})
    s.run(_at(1), {"FR-Q": "green"}, settle={"FR-P": OVER})           # P stops: settled
    s.run(_at(2), {"FR-P": "red", "FR-Q": "green"})                   # and resumes
    for d in range(3, 9):
        s.run(_at(d), {"FR-Q": "green"}, settle={"FR-P": OVER} if d == 3 else None)
    s.run(_at(9), settle={"FR-Q": UNDER})
    r = s.compute()
    assert r.paused == [("TAL", "FR-P")]
    assert [f.subject for f in r.files] == ["FR-Q"]
    text = backtest.render_markdown(r, backtest.verdict(r), datetime(2026, 10, 15))
    assert "| Settled more than once | 1 files |" in text and "FR-P (TAL)" in text


def test_a_file_predicted_again_after_its_outcome_is_left_out():
    """Settled, then running again and not yet settled a second time: its outcome was not final either."""
    s = Store()
    _daily(s, "FR-R", 0, 8)
    s.run(_at(9), settle={"FR-R": OVER})
    s.run(_at(10), {"FR-R": "red"})
    r = s.compute()
    assert r.resumed == [("TAL", "FR-R")] and r.files == [] and r.paused == []


def test_revised_rows_are_excluded_and_counted():
    s = Store()
    _daily(s, "FR-A", 0, 8)
    _daily(s, "FR-B", 0, 8)
    s.run(_at(9), settle={"FR-A": OVER, "FR-B": OVER})
    next(p for p in s.rows if p["subject"] == "FR-A" and p["outcome_at"])["revised"] = 1
    r = s.compute()
    assert r.revised == [("TAL", "FR-A")]
    assert [f.subject for f in r.files] == ["FR-B"]
    assert "| Outcome revised after it was written | 1 files |" in backtest.render_markdown(
        r, backtest.verdict(r), datetime(2026, 10, 15))


# ---------------------------------------------------------------- (4) what "over target" means
def test_over_target_reads_the_dhu_measure_not_the_worst_level():
    """Red on reject rate with DHU under target: over either target, NOT over the DHU target."""
    s = Store()
    _daily(s, "FR-A", 0, 8, "red")
    s.run(_at(9), settle={"FR-A": REJECTS})
    r = s.compute()
    f = r.files[0]
    assert f.outcome_level == "red" and f.over is False and f.dhu == 2.5
    t = r.headline["red"]
    assert (t.files, t.judged, t.over, t.either) == (1, 1, 0, 1)


def test_too_few_checked_at_the_end_is_neither_hit_nor_miss():
    s = Store()
    _daily(s, "FR-A", 0, 8, "green")
    s.run(_at(9), settle={"FR-A": FEW})
    r = s.compute()
    f = r.files[0]
    assert f.over is None and f.why == "too_few" and f.outcome_level == "info"
    t = r.headline["green"]
    assert (t.files, t.judged, t.over, t.too_few) == (1, 0, 0, 1) and t.share is None and t.interval is None
    assert r.judged == 0 and r.too_few == 1
    assert all(n == 0 for cells in r.lead.values() for _k, n in cells.values())


def test_an_outcome_without_measures_is_recomputed_from_its_totals_and_flagged():
    s = Store()
    _daily(s, "FR-A", 0, 8, "red")
    s.run(_at(9), settle={"FR-A": OVER})
    next(p for p in s.rows if p["outcome_at"])["outcome_measures"] = []
    f = s.compute(rule=dict(measure="dhu", amber=3.85, min_checked=200)).files[0]
    assert f.recomputed and f.over is True and f.dhu == 5.0 and f.amber == 3.85
    f = s.compute().files[0]                                  # no rule and no stored amber: not judged
    assert f.recomputed and f.over is None and f.why == "unknown"


# ---------------------------------------------------------------- (5) lead time
def test_every_daily_prediction_joins_its_outcome_by_days_ahead():
    s = Store()
    _daily(s, "FR-A", 0, 9, "amber")
    s.run(_at(10), settle={"FR-A": OVER})
    r = s.compute()
    f = r.files[0]
    assert f.predictions == [(d, "amber") for d in range(10, 0, -1)]
    assert (f.settled_level, f.settled_ahead) == ("amber", 1)        # the settled row is the 1-day row
    assert r.lead["1–7"]["amber"] == [7, 7] and r.lead["8–13"]["amber"] == [3, 3]
    assert r.lead["14–20"]["amber"] == [0, 0]
    text = backtest.render_markdown(r, backtest.verdict(r), datetime(2026, 10, 15))
    assert "| 1–7 | – | 7/7 (100.0%) | – | – | window overlaps outcome |" in text
    assert "| 8–13 | – | 3/3 (100.0%) | – | – |  |" in text


def test_the_headline_uses_the_level_a_window_or_more_ahead():
    """FR-A was red while a week or more out and green at the end; the headline is the level a window
    ahead, the second table the last daily one. FR-B settled three days after its first level: no headline."""
    s = Store()
    for d in range(11):
        levels = {"FR-A": "red" if d <= 4 else "green"}
        if d >= 8:
            levels["FR-B"] = "amber"
        s.run(_at(d), levels)
    s.run(_at(11), settle={"FR-A": OVER, "FR-B": OVER})
    r = s.compute()
    a, b = _file(r, "FR-A"), _file(r, "FR-B")
    assert (a.headline_level, a.headline_ahead) == ("red", 7)
    assert a.settled_level == "green"
    assert b.headline_level is None and r.no_headline == 1
    assert (r.headline["red"].files, r.headline["green"].files, r.headline_base.files) == (1, 0, 1)
    assert (r.last_daily["green"].files, r.last_daily["amber"].files, r.last_daily_base.files) == (1, 1, 2)


def test_the_headline_is_split_by_factory():
    s = Store()
    for d in range(9):
        s.run(_at(d), {"FR-A": "red", "RHL:FR-A": "green"})       # the same file number in two factories
    s.run(_at(9), settle={"FR-A": OVER, "RHL:FR-A": UNDER})
    r = s.compute()
    assert sorted(r.by_factory) == ["RHL", "TAL"]
    assert r.by_factory["TAL"][0]["red"].over == 1 and r.by_factory["RHL"][0]["green"].over == 0


# ---------------------------------------------------------------- the verdict
def test_insufficient_evidence_below_each_minimum():
    r = _enough().compute()
    assert backtest.verdict(r).sufficient and len(r.kept) == 30 and r.judged == 66

    def short(mutate):
        x = copy.deepcopy(r)
        mutate(x)
        v = backtest.verdict(x)
        assert v.status == backtest.INSUFFICIENT and not v.sufficient and v.facts == []
        return v.shortfalls

    assert short(lambda x: setattr(x, "kept", x.kept[:backtest.MIN_DATES - 1])) == [
        f"daily series: {backtest.MIN_DATES - 1} date(s), {backtest.MIN_DATES} needed"]
    assert short(lambda x: setattr(x, "files", x.files[:backtest.MIN_FILES - 1])) == [
        f"usable settled files with DHU known at the end: {backtest.MIN_FILES - 1}, {backtest.MIN_FILES} needed"]
    for lvl in ("red", "amber", "green"):
        assert short(lambda x: setattr(x.headline[lvl], "judged", backtest.MIN_PER_LEVEL - 1)) == [
            f"{lvl} files judged 7 or more days ahead: {backtest.MIN_PER_LEVEL - 1}, "
            f"{backtest.MIN_PER_LEVEL} needed"]
    empty = backtest.verdict(backtest.compute([], []))
    assert empty.status == backtest.INSUFFICIENT and len(empty.shortfalls) == 5


def test_enough_evidence_states_three_facts():
    v = backtest.verdict(_enough().compute())
    assert v.status == backtest.SUFFICIENT and v.ordered and v.separated and v.lift > 0
    assert [f.split(":")[0] for f in v.facts] == ["ordered", "separated", "lift"]
    flipped = backtest.verdict(_enough(lambda lvl, i: OVER if lvl == "green" else UNDER).compute())
    assert flipped.sufficient and flipped.ordered is False and flipped.separated is False and flipped.lift < 0


def test_the_verdict_never_says_pass():
    """The script states measurements; whether they are good enough is a person's decision."""
    cases = [backtest.compute([], []), _enough().compute(),
             _enough(lambda lvl, i: OVER if lvl == "green" else UNDER).compute()]
    for r in cases:
        v = backtest.verdict(r)
        for text in ("\n".join(backtest.verdict_lines(v)), backtest.render_markdown(r, v, datetime(2026, 11, 30))):
            assert not re.search(r"pass|earned", text, re.IGNORECASE), text


# ---------------------------------------------------------------- --until
def _history(db, last_day: int) -> None:
    """Six files, the daily job up to `last_day`: A and B finish on days 9 and 10, C and D on 15 and 18,
    E and F are still running at the end."""
    ends = {"FR-A": 9, "FR-B": 10, "FR-C": 15, "FR-D": 18, "FR-E": 99, "FR-F": 99}
    scored = [_final(k, **(OVER if i % 2 else UNDER)) for i, k in enumerate(ends)]
    for d in range(last_day + 1):
        running = {("TAL", k) for k, end in ends.items() if d < end}
        _store_run(db, _at(d), risk.predictable(scored, running), scored, running)


def test_until_reproduces_an_earlier_baseline(tmp_path):
    full, early = tmp_path / "full.db", tmp_path / "early.db"
    _history(full, 20)
    _history(early, 12)
    when = datetime(2026, 10, 13, 9, 0)

    cut = backtest.compute(*backtest.load(full, until=_at(12).date()))
    then = backtest.compute(*backtest.load(early))
    assert backtest.render_markdown(cut, backtest.verdict(cut), when) == \
        backtest.render_markdown(then, backtest.verdict(then), when)
    assert [f.subject for f in cut.files] == ["FR-A", "FR-B"] and cut.open_files == 4

    now = backtest.compute(*backtest.load(full))
    assert [f.subject for f in now.files] == ["FR-A", "FR-B", "FR-C", "FR-D"] and len(now.kept) == 21


# ---------------------------------------------------------------- the report
def test_the_report_has_every_section():
    sections = ["## Verdict", "## What was read", "## Headline", "## Last daily level", "## By factory",
                "## Lead time", "## Method notes"]
    for r in (backtest.compute([], []), _enough().compute()):
        r.until = date(2026, 11, 30)
        text = backtest.render_markdown(r, backtest.verdict(r), datetime(2026, 11, 30, 9, 0))
        at = [text.find(h) for h in sections]
        assert all(i >= 0 for i in at) and at == sorted(at), at
        assert "30 Nov 2026" in text
    text = backtest.render_markdown(r, backtest.verdict(r), datetime(2026, 11, 30, 9, 0))
    for line in ("Days with no run: none.", "Runs with errors: none.", "| Files still open |",
                 "| Open but no longer predicted (stuck) |", "| Too few checked at the end |"):
        assert line in text


def test_a_file_no_longer_predicted_and_never_settled_is_stuck():
    s = Store()
    s.run(_at(0), {"FR-A": "red", "FR-B": "green"})
    s.run(_at(1), {"FR-A": "red"})
    r = s.compute()
    assert r.open_files == 2 and r.stuck == [("TAL", "FR-B", _iso(_at(0) + timedelta(seconds=3)))]


def test_days_without_a_run_and_runs_with_errors_are_listed():
    s = Store()
    s.run(_at(0), {"FR-A": "red"})
    s.run(_at(3), {"FR-A": "red"})
    s.runs[1]["errors"] = ["300 subjects is more than max_subjects"]
    r = s.compute()
    assert r.missing_dates == ["2026-10-02", "2026-10-03"]
    text = backtest.render_markdown(r, backtest.verdict(r), datetime(2026, 10, 5))
    assert "Days with no run: 2 Oct 2026, 3 Oct 2026." in text and "max_subjects" in text


def test_wilson_interval():
    lo, hi = backtest.wilson(8, 10)
    assert round(lo, 3) == 0.490 and round(hi, 3) == 0.943
    assert backtest.wilson(0, 0) is None


# ---------------------------------------------------------------- the script
def _main(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))            # for `import _path`, as when run from scripts/
    return runpy.run_path(str(SCRIPTS / "backtest.py"), run_name="backtest_cli")["main"]


def test_the_script_exits_1_without_a_store(tmp_path, monkeypatch, capsys):
    main = _main(monkeypatch)
    assert main(["--store", str(tmp_path / "missing.db")]) == 1
    assert not (tmp_path / "missing.db").exists()        # read-only: it does not create one
    assert "no store" in capsys.readouterr().out


def test_the_script_exits_1_when_the_store_holds_no_risk_run(tmp_path, monkeypatch):
    db = tmp_path / "signals.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE watch_run (id INTEGER PRIMARY KEY, started TEXT, finished TEXT, signals INTEGER)")
    con.commit()
    con.close()
    main = _main(monkeypatch)
    assert main(["--store", str(db)]) == 1                # a store from before Phase M
    con = sqlite3.connect(db)
    con.executescript(signals._SCHEMA)
    con.close()
    assert main(["--store", str(db)]) == 1                # the tables, but no run


def test_the_script_writes_the_report_and_exits_3_while_evidence_is_short(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    db = tmp_path / "signals.db"
    _history(db, 12)
    main = _main(monkeypatch)
    assert main(["--store", str(db)]) == 3
    out = capsys.readouterr().out
    assert out.startswith(backtest.INSUFFICIENT) and "daily series: 13 date(s), 28 needed" in out
    reports = list((tmp_path / "logs").glob("backtest_quality_*.md"))
    assert len(reports) == 1 and "## Verdict" in reports[0].read_text(encoding="utf-8")


def test_the_script_exits_0_when_every_minimum_is_met(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    db = tmp_path / "signals.db"
    _dump(_enough(), db)
    main = _main(monkeypatch)
    assert main(["--store", str(db)]) == 0
    assert capsys.readouterr().out.startswith(backtest.SUFFICIENT)
    assert main(["--store", str(db), "--until", "2026-10-20"]) == 3      # an earlier cut is short again
