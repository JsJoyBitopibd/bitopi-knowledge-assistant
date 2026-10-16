"""Phase N3a: recommendations and decisions in signals.db (domain_agents/decisions.py).

The Stage-3 gate is closed — no recommendation may reach staff until the Stage-2 backtest passes and real
cost data exists — so the two last tests here matter as much as the refusals: nothing in the app imports
this code, and the router still refuses "approve" in chat.
"""
import ast
import re
import sqlite3
from datetime import date, datetime

import pytest
import yaml

from ragbot.auth.models import Scope, User
from ragbot.config import ROOT
from ragbot.domain_agents import decisions, playbook as pbm, risk, signals
from ragbot.domain_agents.watch import Signal

EXAMPLE = ROOT / "config" / "playbooks" / "quality.example.yaml"
COSTS_EXAMPLE = ROOT / "config" / "costs.example.yaml"
SRC = {"database": "VistaQ_Default_RHL", "views": ["rag.vw_PlanQualityDay"], "sql": "SELECT 1"}
TODAY = date(2026, 10, 15)


def _rs():
    return risk.RuleSet(dimension="quality", window_days=7, red_caveat="Red is provisional.",
                        rules=[dict(name="file_dhu", title="t", measure="dhu", amber=3.85,
                                    amber_source="DHUThreshold", red=6, red_provisional=True, min_checked=200)])


