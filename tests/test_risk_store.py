"""Phase M: predictions and their outcomes in signals.db.

The sibling-table decision is not stylistic. signals.latest() is
`SELECT ... FROM watch_run ORDER BY id DESC LIMIT 1`, so a risk run written into watch_run would become
"the newest run" and the morning brief would show it instead of the watcher's signals. The first test
below is the one that keeps that from happening.
"""
from datetime import datetime

from ragbot.auth.models import Scope
from ragbot.domain_agents import risk, signals
from ragbot.domain_agents.watch import Signal


def _sig(factory="TAL", rule="dhu_today", level="red", kind="ops"):
    return Signal(agent="quality", rule=rule, factory=factory, level=level, value=6.5, title="t",
                  question="q", weight=2, metric="", database="VistaQ_Default_RHL", views=["rag.v"],
                  sql="SELECT 1", as_of="2026-10-14T10:00:00", values={}, kind=kind)


def _rs():
    return risk.RuleSet(dimension="quality", window_days=7, red_caveat="Red is provisional.",
                        rules=[dict(name="file_dhu", title="t", measure="dhu", amber=3.85,
                                    amber_source="DHUThreshold", red=6, red_provisional=True,
                                    min_checked=200)])


def _assess(subject="FR-26-1985", factory="TAL", checked=4000, defects=300):
    rows = [dict(InspectionDate="2026-10-14", Checked=checked, Defects=defects, Rejected=0,
                 Passed=checked - defects, DefectivePcs=0, Rectified=0)]
    return risk.evaluate(_rs(), subject, factory, rows)


SRC = {"database": "VistaQ_Default_RHL", "views": ["rag.vw_PlanQualityDay"], "sql": "SELECT 1"}


# ---------------------------------------------------------------- the trap
def test_a_risk_run_does_not_become_the_morning_brief(tmp_path):
    """latest() takes the newest watch_run. If a risk run were written there the brief would silently
    become a list of file risk levels — the whole morning page replaced without a word."""
    db = tmp_path / "signals.db"
    signals.write_run([_sig()], datetime.now(), [], path=db)
    signals.write_risk_run("quality", [_assess()], datetime.now(), [], source=SRC, path=db)

    run, sigs = signals.latest(Scope.unrestricted(), path=db)
    assert run is not None and run["signals"] == 1
    assert [s.rule for s in sigs] == ["dhu_today"]      # the watcher's signal, not a prediction


def test_the_watcher_and_the_risk_job_keep_their_own_runs(tmp_path):
    db = tmp_path / "signals.db"
    signals.write_run([_sig()], datetime.now(), [], path=db)
    rid = signals.write_risk_run("quality", [_assess(), _assess("FR-26-1990")], datetime.now(), [],
                                 source=SRC, path=db)
    run, preds = signals.risk_latest(Scope.unrestricted(), path=db)
    assert run["id"] == rid and run["predictions"] == 2 and len(preds) == 2


# ---------------------------------------------------------------- round trip
def test_a_prediction_comes_back_as_the_object_the_engine_produced(tmp_path):
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess()], datetime.now(), [], source=SRC, path=db)
    _, preds = signals.risk_latest(Scope.unrestricted(), path=db)
    a = preds[0]
    assert a.subject == "FR-26-1985" and a.factory == "TAL" and a.level == "red"
    assert a.totals["Checked"] == 4000 and a.totals["Defects"] == 300
    m = a.measures[0]
    assert m.measure == "dhu" and m.value == 7.5 and m.amber == 3.85 and m.red == 6
    assert m.red_provisional and a.provisional


def test_the_caveat_is_stored_on_the_row_not_looked_up_later(tmp_path):
    """A level read back months later must still carry the reason its red is not a judgement, even if the
    rules file has changed in the meantime."""
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess()], datetime.now(), [], source=SRC, path=db)
    _, preds = signals.risk_latest(Scope.unrestricted(), path=db)
    assert "provisional" in preds[0].caveat.lower()


# ---------------------------------------------------------------- scope
def test_a_prediction_is_filtered_by_factory_on_read(tmp_path):
    """A per-file row carries its factory precisely so this filter can be applied; without it a file-level
    level could not be scoped and would reach every viewer."""
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess("FR-A", "TAL"), _assess("FR-B", "RHL")],
                           datetime.now(), [], source=SRC, path=db)
    _, preds = signals.risk_latest(Scope(factories=frozenset({"TAL"})), path=db)
    assert [p.subject for p in preds] == ["FR-A"]


