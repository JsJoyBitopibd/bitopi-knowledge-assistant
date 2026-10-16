"""Stage 3 in the app (Phase N2, N3b): what a department's playbook offers for an open Quality risk, and the
approve / reject that follows — for administrators only, and only while `agents.recommendations.enabled` is
true in settings.yaml (default false).

Built on the owner's word of 2026-10-16 ("complete the stage now", option A): the ladder gate stays as far as
staff are concerned. A recommendation rests on the Stage-2 levels, which have not passed their backtest (M✓,
10 Nov at the earliest), and on cost rates nobody has supplied yet, so nothing here is shown to anyone who is
not an administrator, and nothing is shown at all until the setting is switched on.

What an administrator sees is a preview, and says so: for each open red or amber file, the actions of every
playbook that matches it, with the cost only when it can honestly be computed — the playbook approved, every
rate it needs supplied with an owner, a source and a date, and nothing left for a person to choose — and
otherwise the plain reason it cannot be. No model is asked anything: the options, their order and their costs
are Python over the playbook, the rates and the stored prediction (non-negotiable #2). Writing a
recommendation and a decision goes through domain_agents/decisions.py, which refuses a draft playbook, a rate
without a value, a settled or superseded prediction, and the shared open user.

This is the only module the app may import for Stage 3 (tests/test_decisions.py keeps it so).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

from ..auth.models import Scope
from . import decisions, signals
from .playbook import Param, Playbook, compute, load_costs, load_playbooks, match, ready

LEVEL_ORDER = {"red": 0, "amber": 1}


def enabled(get: Callable[[str, Any], Any], scope: Optional[Scope]) -> bool:
    """True only when `agents.recommendations.enabled` is set AND the viewer is an administrator.
    `get` is settings().get. Anything unexpected reads as off."""
    try:
        on = get("agents.recommendations.enabled", False) is True
    except Exception:
        return False
    return bool(on and scope is not None and getattr(scope, "is_admin", False))


@dataclass
class Option:
    """One action a matching playbook offers for one open file."""
    prediction_id: int
    subject: str
    factory: str
    level: str
    playbook: str
    playbook_title: str
    playbook_status: str
    action: str
    action_title: str
    pdf_page: Any
    params: dict[str, Param] = field(default_factory=dict)
    cost: Optional[dict[str, Any]] = None        # the computed block (decisions-ready), or None
    blocked: list[str] = field(default_factory=list)   # why it cannot be recommended as it stands

    @property
    def recommendable(self) -> bool:
        """Approved, ready, and either nothing to choose or the choice is the person's to make."""
        return not self.blocked


def open_predictions(scope: Scope, dimension: str = "quality",
                     path: Optional[Path] = None) -> list[tuple[int, Any]]:
    """(prediction id, assessment) for the newest risk run's predictions that are not settled yet, for the
    factories `scope` may see. Read-only; [] when there is no store or no run."""
    path = path or signals.db_path()
    if not path.exists():
        return []
    con = signals._connect(path, readonly=True)
    try:
        run = con.execute("SELECT id FROM risk_run WHERE dimension = ? ORDER BY id DESC LIMIT 1",
                          (dimension,)).fetchone()
        if run is None:
            return []
        rows = con.execute(
            "SELECT id, dimension, subject, factory, window_days, level, totals, measures, caveat, days "
            "FROM prediction WHERE run_id = ? AND outcome_at IS NULL ORDER BY id", (run[0],)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [(int(r[0]), signals._assessment(r[1:])) for r in rows if scope.allows_factory(r[3])]


def options(scope: Scope, *, playbooks: Optional[list[Playbook]] = None, costs: Optional[dict[str, Any]] = None,
            today: Optional[date] = None, path: Optional[Path] = None, limit: int = 50) -> list[Option]:
    """Every action of every playbook matching an open red or amber file the viewer may see, red first.

    A cost is filled in only when the action has nothing for a person to choose and the playbook is ready;
    otherwise `blocked` says why (a draft, a missing rate, a choice to make). Nothing is written."""
    if playbooks is None:
        playbooks = load_playbooks()
    if costs is None:
        try:
            costs = load_costs()
        except Exception:
            costs = {}
    out: list[Option] = []
    for pid, a in open_predictions(scope, path=path):
        if a.level not in LEVEL_ORDER:
            continue
        for pb in match(a, playbooks):
            reasons = ready(pb, costs, today)
            for act in pb.actions:
                opt = Option(prediction_id=pid, subject=a.subject, factory=a.factory, level=a.level,
                             playbook=pb.id, playbook_title=pb.title, playbook_status=pb.status or "missing",
                             action=act.id, action_title=act.title, pdf_page=act.pdf_page, params=dict(act.params),
                             blocked=list(reasons))
                if not reasons and not act.params:
                    try:
                        opt.cost = compute(act, {}, costs, a)["cost"]
                    except ValueError as e:
                        opt.blocked.append(str(e))
                out.append(opt)
    out.sort(key=lambda o: (LEVEL_ORDER.get(o.level, 9), o.factory, o.subject, o.playbook, o.action))
    return out[:limit]


def cost_text(cost: Optional[dict[str, Any]]) -> str:
    """The computed cost as staff read it, with every input and where it came from; '' when there is none."""
    if not cost or cost.get("value") is None:
        return ""
    parts = []
    for name, used in (cost.get("inputs") or {}).items():
        ref = str(used.get("ref", ""))
        src = f" ({used['owner']}, {used['as_of']})" if ref.startswith("cost.") and used.get("owner") else ""
        parts.append(f"{name} {used.get('value')} from {ref}{src}")
    return f"{cost['value']:,} — " + "; ".join(parts)


def recommend(option: Option, params: Optional[dict[str, Any]] = None, *, playbooks: Optional[list[Playbook]] = None,
              costs_file: Optional[Path] = None, path: Optional[Path] = None, today: Optional[date] = None) -> int:
    """Store this option as a recommendation (decisions.write_recommendation, every refusal included) and
    return its id. The playbook is re-read from disk here, so a file edited since the preview is what counts."""
    pb = next((p for p in (load_playbooks() if playbooks is None else playbooks) if p.id == option.playbook), None)
    if pb is None:
        raise decisions.Refused(f"playbook {option.playbook} is no longer in config/playbooks/")
    return decisions.write_recommendation(option.prediction_id, pb, option.action, params or {}, costs_file,
                                          path=path, today=today)


def decide(recommendation_id: int, user: Any, verdict: str, reason: str = "", *, path: Optional[Path] = None) -> int:
    """Approve or reject a stored recommendation (decisions.write_decision: a named, signed-in user in scope;
    never the shared open user; a reject needs a reason)."""
    return decisions.write_decision(recommendation_id, user, verdict, reason, path=path)


def stored(scope: Scope, *, path: Optional[Path] = None) -> list[dict[str, Any]]:
    """The recommendations already written for the viewer's factories, newest first, each with its current
    decision (or None)."""
    return list(reversed(decisions.recommendations(scope, "quality", path=path)))


def params_summary(params: dict[str, Param]) -> str:
    """What a person chooses for an action, e.g. 'inspectors (people, 1–6)'."""
    bits = []
    for name, p in params.items():
        rng = f", {p.min:g}–{p.max:g}" if p.min is not None and p.max is not None else ""
        bits.append(f"{name} ({p.unit or p.type}{rng})")
    return ", ".join(bits)
