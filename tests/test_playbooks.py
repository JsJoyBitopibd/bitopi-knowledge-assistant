"""Phase N1a: the playbook file format, its validator and the calculators (domain_agents/playbook.py).

Stage 3 is gated: nothing in the app reads a playbook yet. These tests hold the format to the rules that
make a recommendation trustworthy the day the gate opens — no number written in a playbook, no number
nobody owns, no typo that silently widens which risks a playbook answers.
"""
import copy
import shutil
from datetime import date

import pytest
import yaml

from ragbot.config import ROOT
from ragbot.domain_agents import playbook as pbm
from ragbot.domain_agents import risk

EXAMPLE = ROOT / "config" / "playbooks" / "quality.example.yaml"
COSTS_EXAMPLE = ROOT / "config" / "costs.example.yaml"


def _example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def _costs(value=None) -> dict:
    out = pbm.load_costs(COSTS_EXAMPLE)
    if value is not None:
        for src in out.values():
            src.update(value=value, owner="Commercial", source="rate card", as_of="2026-10-01")
    return out


def _load(tmp_path, d: dict, name: str = "p.yaml") -> pbm.Playbook:
    f = tmp_path / name
    f.write_text(yaml.safe_dump(d, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return pbm.load_file(f)


def _problems(tmp_path, d: dict, costs=None) -> list[str]:
    return pbm.validate([_load(tmp_path, d)], risk.load_rule_sets(), _costs() if costs is None else costs)


def _approved(d: dict) -> dict:
    d = copy.deepcopy(d)
    d.update(status="approved", owner="Quality", author="QC manager", approved_by="Head of Quality",
             approved_on="2026-10-01", source_pdf="data/pdfs/Playbook/quality_playbook.pdf")
    for i, a in enumerate(d["actions"], start=1):
        a["pdf_page"] = i
        for p in (a.get("params") or {}).values():
            p.update(min=1, max=4)
    return d


def _numbers(node, path="") -> list[str]:
    """Every int or float value in a parsed YAML tree (bools are not numbers here)."""
    if isinstance(node, dict):
        return [n for k, v in node.items() for n in _numbers(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [n for i, v in enumerate(node) for n in _numbers(v, f"{path}[{i}]")]
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        return [f"{path} = {node}"]
    return []


def _assess(defects: int, checked: int = 4000, factory: str = "TAL") -> risk.Assessment:
    rs = risk.RuleSet(dimension="quality", window_days=7, red_caveat="Red is provisional.",
                      rules=[dict(name="file_dhu", title="t", measure="dhu", amber=3.85, amber_source="DHUThreshold",
                                  red=6, red_provisional=True, min_checked=200)])
    rows = [dict(InspectionDate="2026-10-14", Checked=checked, Defects=defects, Rejected=0,
                 Passed=checked - defects, DefectivePcs=defects // 2, Rectified=0)]
    return risk.evaluate(rs, "FR-26-1985", factory, rows)


# ---------------------------------------------------------------- the template
def test_the_example_validates_and_holds_no_numbers(tmp_path):
    for f in (EXAMPLE, COSTS_EXAMPLE):
        assert _numbers(yaml.safe_load(f.read_text(encoding="utf-8"))) == [], f.name

    pb = pbm.load_file(EXAMPLE)
    costs = pbm.load_costs(COSTS_EXAMPLE)
    assert pbm.validate([pb], risk.load_rule_sets(), costs) == []
    assert pb.status == "draft" and all(s["value"] is None for s in costs.values())
    # Valid, and not ready: a draft whose rates nobody has supplied is the honest state of Stage 3 today.
    reasons = pbm.ready(pb, costs, date(2026, 10, 15))
    assert any("not approved" in r for r in reasons)
    assert any("qc_inspector_day_rate has no value" in r for r in reasons)

    # *.example.yaml is a template and is never loaded as a playbook; a copy under another name is.
    shutil.copy(EXAMPLE, tmp_path / "quality.example.yaml")
    assert pbm.load_playbooks(tmp_path) == []
    shutil.copy(EXAMPLE, tmp_path / "quality.yaml")
    assert [p.id for p in pbm.load_playbooks(tmp_path)] == ["quality_dhu_over_target"]


def test_no_cost_file_means_no_sources_and_no_problems(tmp_path):
    assert pbm.load_costs(tmp_path / "costs.yaml") == {}
    assert pbm.validate([], risk.load_rule_sets(), {}) == []


# ---------------------------------------------------------------- loud at load
def test_an_unknown_key_under_when_fails_at_load(tmp_path):
    """`factories:` for `factory:` would be ignored by a tolerant loader and match every factory."""
    d = _example()
    d["when"]["factories"] = ["TAL"]
    with pytest.raises(RuntimeError, match="factories"):
        _load(tmp_path, d)
    (tmp_path / "pb").mkdir()
    (tmp_path / "pb" / "quality.yaml").write_text(yaml.safe_dump(d), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unknown key"):
        pbm.load_playbooks(tmp_path / "pb")


def test_unknown_keys_elsewhere_are_problems(tmp_path):
    d = _example()
    d["approver"] = "someone"
    d["actions"][0]["costs"] = {}
    d["actions"][0]["params"]["inspectors"]["maximum"] = None
    d["actions"][0]["cost"]["formula"] = "a*b"
    found = "\n".join(_problems(tmp_path, d))
    for k in ("'approver'", "'costs'", "'maximum'", "'formula'"):
        assert f"unknown key {k}" in found


# ---------------------------------------------------------------- no number nobody owns
def test_a_literal_cost_input_is_refused(tmp_path):
    for literal in (350, 12.5, "350"):
        d = _example()
        d["actions"][0]["cost"]["inputs"]["rate"] = literal
        found = _problems(tmp_path, d)
        assert any("add_inline_inspectors: cost.inputs.rate" in p and "literal number" in p for p in found), found


def test_a_cost_reference_must_be_a_declared_source(tmp_path):
    d = _example()
    d["actions"][0]["cost"]["inputs"]["rate"] = "cost.no_such_rate"
    assert any("cost.no_such_rate is not a declared cost source" in p for p in _problems(tmp_path, d))
    # The same check covers the other two kinds of input.
    d = _example()
    d["actions"][0]["cost"]["inputs"]["quantity"] = "param.operators"
    d["actions"][1]["cost"]["inputs"]["quantity"] = "subject.Shipped"
    found = "\n".join(_problems(tmp_path, d))
    assert "param.operators is not a parameter of this action" in found
    assert "subject.Shipped is not one of" in found
    # With no cost file at all, every cost reference is undeclared.
    assert any("not a declared cost source" in p for p in _problems(tmp_path, _example(), costs={}))


def test_a_cost_value_needs_an_owner_a_source_and_a_date(tmp_path):
    costs = _costs()
    costs["qc_inspector_day_rate"]["value"] = 900
    found = "\n".join(pbm.validate([], {}, costs))
    for need in ("qc_inspector_day_rate: a value needs owner", "qc_inspector_day_rate: a value needs source",
                 "qc_inspector_day_rate: a value needs as_of"):
        assert need in found, need
    costs["qc_inspector_day_rate"].update(value="nine hundred", owner="Commercial", source="rate card",
                                          as_of="2026-10-01", currency="BDT")
    found = "\n".join(pbm.validate([], {}, costs))
    assert "value 'nine hundred' is not a number" in found and "unknown key 'currency'" in found
    assert pbm.validate([], {}, _costs(value=900)) == []


def test_digits_in_a_title_are_refused(tmp_path):
    d = _example()
    d["title"] = "DHU over 3.85 on an order file"
    assert any("quality_dhu_over_target: the title holds a digit" in p for p in _problems(tmp_path, d))
    d = _example()
    d["actions"][0]["title"] = "Add 2 inline inspectors"
    assert any("add_inline_inspectors: the title holds a digit" in p for p in _problems(tmp_path, d))
    d = _example()
    d["actions"][1]["title"] = "Rework ৩ pieces"              # a Bangla digit is a digit too
    assert any("rework_defective_pieces: the title holds a digit" in p for p in _problems(tmp_path, d))


def test_approved_needs_a_named_approver_and_date(tmp_path):
    d = _example()
    d["status"] = "approved"
    found = "\n".join(_problems(tmp_path, d))
    for need in ("approved needs approved_by", "approved needs approved_on", "approved needs pdf_page",
                 "approved needs both min and max", "owner is still a placeholder"):
        assert need in found, need
    assert _problems(tmp_path, _approved(_example())) == []
    d = _approved(_example())
    d["approved_on"] = "last week"
    assert any("approved_on 'last week' is not a date" in p for p in _problems(tmp_path, d))
    d = _approved(_example())
    d["status"] = "live"
    assert any("status 'live' is not one of" in p for p in _problems(tmp_path, d))


def test_parameter_bounds_and_types(tmp_path):
    d = _example()
    d["actions"][0]["params"]["inspectors"].update(min=5, max=2)
    assert any("min is greater than max" in p for p in _problems(tmp_path, d))
    d = _example()
    d["actions"][0]["params"]["inspectors"]["type"] = "boolean"
    found = "\n".join(_problems(tmp_path, d))
    assert "a calculator input is an integer or a number" in found
    d = _example()
    d["actions"][0]["params"]["inspectors"] = {"type": "choice", "unit": "people"}
    assert any("needs a non-empty list of choices" in p for p in _problems(tmp_path, d))


def test_every_action_needs_a_cost_block_and_a_known_calculator(tmp_path):
    d = _example()
    del d["actions"][0]["cost"]
    assert any("add_inline_inspectors: every action needs a cost block" in p for p in _problems(tmp_path, d))
    d = _example()
    d["actions"][0]["cost"]["calculator"] = "people_x_rate_x_days"
    assert any("cost.calculator 'people_x_rate_x_days' is not one of" in p for p in _problems(tmp_path, d))
    d = _example()
    d["actions"][1]["id"] = d["actions"][0]["id"]
    assert any("used by another action" in p for p in _problems(tmp_path, d))


def test_two_files_with_one_id_are_a_problem(tmp_path):
    a, b = _load(tmp_path, _example(), "a.yaml"), _load(tmp_path, _example(), "b.yaml")
    found = pbm.validate([a, b], risk.load_rule_sets(), _costs())
    assert any("the id is used by another playbook" in p for p in found)


def test_when_must_name_a_real_rule(tmp_path):
    d = _example()
    d["when"]["rule"] = "file_dhu_typo"
    assert any("when.rule 'file_dhu_typo' is not a rule" in p for p in _problems(tmp_path, d))
    d = _example()
    d["when"]["dimension"] = "shipping"
    assert any("when.dimension 'shipping' has no rule set" in p for p in _problems(tmp_path, d))


# ---------------------------------------------------------------- matching
def test_green_and_info_cannot_be_matched(tmp_path):
    for word in ("green", "info"):
        d = _example()
        d["when"]["level"] = [word]
        assert any(f"when.level {word} cannot be matched" in p for p in _problems(tmp_path, d))
    d = _example()
    d["when"]["level"] = []
    assert any("when.level must name red, amber or both" in p for p in _problems(tmp_path, d))

    # And whatever a file says, a green or info level never matches.
    d = _example()
    d["when"]["level"] = ["red", "amber", "green", "info"]
    pb = _load(tmp_path, d)
    assert pbm.match(_assess(defects=300), [pb]) == [pb]                 # DHU 7.5: red
    assert pbm.pattern_of(pb, _assess(defects=200)) == "quality.file_dhu=amber"
    assert pbm.match(_assess(defects=100), [pb]) == []                   # DHU 2.5: green
    assert pbm.match(_assess(defects=10, checked=100), [pb]) == []       # too few checked: info


def test_match_reads_the_named_rule_the_factory_and_the_status(tmp_path):
    pb = pbm.load_file(EXAMPLE)                                          # level [red], every factory
    assert pbm.match(_assess(defects=200), [pb]) == []                   # amber is not red
    d = _example()
    d["when"]["factory"] = ["rhl"]
    assert pbm.match(_assess(defects=300, factory="TAL"), [_load(tmp_path, d)]) == []
    assert pbm.match(_assess(defects=300, factory="RHL"), [_load(tmp_path, d)]) != []
    d = _example()
    d["status"] = "retired"
    assert pbm.match(_assess(defects=300), [_load(tmp_path, d)]) == []


# ---------------------------------------------------------------- computing
def test_compute_multiplies_named_inputs_and_says_where_each_came_from(tmp_path):
    pb = _load(tmp_path, _approved(_example()))
    costs = _costs(value=900)
    assert pbm.ready(pb, costs, date(2026, 10, 15)) == []
    a = _assess(defects=300)                                             # DefectivePcs = 150
    out = pbm.compute(pb.action("add_inline_inspectors"), {"inspectors": 3}, costs, a)
    assert out["cost"]["value"] == 2700.0
    assert out["cost"]["inputs"]["rate"] == {"ref": "cost.qc_inspector_day_rate", "value": 900,
                                             "unit": "<currency> per inspector per day", "owner": "Commercial",
                                             "source": "rate card", "as_of": "2026-10-01"}
    assert out["effect"] == {"calculator": "none", "inputs": {}, "value": None}
    out = pbm.compute(pb.action("rework_defective_pieces"), {}, costs, a)
    assert out["cost"]["value"] == 135000.0 and out["cost"]["inputs"]["quantity"]["value"] == 150.0


def test_compute_refuses_a_choice_outside_its_bounds_or_an_unknown_parameter(tmp_path):
    pb = _load(tmp_path, _approved(_example()))
    act, costs, a = pb.action("add_inline_inspectors"), _costs(value=900), _assess(defects=300)
    for params in ({"inspectors": 9}, {"inspectors": 0}, {"inspectors": 2.5}, {"inspectors": True}, {},
                   {"inspectors": 2, "days": 3}):
        with pytest.raises(ValueError):
            pbm.compute(act, params, costs, a)
    with pytest.raises(ValueError, match="has no value"):
        pbm.compute(act, {"inspectors": 2}, _costs(), a)


def test_the_calculators_are_functions_not_expressions():
    src = (ROOT / "src" / "ragbot" / "domain_agents" / "playbook.py").read_text(encoding="utf-8")
    assert "eval(" not in src and "exec(" not in src
    assert all(callable(fn) and isinstance(needs, tuple) for needs, fn in pbm.CALCULATORS.values())


def test_ready_reports_a_future_approval_or_cost_date(tmp_path):
    d = _approved(_example())
    d["approved_on"] = "2026-12-01"
    costs = _costs(value=900)
    costs["rework_cost_per_piece"]["as_of"] = "2027-01-01"
    reasons = pbm.ready(_load(tmp_path, d), costs, date(2026, 10, 15))
    assert any("approved_on 2026-12-01 is after today" in r for r in reasons)
    assert any("rework_cost_per_piece is dated after today" in r for r in reasons)