# ---------------------------------------------------------------- outcomes
def test_an_outcome_settles_the_newest_prediction_for_a_subject(tmp_path):
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess()], datetime.now(), [], source=SRC, path=db)
    signals.write_risk_run("quality", [_assess()], datetime.now(), [], source=SRC, path=db)

    open_now = signals.open_predictions(path=db)
    assert len(open_now) == 1                      # one per subject, not one per run
    assert signals.write_outcome(open_now[0]["id"], "red", {"Checked": 9000, "Defects": 700}, [], path=db)
    assert signals.open_predictions(path=db) == []

    done = signals.settled(path=db)
    assert len(done) == 1 and done[0]["level"] == "red" and done[0]["outcome_level"] == "red"
    assert done[0]["outcome_totals"]["Checked"] == 9000 and done[0]["revised"] == 0


def test_a_subject_that_resumes_and_settles_differently_is_marked_revised(tmp_path):
    """A file can pause and resume. A backtest must be able to exclude a subject that was settled early
    and then moved, rather than scoring against a figure that was not final."""
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess()], datetime.now(), [], source=SRC, path=db)
    pid = signals.open_predictions(path=db)[0]["id"]
    signals.write_outcome(pid, "amber", {"Checked": 9000}, [], path=db)
    signals.write_outcome(pid, "red", {"Checked": 15000}, [], path=db)
    done = signals.settled(path=db)
    assert done[0]["revised"] == 1 and done[0]["outcome_level"] == "red"


def test_rewriting_the_same_outcome_is_not_a_revision(tmp_path):
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess()], datetime.now(), [], source=SRC, path=db)
    pid = signals.open_predictions(path=db)[0]["id"]
    signals.write_outcome(pid, "red", {"Checked": 9000}, [], path=db)
    signals.write_outcome(pid, "red", {"Checked": 9000}, [], path=db)
    assert signals.settled(path=db)[0]["revised"] == 0


def test_an_unknown_prediction_cannot_be_settled(tmp_path):
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess()], datetime.now(), [], source=SRC, path=db)
    assert signals.write_outcome(9999, "red", {}, [], path=db) is False


# ---------------------------------------------------------------- empty and old stores
def test_reading_before_any_risk_run_is_empty_not_an_error(tmp_path):
    db = tmp_path / "signals.db"
    assert signals.risk_latest(Scope.unrestricted(), path=db) == (None, [])
    assert signals.open_predictions(path=db) == [] and signals.settled(path=db) == []
    signals.write_run([_sig()], datetime.now(), [], path=db)      # a store with only watcher runs
    assert signals.risk_latest(Scope.unrestricted(), path=db) == (None, [])


def test_a_store_written_before_phase_m_gains_the_new_tables(tmp_path):
    """The existing store on the server is 2.1 MB of watcher history; it must not need rebuilding."""
    db = tmp_path / "signals.db"
    signals.write_run([_sig()], datetime.now(), [], path=db)
    signals.write_risk_run("quality", [_assess()], datetime.now(), [], source=SRC, path=db)
    run, sigs = signals.latest(Scope.unrestricted(), path=db)
    assert len(sigs) == 1                                          # the old history still reads
    assert len(signals.risk_latest(Scope.unrestricted(), path=db)[1]) == 1


def test_another_dimension_does_not_show_up_as_quality(tmp_path):
    db = tmp_path / "signals.db"
    a = _assess()
    a.dimension = "shipping"
    signals.write_risk_run("shipping", [a], datetime.now(), [], source=SRC, path=db)
    assert signals.risk_latest(Scope.unrestricted(), "quality", path=db) == (None, [])
    assert len(signals.risk_latest(Scope.unrestricted(), "shipping", path=db)[1]) == 1


# ---------------------------------------------------------------- settlement (Phase M5)
def test_a_subject_still_planned_is_not_settled(tmp_path):
    """Settling a file that is still running would record a half-finished window as its final quality."""
    db = tmp_path / "signals.db"
    a = _assess("FR-A", "TAL")
    signals.write_risk_run("quality", [a], datetime.now(), [], source=SRC, path=db)
    done = risk.settle("quality", [a], running={("TAL", "FR-A")}, newest="2026-10-14", path=db)
    assert done == 0 and len(signals.open_predictions(path=db)) == 1


