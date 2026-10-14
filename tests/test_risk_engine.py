"""Phase M: the pure risk engine — scoring one subject over a window.

Pure on purpose: no database, no clock, no settings, exactly as data/quality.py evaluate() takes `today`
as an argument so a check can be asserted without either. Everything here runs on literal rows.

The rule this file exists to protect is the Quality charter's: DHU is 100 x SUM(Defects) / SUM(Checked)
computed over the sums, and averaging the DHU column is wrong. A line that checked 40 pieces must not
weigh as much as one that checked 4,000.
"""
import pytest

from ragbot.domain_agents import risk


def _rs(**over):
    base = dict(
        dimension="quality", window_days=7, red_caveat="Red is provisional; amber is VistaQ's own target.",
        rules=[
            dict(name="file_dhu", title="t", measure="dhu", amber=3.85, amber_source="DHUThreshold",
                 red=6, red_provisional=True, weight=2, min_checked=200),
            dict(name="file_reject_pct", title="t", measure="reject_pct", amber=1.0,
                 amber_source="RejectRateThreshold", red=2, red_provisional=True, weight=1, min_checked=200),
        ])
    base.update(over)
    return risk.RuleSet(**base)


def _row(checked, defects=0, rejected=0, passed=None, date="2026-10-14", **extra):
    return dict(InspectionDate=date, Checked=checked, Defects=defects, Rejected=rejected,
                Passed=checked if passed is None else passed, DefectivePcs=0, Rectified=0, **extra)


# ---------------------------------------------------------------- the aggregation rule
def test_a_ratio_is_recomputed_from_the_sums_not_averaged():
    """The charter: "compute it from the sums in the SQL; never average the DHU column". Averaging the two
    days below gives (1.0 + 20.0) / 2 = 10.5; the honest figure is 60 defects over 4,040 pieces = 1.49."""
    rows = [_row(4000, defects=40), _row(40, defects=8, date="2026-10-13")]
    sums = risk.totals(rows)
    assert sums["Checked"] == 4040 and sums["Defects"] == 48
    assert risk.ratio("dhu", sums) == 1.19          # 100 * 48 / 4040, not the 10.5 an average would give


def test_a_small_noisy_day_cannot_drag_a_large_clean_one():
    rs = _rs()
    big_and_small = risk.evaluate(rs, "FR-26-1985", "TAL", [_row(4000, defects=40), _row(40, defects=8)])
    assert big_and_small.level == "green"           # 1.19 DHU, under the 3.85 target


def test_missing_counts_are_zero_not_unknown():
    """VistaQ returns no value for a measure a line did not record that day; that is a zero."""
    sums = risk.totals([dict(Checked=100, Defects=None), dict(Checked=50)])
    assert sums["Checked"] == 150 and sums["Defects"] == 0 and sums["Rejected"] == 0


def test_a_measure_with_nothing_checked_has_no_value():
    """Not zero — the measure does not exist when the denominator is zero."""
    assert risk.ratio("dhu", {"Defects": 0, "Checked": 0}) is None
    assert risk.ratio("rectified_pct", {"Rectified": 5, "DefectivePcs": 0}) is None


def test_an_unknown_measure_has_no_value_rather_than_a_wrong_one():
    assert risk.ratio("not_a_measure", {"Checked": 100}) is None


# ---------------------------------------------------------------- levels
@pytest.mark.parametrize("defects, expect", [(40, "green"), (160, "amber"), (300, "red")])
def test_the_level_follows_the_thresholds(defects, expect):
    """4,000 checked: 1.0 DHU green, 4.0 amber (target 3.85), 7.5 red (provisional 6)."""
    a = risk.evaluate(_rs(), "FR-26-1985", "TAL", [_row(4000, defects=defects)])
    assert a.level == expect


def test_the_worst_measure_decides_the_subject_s_level():
    """DHU is clean but rejects are over the provisional red; the file is red."""
    a = risk.evaluate(_rs(), "FR-26-1985", "TAL", [_row(4000, defects=40, rejected=120)])
    assert dict((m.rule, m.level) for m in a.measures) == {"file_dhu": "green", "file_reject_pct": "red"}
    assert a.level == "red"


def test_too_few_pieces_is_info_not_green():
    """At 50 pieces one garment with three defects is a DHU of 6, which would read red on noise. "Not
    enough was checked to judge" is true; green would be a claim."""
    a = risk.evaluate(_rs(), "FR-26-1985", "TAL", [_row(50, defects=3)])
    assert a.level == "info"
    assert all(not m.enough for m in a.measures)
    assert "fewer than the 200" in a.measures[0].reason


def test_an_empty_window_is_info_and_does_not_crash():
    a = risk.evaluate(_rs(), "FR-26-1985", "TAL", [])
    assert a.level == "info" and a.totals["Checked"] == 0 and a.days == 0


