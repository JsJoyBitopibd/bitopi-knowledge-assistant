"""Phase N2/N3b: the Stage-3 preview (domain_agents/recommend.py) — off by default, administrators only, costs
only when they can honestly be computed, and every write through the refusals of domain_agents/decisions.py."""
import sqlite3

import pytest
import yaml

from ragbot.auth.models import Scope, User
from ragbot.config import ROOT
from ragbot.domain_agents import decisions, recommend
from test_decisions import TODAY, _assess, _costs, _costs_file, _playbook, _predict

ADMIN = Scope(factories=frozenset(["*"]), is_admin=True)
STAFF = Scope(factories=frozenset(["*"]))


def _get(value):
    return lambda key, default=None: value if key == "agents.recommendations.enabled" else default


# ---------------------------------------------------------------- the gate
def test_off_by_default_and_for_everyone_but_administrators():
    d = yaml.safe_load((ROOT / "config" / "settings.example.yaml").read_text(encoding="utf-8"))
    assert d["agents"]["recommendations"]["enabled"] is False
    assert not recommend.enabled(_get(False), ADMIN)
    assert not recommend.enabled(_get(None), ADMIN)
    assert not recommend.enabled(_get("true"), ADMIN)            # only a real boolean switches it on
    assert not recommend.enabled(_get(True), STAFF)
    assert not recommend.enabled(_get(True), None)
    assert recommend.enabled(_get(True), ADMIN)

    def broken(key, default=None):
        raise RuntimeError("settings unreadable")
    assert not recommend.enabled(broken, ADMIN)


# ---------------------------------------------------------------- the preview
def test_a_draft_playbook_shows_its_actions_but_nothing_is_recommendable(tmp_path):
    db = tmp_path / "signals.db"
    _predict(db, _assess(defects=300))                           # DHU 7.5: red
    opts = recommend.options(ADMIN, playbooks=[_playbook(tmp_path, "draft")], costs=_costs(), today=TODAY, path=db)
    assert [o.action for o in opts] == ["add_inline_inspectors", "rework_defective_pieces"]
    assert all(o.level == "red" and o.playbook_status == "draft" for o in opts)
    assert all(not o.recommendable and o.cost is None for o in opts)
    assert all(any("not approved" in b for b in o.blocked) for o in opts)


def test_an_approved_ready_playbook_computes_the_cost_it_can(tmp_path):
    db = tmp_path / "signals.db"
    _predict(db, _assess(defects=300))
    opts = {o.action: o for o in recommend.options(ADMIN, playbooks=[_playbook(tmp_path)], costs=_costs(900),
                                                   today=TODAY, path=db)}
    rework = opts["rework_defective_pieces"]                     # subject.DefectivePcs x cost.rework_cost_per_piece
    assert rework.recommendable and rework.cost["value"] == 150 * 900
    assert "cost.rework_cost_per_piece" in recommend.cost_text(rework.cost)
    inspectors = opts["add_inline_inspectors"]                   # a person chooses how many: no cost until then
    assert inspectors.recommendable and inspectors.cost is None
    assert recommend.params_summary(inspectors.params) == "inspectors (people, 1–4)"


def test_a_missing_rate_blocks_the_option(tmp_path):
    db = tmp_path / "signals.db"
    _predict(db, _assess(defects=300))
    opts = recommend.options(ADMIN, playbooks=[_playbook(tmp_path)], costs=_costs(None), today=TODAY, path=db)
    assert opts and all(not o.recommendable for o in opts)


def test_green_settled_and_out_of_scope_files_are_not_offered(tmp_path):
    db = tmp_path / "signals.db"
    pids = _predict(db, _assess("FR-GREEN", defects=40), _assess("FR-RHL", factory="RHL", defects=300),
                    _assess("FR-DONE", defects=300))
    con = sqlite3.connect(db)
    with con:
        con.execute("UPDATE prediction SET outcome_at = '2026-10-15T00:00:00' WHERE id = ?", (pids[2],))
    con.close()
    pb = [_playbook(tmp_path)]
    subjects = {o.subject for o in recommend.options(ADMIN, playbooks=pb, costs=_costs(), today=TODAY, path=db)}
    assert subjects == {"FR-RHL"}                                # green and settled are never offered
    tal_only = Scope(factories=frozenset(["TAL"]), is_admin=True)
    assert recommend.options(tal_only, playbooks=pb, costs=_costs(), today=TODAY, path=db) == []


def test_no_store_means_no_options(tmp_path):
    assert recommend.options(ADMIN, playbooks=[], costs={}, path=tmp_path / "missing.db") == []


# ---------------------------------------------------------------- writing goes through decisions.py
def test_recommend_and_decide_use_the_refusals_of_decisions(tmp_path):
    db = tmp_path / "signals.db"
    _predict(db, _assess(defects=300))
    pb = _playbook(tmp_path)
    costs_file = _costs_file(tmp_path)
    opt = next(o for o in recommend.options(ADMIN, playbooks=[pb], costs=_costs(), today=TODAY, path=db)
               if o.action == "rework_defective_pieces")
    rid = recommend.recommend(opt, playbooks=[pb], costs_file=costs_file, path=db, today=TODAY)
    with pytest.raises(decisions.Refused, match="open user"):
        recommend.decide(rid, User(name="open", scope=ADMIN), "approve", path=db)
    with pytest.raises(decisions.Refused, match="needs a reason"):
        recommend.decide(rid, User(name="j.hossain", scope=ADMIN), "reject", path=db)
    recommend.decide(rid, User(name="j.hossain", scope=ADMIN), "approve", path=db)
    [r] = recommend.stored(ADMIN, path=db)
    assert r["id"] == rid and r["decision"]["verdict"] == "approve" and r["cost"]["value"] == 150 * 900


def test_recommend_refuses_a_draft_even_when_called_directly(tmp_path):
    db = tmp_path / "signals.db"
    _predict(db, _assess(defects=300))
    draft = _playbook(tmp_path, "draft")
    opt = recommend.options(ADMIN, playbooks=[draft], costs=_costs(), today=TODAY, path=db)[0]
    with pytest.raises(decisions.Refused, match="not approved"):
        recommend.recommend(opt, {"inspectors": 2}, playbooks=[draft], costs_file=_costs_file(tmp_path), path=db,
                            today=TODAY)
