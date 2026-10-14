"""Phase M: the Stage-2 risk rules — loading, validation, and the two honesty rules they exist to keep.

The thresholds are the whole point of this file. Amber is VistaQ's own target mirrored from the database,
so the factories are measured against the number they set for themselves; red is provisional and nobody has
validated it. The validator refuses a provisional red with no caveat, and refuses an amber that does not
say which database column it mirrors, because those are the two ways this could quietly start lying.
"""
import pytest

from ragbot.domain_agents import risk
from ragbot.domain_agents.watch import level


def _rs(**over):
    """A minimal valid rule set; each test breaks exactly one thing."""
    base = dict(dimension="demo", window_days=7, red_caveat="Red is provisional.",
                rules=[dict(name="r1", title="t", measure="dhu", amber=3.85, amber_source="DHUThreshold",
                            red=6, red_provisional=True, weight=1)])
    base.update(over)
    return {"demo": risk.RuleSet(**base)}


# ---------------------------------------------------------------- the real file
def test_the_quality_rules_load_and_validate():
    sets = risk.load_rule_sets()
    assert "quality" in sets, "config/rules/quality.yaml is not loaded"
    assert risk.validate(sets) == []
    q = sets["quality"]
    assert q.window_days == 7 and q.red_caveat
    assert {r["name"] for r in q.rules} == {"file_dhu", "file_reject_pct"}


def test_amber_mirrors_vistaq_s_own_targets():
    """Read live from dbo.Threshold on 2026-10-14: DHUThreshold 3.85, RejectRateThreshold 1.00. If these
    drift apart the runner records an error rather than scoring against a stale number."""
    q = risk.load_rule_sets()["quality"]
    assert q.rule("file_dhu")["amber"] == 3.85
    assert q.rule("file_dhu")["amber_source"] == "DHUThreshold"
    assert q.rule("file_reject_pct")["amber"] == 1.0
    assert q.rule("file_reject_pct")["amber_source"] == "RejectRateThreshold"


def test_first_pass_is_deliberately_not_a_rule():
    """dbo.Threshold.FTPRateThreshold is NULL — VistaQ has set no first-pass target. Inventing an amber for
    it would make up the one number this file is careful not to make up; it stays a recorded measure."""
    q = risk.load_rule_sets()["quality"]
    assert all(r["measure"] != "ftp_pct" for r in q.rules)


def test_every_provisional_red_in_the_real_file_carries_its_caveat():
    q = risk.load_rule_sets()["quality"]
    assert any(r.get("red_provisional") for r in q.rules)
    assert "provisional" in q.red_caveat.lower()


# ---------------------------------------------------------------- the validator
def test_a_provisional_red_without_a_caveat_is_refused():
    """The caveat is what stops an unvalidated threshold being read as the QC department's judgement."""
    problems = risk.validate(_rs(red_caveat=""))
    assert any("red_caveat" in p for p in problems)


def test_an_amber_that_does_not_name_its_source_column_is_refused():
    """An amber with no `amber_source` cannot be checked against the database, so it would quietly become
    a number this project invented rather than the factory's own target."""
    rules = [dict(name="r1", title="t", measure="dhu", amber=3.85, red=6)]
    assert any("amber_source" in p for p in risk.validate(_rs(rules=rules)))


@pytest.mark.parametrize("bad, expect", [
    (dict(amber=6, red=3.85), "wrong order"),
    (dict(amber=3.85, red=None), "both amber and red"),
    (dict(measure="nonsense"), "not one of"),
    (dict(title=""), "missing title"),
])
def test_the_validator_catches_a_rule_that_could_not_score_honestly(bad, expect):
    rule = dict(name="r1", title="t", measure="dhu", amber=3.85, amber_source="DHUThreshold", red=6)
    rule.update(bad)
    assert any(expect in p for p in risk.validate(_rs(rules=[rule])))


def test_below_is_bad_reverses_the_order_check():
    """A first-pass style measure would be `direction: below`; amber above red is then correct."""
    rule = dict(name="r1", title="t", measure="ftp_pct", amber=95, amber_source="FTPRateThreshold",
                red=90, direction="below")
    assert risk.validate(_rs(rules=[rule])) == []


def test_two_rules_cannot_share_a_name():
    r = dict(name="same", title="t", measure="dhu", amber=3.85, amber_source="DHUThreshold", red=6)
    assert any("unique name" in p for p in risk.validate(_rs(rules=[r, dict(r)])))


# ---------------------------------------------------------------- drift against the database
def test_drift_is_silent_when_the_target_still_matches():
    q = risk.load_rule_sets()["quality"]
    assert risk.drift(q, {"DHUThreshold": 3.85, "RejectRateThreshold": 1.0}) == []


def test_drift_reports_a_target_changed_in_vistaq():
    """Somebody raising the DHU target in VistaQ must not leave this file scoring against the old one."""
    q = risk.load_rule_sets()["quality"]
    out = risk.drift(q, {"DHUThreshold": 4.5, "RejectRateThreshold": 1.0})
    assert len(out) == 1 and "file_dhu" in out[0] and "4.5" in out[0]


def test_drift_ignores_a_target_the_database_does_not_set():
    """FTPRateThreshold is NULL in the live row; a null target is 'not set', not 'drifted to nothing'."""
    q = risk.load_rule_sets()["quality"]
    assert risk.drift(q, {"DHUThreshold": None, "RejectRateThreshold": 1.0}) == []


# ---------------------------------------------------------------- one vocabulary
def test_the_rules_are_scored_by_the_same_function_watch_rules_use():
    """There is one place where a threshold means something: watch.level(). These rules reuse its keys."""
    r = risk.load_rule_sets()["quality"].rule("file_dhu")
    assert level(r, 2.0) == "green"
    assert level(r, 3.85) == "amber"
    assert level(r, 5.9) == "amber"
    assert level(r, 6.0) == "red"
    assert level(r, None) == "green"