def test_a_subject_no_longer_planned_settles(tmp_path):
    db = tmp_path / "signals.db"
    a = _assess("FR-A", "TAL")
    signals.write_risk_run("quality", [a], datetime.now(), [], source=SRC, path=db)
    assert risk.settle("quality", [a], running=set(), newest="2026-10-14", path=db) == 1
    assert signals.settled(path=db)[0]["outcome_level"] == "red"


def test_nothing_settles_while_the_feed_holds_no_newest_day(tmp_path):
    """The guard against an outage: if VistaQ's newest day cannot be read, every open subject would look
    finished at once and the backtest would be built on outcomes recorded during the outage."""
    db = tmp_path / "signals.db"
    a = _assess("FR-A", "TAL")
    signals.write_risk_run("quality", [a], datetime.now(), [], source=SRC, path=db)
    assert risk.settle("quality", [a], running=set(), newest=None, path=db) == 0
    assert risk.settle("quality", [a], running=set(), newest="", path=db) == 0
    assert len(signals.open_predictions(path=db)) == 1


def test_a_subject_with_nothing_left_in_the_window_waits(tmp_path):
    """Not planned and not in the window either: there is nothing to settle against, so it waits rather
    than being settled against a figure that is not there."""
    db = tmp_path / "signals.db"
    signals.write_risk_run("quality", [_assess("FR-A", "TAL")], datetime.now(), [], source=SRC, path=db)
    assert risk.settle("quality", [], running=set(), newest="2026-10-14", path=db) == 0
    assert len(signals.open_predictions(path=db)) == 1


def test_a_factory_is_part_of_the_subject_key(tmp_path):
    """The same file can run in two factories; one finishing must not settle the other."""
    db = tmp_path / "signals.db"
    tal, rhl = _assess("FR-A", "TAL"), _assess("FR-A", "RHL")
    signals.write_risk_run("quality", [tal, rhl], datetime.now(), [], source=SRC, path=db)
    assert risk.settle("quality", [tal, rhl], running={("RHL", "FR-A")}, newest="2026-10-14", path=db) == 1
    still = signals.open_predictions(path=db)
    assert [(p["factory"], p["subject"]) for p in still] == [("RHL", "FR-A")]


def test_only_subjects_still_in_production_get_a_prediction():
    """Found by running the job twice: a finished file was predicted and settled in the same run, every
    run, which would have filled the backtest with zero-lead-time rows — a record of the present dressed
    as a forecast. Finished subjects are still scored, because their final window is what settles them."""
    a, b = _assess("FR-RUNNING", "TAL"), _assess("FR-DONE", "TAL")
    keep = risk.predictable([a, b], running={("TAL", "FR-RUNNING")})
    assert [x.subject for x in keep] == ["FR-RUNNING"]
    assert risk.predictable([a, b], running=set()) == []
    assert len(risk.predictable([a, b], running={("TAL", "FR-RUNNING"), ("TAL", "FR-DONE")})) == 2


def test_a_finished_subject_stays_settled_across_runs(tmp_path):
    """The end-to-end shape of the fix: once settled, a subject neither reopens nor is re-settled."""
    db = tmp_path / "signals.db"
    a, b = _assess("FR-RUNNING", "TAL"), _assess("FR-DONE", "TAL")
    running = {("TAL", "FR-RUNNING")}
    # day 1: both running, both predicted
    signals.write_risk_run("quality", risk.predictable([a, b], {("TAL", "FR-RUNNING"), ("TAL", "FR-DONE")}),
                           datetime.now(), [], source=SRC, path=db)
    assert len(signals.open_predictions(path=db)) == 2
    # day 2: FR-DONE has finished — it settles once and is not predicted again
    signals.write_risk_run("quality", risk.predictable([a, b], running), datetime.now(), [],
                           source=SRC, path=db)
    assert risk.settle("quality", [a, b], running, "2026-10-14", path=db) == 1
    # day 3: nothing left to settle, and the open set is only the running subject
    signals.write_risk_run("quality", risk.predictable([a, b], running), datetime.now(), [],
                           source=SRC, path=db)
    assert risk.settle("quality", [a, b], running, "2026-10-14", path=db) == 0
    assert [p["subject"] for p in signals.open_predictions(path=db)] == ["FR-RUNNING"]
    assert len(signals.settled(path=db)) == 1