def _assess(subject="FR-26-1985", factory="TAL", defects=300, checked=4000):
    rows = [dict(InspectionDate="2026-10-14", Checked=checked, Defects=defects, Rejected=0,
                 Passed=checked - defects, DefectivePcs=defects // 2, Rectified=0)]
    return risk.evaluate(_rs(), subject, factory, rows)


def _predict(db, *assessments) -> list[int]:
    """Store a risk run and return its prediction ids, in order."""
    run = signals.write_risk_run("quality", list(assessments) or [_assess()], datetime.now(), [], source=SRC,
                                 path=db)
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT id FROM prediction WHERE run_id = ? ORDER BY id", (run,))]
    finally:
        con.close()


def _playbook(tmp_path, status="approved") -> pbm.Playbook:
    d = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    if status == "approved":
        d.update(status="approved", owner="Quality", author="QC manager", approved_by="Head of Quality",
                 approved_on="2026-10-01", source_pdf="data/pdfs/Playbook/quality_playbook.pdf")
        for i, a in enumerate(d["actions"], start=1):
            a["pdf_page"] = i
            for p in (a.get("params") or {}).values():
                p.update(min=1, max=4)
    else:
        d["status"] = status
    f = tmp_path / f"{status}.yaml"
    f.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    return pbm.load_file(f)


def _costs(value=900):
    out = pbm.load_costs(COSTS_EXAMPLE)
    if value is not None:
        for src in out.values():
            src.update(value=value, owner="Commercial", source="rate card", as_of="2026-10-01",
                       unit=str(src.get("unit") or "BDT").replace("<currency>", "BDT"))
    return out


def _costs_file(tmp_path, value=900, name="costs.yaml"):
    """write_recommendation reads the cost file itself; a test writes one."""
    f = tmp_path / name
    f.write_text(yaml.safe_dump(_costs(value), sort_keys=False), encoding="utf-8")
    return f


def _user(name="j.hossain", factories=("TAL",)):
    return User(name=name, scope=Scope(factories=frozenset(factories)))


def _recommend(tmp_path, db, **kw) -> int:
    pid = _predict(db)[0]
    return decisions.write_recommendation(pid, _playbook(tmp_path), "add_inline_inspectors", {"inspectors": 2},
                                          _costs_file(tmp_path), path=db, today=TODAY, **kw)


def _count(db, table) -> int:
    con = sqlite3.connect(db)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


# ---------------------------------------------------------------- recommendations
def test_a_draft_playbook_cannot_produce_a_recommendation(tmp_path):
    db = tmp_path / "signals.db"
    pid = _predict(db)[0]
    with pytest.raises(decisions.Refused, match="is draft, not approved"):
        decisions.write_recommendation(pid, _playbook(tmp_path, "draft"), "add_inline_inspectors",
                                       {"inspectors": 2}, _costs_file(tmp_path), path=db, today=TODAY)
    assert _count(db, "recommendation") == 0


def test_missing_cost_data_refuses_a_recommendation(tmp_path):
    db = tmp_path / "signals.db"
    pid = _predict(db)[0]
    pb = _playbook(tmp_path)
    with pytest.raises(decisions.Refused, match="qc_inspector_day_rate has no value"):
        decisions.write_recommendation(pid, pb, "add_inline_inspectors", {"inspectors": 2}, _costs_file(tmp_path, None),
                                       path=db, today=TODAY)
    with pytest.raises(decisions.Refused, match="not a declared cost source"):
        decisions.write_recommendation(pid, pb, "add_inline_inspectors", {"inspectors": 2},
                                       tmp_path / "no_costs.yaml", path=db, today=TODAY)
    assert _count(db, "recommendation") == 0

    # With the data in place the cost is computed here, and the row says where every number came from.
    rid = decisions.write_recommendation(pid, pb, "add_inline_inspectors", {"inspectors": 2}, _costs_file(tmp_path),
                                         path=db, today=TODAY)
    [rec] = decisions.recommendations(Scope.unrestricted(), "quality", subject="FR-26-1985", path=db)
    assert decisions.recommendations(Scope.unrestricted(), "quality", subject="FR-26-1990", path=db) == []
    assert rec["id"] == rid and rec["prediction_id"] == pid and rec["factory"] == "TAL"
    assert rec["pattern"] == "quality.file_dhu=red" and rec["playbook_sha"] == pb.sha
    assert rec["cost"]["value"] == 1800.0 and rec["params"] == {"inspectors": 2}
    kinds = {c["kind"]: c for c in rec["cites"]}
    assert kinds["prediction"]["database"] == "VistaQ_Default_RHL" and kinds["prediction"]["caveat"]
    assert kinds["playbook"]["source_pdf"] == "data/pdfs/Playbook/quality_playbook.pdf" and kinds["playbook"]["page"] == 1
    assert kinds["cost"] == {"kind": "cost", "source": "qc_inspector_day_rate", "owner": "Commercial",
                             "from": "rate card", "as_of": "2026-10-01", "unit": "BDT per inspector per day"}
    assert rec["decision"] is None


def test_a_recommendation_needs_a_stored_prediction_its_pattern_matches(tmp_path):
    db = tmp_path / "signals.db"
    amber, green = _predict(db, _assess(defects=200), _assess("FR-26-1990", defects=100))
    pb = _playbook(tmp_path)
    for pid in (amber, green):                         # the example answers red only
        with pytest.raises(decisions.Refused, match="does not match"):
            decisions.write_recommendation(pid, pb, "add_inline_inspectors", {"inspectors": 2}, _costs_file(tmp_path),
                                           path=db, today=TODAY)
    with pytest.raises(decisions.Refused, match="does not exist"):
        decisions.write_recommendation(9999, pb, "add_inline_inspectors", {"inspectors": 2}, _costs_file(tmp_path),
                                       path=db, today=TODAY)
    red = _predict(db)[0]
    with pytest.raises(decisions.Refused, match="outside"):     # a chosen value outside the playbook's bounds
        decisions.write_recommendation(red, pb, "add_inline_inspectors", {"inspectors": 40}, _costs_file(tmp_path),
                                       path=db, today=TODAY)
    with pytest.raises(decisions.Refused, match="no action"):
        decisions.write_recommendation(red, pb, "air_freight", {}, _costs_file(tmp_path), path=db, today=TODAY)
    assert _count(db, "recommendation") == 0


# ---------------------------------------------------------------- decisions
def test_the_open_user_cannot_decide(tmp_path):
    db = tmp_path / "signals.db"
    rid = _recommend(tmp_path, db)
    for who in (_user("open", ("*",)), _user("OPEN", ("*",)), _user("  ", ("*",)), None):
        with pytest.raises(decisions.Refused):
            decisions.write_decision(rid, who, "approve", path=db)
    assert _count(db, "decision") == 0


def test_a_decision_outside_the_users_factories_is_refused(tmp_path):
    db = tmp_path / "signals.db"
    rid = _recommend(tmp_path, db)                     # a TAL file
    with pytest.raises(decisions.Refused, match="outside"):
        decisions.write_decision(rid, _user(factories=("RHL",)), "approve", path=db)
    with pytest.raises(decisions.Refused, match="does not exist"):
        decisions.write_decision(rid + 1, _user(), "approve", path=db)
    assert _count(db, "decision") == 0
    decisions.write_decision(rid, _user(), "approve", path=db)
    # Reading is scoped the same way.
    rhl = Scope(factories=frozenset({"RHL"}))
    assert decisions.recommendations(rhl, path=db) == [] and decisions.decisions(rhl, path=db) == []
    assert len(decisions.decisions(_user().scope, path=db)) == 1


def test_a_reject_needs_a_reason(tmp_path):
    db = tmp_path / "signals.db"
    rid = _recommend(tmp_path, db)
    for reason in ("", "   ", None):
        with pytest.raises(decisions.Refused, match="reject needs a reason"):
            decisions.write_decision(rid, _user(), "reject", reason, path=db)
    with pytest.raises(decisions.Refused, match="verdict"):
        decisions.write_decision(rid, _user(), "maybe", "unsure", path=db)
    assert _count(db, "decision") == 0
    decisions.write_decision(rid, _user(), "reject", "the line is already short of inspectors", path=db)


def test_a_later_decision_supersedes_and_neither_can_be_changed(tmp_path):
    db = tmp_path / "signals.db"
    rid = _recommend(tmp_path, db)
    first = decisions.write_decision(rid, _user(), "reject", "no budget this week", path=db)
    second = decisions.write_decision(rid, _user("a.rahman"), "approve", path=db)
    rows = decisions.decisions(Scope.unrestricted(), rid, path=db)
    assert [(d["id"], d["current"]) for d in rows] == [(first, False), (second, True)]
    assert decisions.recommendations(Scope.unrestricted(), path=db)[0]["decision"]["verdict"] == "approve"
    con = sqlite3.connect(db)
    try:
        for sql in ("UPDATE decision SET verdict = 'reject'", "DELETE FROM decision",
                    "UPDATE recommendation SET cost = '{}'", "DELETE FROM recommendation"):
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                con.execute(sql)
    finally:
        con.close()


def test_a_decision_is_joined_to_what_then_happened(tmp_path):
    db = tmp_path / "signals.db"
    rid = _recommend(tmp_path, db)
    decisions.write_decision(rid, _user(), "approve", path=db)
    [row] = decisions.decision_outcomes("quality", path=db)
    assert row["recommendation_id"] == rid and row["verdict"] == "approve" and not row["settled"]
    # The next day's prediction is the one the outcome pass settles (newest per subject).
    later = _predict(db, _assess(defects=100))[0]
    final = _assess(defects=100)
    signals.write_outcome(later, final.level, final.totals, [], path=db)
    [row] = decisions.decision_outcomes("quality", path=db)
    assert row["settled"] and row["outcome_prediction_id"] == later and row["outcome_level"] == "green"
    assert row["level"] == "red"


def test_a_settled_or_superseded_prediction_cannot_be_recommended_on(tmp_path):
    """Review of 2026-10-15: a recommendation on a finished file, or on a level a later day has replaced, would be
    credited with an outcome it could not have caused."""
    db = tmp_path / "signals.db"
    pb = _playbook(tmp_path)
    old = _predict(db)[0]
    new = _predict(db)[0]                                   # the next day's level for the same file
    with pytest.raises(decisions.Refused, match=f"not the newest level .* recommend on prediction {new}"):
        decisions.write_recommendation(old, pb, "add_inline_inspectors", {"inspectors": 2}, _costs_file(tmp_path),
                                       path=db, today=TODAY)
    final = _assess()
    signals.write_outcome(new, final.level, final.totals, [], path=db)
    with pytest.raises(decisions.Refused, match="already settled"):
        decisions.write_recommendation(new, pb, "add_inline_inspectors", {"inspectors": 2}, _costs_file(tmp_path),
                                       path=db, today=TODAY)
    assert _count(db, "recommendation") == 0


def test_a_playbook_or_cost_source_that_does_not_validate_is_refused(tmp_path):
    """ready() alone let a placeholder approver, a path outside data/pdfs and a negative rate through."""
    db = tmp_path / "signals.db"
    pid = _predict(db)[0]
    d = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    d.update(status="approved", owner="Quality", author="QC manager", approved_by="<name>",
             approved_on="2026-10-01", source_pdf="/etc/passwd.pdf")
    for i, a in enumerate(d["actions"], start=1):
        a["pdf_page"] = i
        for p in (a.get("params") or {}).values():
            p.update(min=1, max=4)
    f = tmp_path / "bad.yaml"
    f.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    with pytest.raises(decisions.Refused, match="does not validate"):
        decisions.write_recommendation(pid, pbm.load_file(f), "add_inline_inspectors", {"inspectors": 2},
                                       _costs_file(tmp_path), path=db, today=TODAY)
    negative = _costs_file(tmp_path, -900, name="negative.yaml")
    with pytest.raises(decisions.Refused, match="cannot be negative"):
        decisions.write_recommendation(pid, _playbook(tmp_path), "add_inline_inspectors", {"inspectors": 2},
                                       negative, path=db, today=TODAY)
    assert decisions.recommendations(Scope.unrestricted(), path=db) == []      # refused before anything was written


def test_an_outcome_written_before_the_decision_is_not_credited_to_it(tmp_path):
    db = tmp_path / "signals.db"
    rid = _recommend(tmp_path, db)
    pid = decisions.recommendations(Scope.unrestricted(), path=db)[0]["prediction_id"]
    final = _assess(defects=100)
    signals.write_outcome(pid, final.level, final.totals, [], path=db)
    con = sqlite3.connect(db)
    with con:
        con.execute("UPDATE prediction SET outcome_at = '2026-10-01T00:00:00' WHERE id = ?", (pid,))
    con.close()
    decisions.write_decision(rid, _user(), "approve", path=db)
    [row] = decisions.decision_outcomes("quality", path=db)
    assert row["settled"] and row["after_decision"] is False


def test_a_paused_file_that_resumes_reads_as_open_again(tmp_path):
    """The outcome is read from the subject's newest prediction, settled or not: a file that paused (its newest
    level settled) and resumed (a newer, open level) is still running, not finished on the old outcome."""
    db = tmp_path / "signals.db"
    rid = _recommend(tmp_path, db)
    decisions.write_decision(rid, _user(), "approve", path=db)
    paused = _predict(db)[0]
    final = _assess(defects=100)
    signals.write_outcome(paused, final.level, final.totals, [], path=db)
    [row] = decisions.decision_outcomes("quality", path=db)
    assert row["settled"] and row["outcome_prediction_id"] == paused and row["after_decision"]
    _predict(db)                                            # it resumes
    [row] = decisions.decision_outcomes("quality", path=db)
    assert not row["settled"] and row["outcome_prediction_id"] is None


# ---------------------------------------------------------------- nothing else changes
def _sig():
    return Signal(agent="quality", rule="dhu_today", factory="TAL", level="red", value=6.5, title="t",
                  question="q", weight=2, metric="", database="VistaQ_Default_RHL", views=["rag.v"],
                  sql="SELECT 1", as_of="2026-10-14T10:00:00", values={}, kind="ops")


def _snapshot(db):
    con = sqlite3.connect(db)
    try:
        schema = {t: con.execute(f"PRAGMA table_info({t})").fetchall()
                  for t in ("watch_run", "signal", "risk_run", "prediction")}
    finally:
        con.close()
    run, sigs = signals.latest(Scope.unrestricted(), path=db)
    rrun, preds = signals.risk_latest(Scope.unrestricted(), path=db)
    return (schema, run, [signals.as_dict(s) for s in sigs], rrun, [vars(a) for a in preds],
            signals.open_predictions(path=db), signals.settled(path=db), signals.risk_due(TODAY, path=db))


def test_the_new_tables_do_not_change_the_brief_or_risk_latest(tmp_path):
    db = tmp_path / "signals.db"
    signals.write_run([_sig()], datetime.now(), [], path=db)
    pid = _predict(db)[0]
    before = _snapshot(db)
    rid = decisions.write_recommendation(pid, _playbook(tmp_path), "add_inline_inspectors", {"inspectors": 2},
                                         _costs_file(tmp_path), path=db, today=TODAY)
    decisions.write_decision(rid, _user(), "approve", path=db)
    assert _snapshot(db) == before
    # And a store without the new tables reads as empty, not as an error.
    fresh = tmp_path / "old.db"
    signals.write_run([_sig()], datetime.now(), [], path=fresh)
    assert decisions.recommendations(Scope.unrestricted(), path=fresh) == []
    assert decisions.decision_outcomes("quality", path=fresh) == []


def _imports(path) -> set[str]:
    """Every dotted name a file imports, relative imports included ("from . import x" -> "x")."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            out.add(base)
            out.update(f"{base}.{a.name}" if base else a.name for a in node.names)
    return out


def test_nothing_in_the_app_imports_playbooks_or_decisions():
    """No caller in the app, the UI, the agents or the model code except through domain_agents/recommend.py
    (Phase N2/N3b, 2026-10-16), which the app reaches only for administrators while
    agents.recommendations.enabled is on (tests/test_recommend.py). Only the two modules themselves,
    recommend.py and check_catalog's offline validation may import them."""
    allowed = {"playbook": {"src/ragbot/domain_agents/playbook.py", "src/ragbot/domain_agents/decisions.py",
                            "src/ragbot/domain_agents/recommend.py", "scripts/check_catalog.py"},
               "decisions": {"src/ragbot/domain_agents/decisions.py", "src/ragbot/domain_agents/recommend.py"}}
    files = sorted((ROOT / "src" / "ragbot").rglob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))
    assert any(f.name == "app.py" for f in files)           # the scan really covers the app
    offenders = []
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        names = _imports(f)
        text = f.read_text(encoding="utf-8")
        for mod, ok in allowed.items():
            hit = any(mod in n.split(".") for n in names) or \
                re.search(rf"(import_module|__import__)\(\s*['\"][\w.]*{mod}", text)
            if hit and rel not in ok:
                offenders.append(f"{rel} imports {mod}")
    assert offenders == []


def test_the_router_still_refuses_approve():
    lines = (ROOT / "prompts" / "router.txt").read_text(encoding="utf-8").splitlines()
    assert "refuse     - asks to change, approve, delete or send anything; or is unrelated to Bitopi's work" in lines
    from ragbot.agent.router import route
    assert route("Please approve recommendation 3") == "refuse"
    assert route("Can you approve the extra inspectors for FR-26-1985?") == "refuse"


def test_the_app_reaches_stage_three_only_behind_the_admin_switch():
    """app.py imports recommend and nothing else of Stage 3, and calls it only inside `if _rec.enabled(...)`."""
    text = (ROOT / "src" / "ragbot" / "app.py").read_text(encoding="utf-8")
    assert "from ragbot.domain_agents import recommend as _rec" in text
    head, _, body = text.partition("if _rec.enabled(s.get, scope):")
    assert body, "the Stage-3 panel must sit behind recommend.enabled"
    assert "_rec." not in head.split("from ragbot.domain_agents import recommend as _rec")[1]