# ---------------------------------------------------------------- what a backtest will need
def test_the_assessment_carries_the_counts_the_level_came_from():
    """The counts behind a level cannot be recovered once the window has moved on."""
    a = risk.evaluate(_rs(), "FR-26-1985", "TAL", [_row(4000, defects=300, rejected=10)])
    assert a.totals["Checked"] == 4000 and a.totals["Defects"] == 300
    assert a.subject == "FR-26-1985" and a.factory == "TAL" and a.dimension == "quality"
    assert a.window_days == 7 and a.days == 1
    m = next(m for m in a.measures if m.rule == "file_dhu")
    assert m.value == 7.5 and m.amber == 3.85 and m.red == 6     # the thresholds it was judged against


def test_the_factory_is_carried_so_the_scope_filter_can_be_applied():
    """Signals are filtered on read with scope.allows_factory(); a per-file row without a factory could
    not be filtered and would be shown to everyone."""
    a = risk.evaluate(_rs(), "FR-26-1985", "RHL", [_row(4000, defects=40)])
    assert a.factory == "RHL"


def test_distinct_inspection_days_are_counted():
    rows = [_row(1000, defects=10, date="2026-10-14"), _row(1000, defects=10, date="2026-10-13"),
            _row(1000, defects=10, date="2026-10-13")]
    assert risk.evaluate(_rs(), "FR", "TAL", rows).days == 2


# ---------------------------------------------------------------- honesty
def test_a_provisional_red_carries_its_caveat_on_the_row():
    """Stored on the assessment, not looked up at display time: a row read months later must still carry
    the reason its red is not a judgement."""
    a = risk.evaluate(_rs(), "FR-26-1985", "TAL", [_row(4000, defects=300)])
    assert a.level == "red" and a.provisional and "provisional" in a.caveat.lower()


def test_amber_and_green_carry_no_caveat():
    """The caveat is about red. Amber is VistaQ's own target and needs no apology."""
    for defects, expect in ((160, "amber"), (40, "green")):
        a = risk.evaluate(_rs(), "FR", "TAL", [_row(4000, defects=defects)])
        assert a.level == expect and a.caveat == "" and not a.provisional


def test_a_red_from_a_validated_threshold_is_not_marked_provisional():
    """When the QC interviews set a real red, the same red level stops carrying the caveat — without any
    code change, because provisional-ness is a property of the rule."""
    rs = _rs(rules=[dict(name="file_dhu", title="t", measure="dhu", amber=3.85,
                         amber_source="DHUThreshold", red=6, red_provisional=False, min_checked=200)])
    a = risk.evaluate(rs, "FR", "TAL", [_row(4000, defects=300)])
    assert a.level == "red" and not a.provisional and a.caveat == ""


# ---------------------------------------------------------------- the note on an answer (Phase M6b)
def test_the_note_states_only_numbers_that_are_in_its_cited_rows():
    """Non-negotiable #2: every number in an answer must appear in a retrieved row. The level is computed
    in Python from VistaQ rows, never by the model, and note_source() is the result that licenses it."""
    import re

    a = risk.evaluate(_rs(), "FR-26-1945", "TAL", [_row(4000, defects=327, rejected=0)])
    text = risk.note(a, marker="D3", as_of_day="2026-10-14")
    src = risk.note_source(a)

    in_rows = {str(v) for row in src.rows for v in row}
    # the file number and the date are identifiers, not claims the sentence makes
    claims = text.replace(a.subject, "").replace("2026-10-14", "").replace("D3", "")
    for n in re.findall(r"\d+\.\d+|\b\d+\b", claims):
        assert n in in_rows or n == str(a.window_days), f"{n} is in the sentence but not in the cited rows"


def test_the_note_is_readable_rather_than_a_column_dump():
    a = risk.evaluate(_rs(), "FR-26-1945", "TAL", [_row(4000, defects=327, rejected=40)])
    text = risk.note(a, marker="D3")
    assert "DHU" in text and "reject rate" in text
    assert "dhu " not in text and "reject_pct" not in text
    assert "critical" in text


def test_the_note_carries_the_caveat_only_for_a_provisional_red():
    red = risk.note(risk.evaluate(_rs(), "FR", "TAL", [_row(4000, defects=327)]), marker="D3")
    green = risk.note(risk.evaluate(_rs(), "FR", "TAL", [_row(4000, defects=40)]), marker="D3")
    assert "provisional" in red.lower() and "provisional" not in green.lower()


def test_the_note_says_so_when_too_little_was_checked():
    a = risk.evaluate(_rs(), "FR", "TAL", [_row(50, defects=3)])
    assert "too few to judge" in risk.note(a, marker="D3")


def test_the_note_source_is_a_local_result_like_the_aggregates_copies():
    """engine="local" is the project's existing way to cite a pre-computed copy (data/aggregates.py)."""
    src = risk.note_source(risk.evaluate(_rs(), "FR-26-1945", "TAL", [_row(4000, defects=327)]))
    assert src.engine == "local" and src.database == "signals.db" and src.views == ["prediction"]
    assert src.tool == "risk" and "FR-26-1945" in src.sql
    assert "Measure" in src.columns and len(src.rows) == 2
