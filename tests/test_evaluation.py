"""The eval's scoring rules (ragbot/evaluation.py), from what the 2026-10-14 morning run got wrong."""
import json
from types import SimpleNamespace

import pytest

from ragbot.evaluation import (baseline_rows, blocks, complete, infra_failure, kind_changed, outage_row, pick_baseline,
                               scores)

QUOTA_TEXT = "The daily AI quota has been reached. Please try again after midnight Pacific time"


def _row(i, kind="data", correct=1.0, hit=1.0, **kw):
    r = {"id": i, "kind": kind, "route": "data", "answer": "x", "warnings": [], "citation_valid": 1.0,
         "hit": hit, "faithful": 1.0 if correct is not None else None, "correct": correct}
    r.update(kw)
    return r


def _write(folder, stamp, rows):
    (folder / f"{stamp}.json").write_text(json.dumps({"summary": {}, "rows": rows}), encoding="utf-8")
    return folder / f"{stamp}.json"


def test_a_provider_outage_is_not_a_wrong_answer():
    assert infra_failure(SimpleNamespace(error_kind="quota"))
    assert infra_failure(SimpleNamespace(error_kind="timeout"))
    assert not infra_failure(SimpleNamespace(error_kind=""))
    assert not infra_failure(SimpleNamespace(error_kind="db"))       # a database error is the assistant's
    assert not infra_failure(SimpleNamespace(error_kind="auth"))


def test_a_stored_outage_row_is_recognised_old_style_and_new():
    """Runs written before this rule stored the quota answer scored 0 with the orchestrator's warning."""
    old_style = _row(97, correct=0.0, hit=0.0, route="", answer=QUOTA_TEXT,
                     warnings=["llm quota: Error code: 429 - You exceeded your current quota"])
    assert outage_row(old_style)
    assert outage_row(_row(77, correct=None, unscored=True))
    assert not outage_row(_row(1, warnings=["templated answer (no model call)"]))
    assert not complete([_row(1), old_style]) and complete([_row(1), _row(2)])


def test_a_damaged_run_is_never_the_baseline(tmp_path):
    """14 Oct: compare() took 20261014T0938 — the newest run covering every case — though four of its
    answers were the quota message and four more were unscored."""
    good = [_row(i) for i in (1, 2, 3)]
    _write(tmp_path, "20261013T1644", good)
    _write(tmp_path, "20261014T0938", [_row(1), _row(2), _row(3, correct=0.0, warnings=["llm quota: 429"])])
    current = _write(tmp_path, "20261014T1600", [_row(i) for i in (1, 2, 3)])
    name, rows = pick_baseline(current, {1, 2, 3})
    assert name == "20261013T1644.json" and [r["id"] for r in rows] == [1, 2, 3]


def test_a_baseline_must_cover_every_case_and_a_named_one_says_which_is_missing(tmp_path):
    _write(tmp_path, "20261013T1644", [_row(1), _row(2)])
    current = _write(tmp_path, "20261014T1600", [_row(1), _row(2), _row(3)])
    assert pick_baseline(current, {1, 2, 3}) is None
    with pytest.raises(ValueError, match=r"\[3\]"):
        baseline_rows(tmp_path / "20261013T1644.json", {1, 2, 3})
    assert [r["id"] for r in baseline_rows(tmp_path / "20261013T1644.json", {2})] == [2]


def test_a_not_found_drop_blocks_although_correctness_overall_barely_moves():
    """Case 44 on 14 Oct: one not-found case answered from a table. Over 100 cases correctness moved about one
    point; over the five not-found cases it fell 20 — and not-found was never compared."""
    nf = [_row(i, kind="not_found", hit=None) for i in (44, 45, 46, 79, 93)]
    data = [_row(i) for i in range(1, 96)]
    before = scores(nf + data)
    after_nf = [dict(r, correct=0.0) if r["id"] == 44 else r for r in nf]
    after = scores(after_nf + data)
    assert round(before["correctness"] - after["correctness"], 3) == 0.01
    assert before["not_found_ok"] == 1.0 and after["not_found_ok"] == pytest.approx(0.8)
    assert blocks(before, after) == ["not_found_ok"]


def test_refuse_is_gated_and_a_kind_absent_on_one_side_is_not_compared():
    before = scores([_row(1, kind="refuse", hit=None), _row(2, kind="refuse", hit=None)])
    after = scores([_row(1, kind="refuse", hit=None), _row(2, kind="refuse", hit=None, correct=0.0)])
    assert "refuse_ok" in blocks(before, after)
    assert scores([_row(1)])["not_found_ok"] is None
    assert blocks(scores([_row(1)]), scores([_row(1)])) == []


def test_exactly_five_points_is_not_a_block():
    assert blocks({"hit_rate": 0.95}, {"hit_rate": 0.90}) == []
    assert blocks({"hit_rate": 0.95}, {"hit_rate": 0.8999}) == ["hit_rate"]


def test_a_redefined_case_is_named_not_compared():
    """Case 44 was not-found on 13 Oct and data on 14 Oct (R8). Left in, it is 25 points of not-found on one side
    and a data case on the other; compare() sets it aside and prints it instead."""
    old = [_row(44, kind="not_found", hit=None), _row(45, kind="not_found", hit=None), _row(1)]
    new = [_row(44, kind="data"), _row(45, kind="not_found", hit=None), _row(1)]
    assert kind_changed(old, new) == [44]
    assert kind_changed(old, old) == []
    assert kind_changed(old, [_row(99)]) == []          # a case only one side has is not "changed"
