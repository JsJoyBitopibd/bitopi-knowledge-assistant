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

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..auth.models import Scope
from . import decisions, signals
from .playbook import Param, Playbook, compute, load_costs, load_playbooks, match, ready

LEVEL_ORDER = {"red": 0, "amber": 1}
# Data minimisation (non-negotiable #4): one explanation sends at most this many options and playbook pages.
MAX_EXPLAINED = 8


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


# ---------------------------------------------------------------- N2: ranked, then explained (Phase N, 2026-11-03)
# The order is the program's, never the model's: for one file, the options with a computed cost come first,
# cheapest first; then the ones whose cost depends on a quantity a person still chooses; options that cannot be
# recommended are listed by the preview but never ranked or explained. The executive-tier model only explains
# that order, from the stored risk level [D1], the ranked options and their computed costs [D2], and the
# playbook page each action is written on [P#] - and its text goes through the same citation and number check
# as every answer (agent/citations.verify). It is shown to administrators in the preview, never stored.

def rank(opts: list[Option]) -> list[Option]:
    """The recommendable options, per file, in the order they are put to a person. Pure."""
    def key(o: Option) -> tuple:
        cost = o.cost.get("value") if o.cost else None
        return (LEVEL_ORDER.get(o.level, 9), o.factory, o.subject,
                0 if cost is not None else 1, cost if cost is not None else 0, o.playbook, o.action)
    return sorted((o for o in opts if o.recommendable), key=key)


@dataclass
class Explanation:
    """The ranked options of one file and, when it could be verified, the model's explanation of them."""
    prediction_id: int
    ranked: list[Option] = field(default_factory=list)
    text: str = ""
    ok: bool = False
    references: list[Any] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _prediction(pid: int, scope: Scope, path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """The stored prediction `pid` with what a [D#] card needs, or None when it is missing, settled or out of
    the viewer's scope. Read-only."""
    path = path or signals.db_path()
    if not path.exists():
        return None
    con = signals._connect(path, readonly=True)
    try:
        r = con.execute("SELECT subject, factory, level, window_days, totals, measures, caveat, as_of, outcome_at "
                        "FROM prediction WHERE id = ?", (pid,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if r is None or r[8] is not None or not scope.allows_factory(r[1]):
        return None
    return dict(subject=r[0], factory=r[1], level=r[2], window_days=int(r[3] or 7), totals=json.loads(r[4] or "{}"),
                measures=json.loads(r[5] or "[]"), caveat=r[6] or "", as_of=r[7])


def _level_source(pid: int, p: dict[str, Any]) -> Any:
    """[D1]: the stored risk level, its measures and the counts it came from."""
    from ..models import QueryResult
    cols = ["Factory", "FileNo", "Level", "WindowDays", "Checked", "Defects"]
    row: list[Any] = [p["factory"], p["subject"], p["level"], p["window_days"],
                      int(p["totals"].get("Checked", 0)), int(p["totals"].get("Defects", 0))]
    for m in p["measures"]:
        name = str(m.get("measure") or "")
        cols += [name, f"{name}_target", f"{name}_red"]
        row += [m.get("value"), m.get("amber"), m.get("red")]
    if p["caveat"]:
        cols.append("Caveat")
        row.append(p["caveat"])
    try:
        as_of = datetime.fromisoformat(str(p["as_of"]))
    except ValueError:
        as_of = datetime.now()
    return QueryResult(database="Risk levels (signals.db)", engine="local", views=["prediction"],
                       sql=f"stored prediction {pid}", tool="risk_level", columns=cols, rows=[row],
                       key_columns=["FileNo"], as_of=as_of)


def _options_source(ranked: list[Option]) -> Any:
    """[D2]: the options in the program's order, each with its computed cost and the rates it used."""
    from ..models import QueryResult
    rows = []
    for i, o in enumerate(ranked, start=1):
        if o.cost and o.cost.get("value") is not None:
            cost, inputs = o.cost["value"], cost_text(o.cost).split(" — ", 1)[-1]
        else:
            cost, inputs = "", "chosen by a person: " + params_summary(o.params)
        rows.append([i, o.action_title, o.playbook_title, cost, inputs])
    return QueryResult(database="Playbooks and cost rates", engine="local",
                       views=["config/playbooks", "config/costs.yaml"], sql="computed in Python (playbook.compute)",
                       tool="recommend:ranked", columns=["Rank", "Action", "Playbook", "Cost", "CostInputs"],
                       rows=rows, transform="ranked by computed cost, lowest first; a person's choice last")


def _page_chunks(ranked: list[Option], playbooks: list[Playbook], scope: Scope,
                 registry: Any = None, store: Any = None) -> tuple[list[Any], list[str]]:
    """The playbook page each ranked action is written on, as chunks the viewer may see. Returns (chunks,
    problems): an action whose page is not in the index is named, never paraphrased from memory."""
    from ..auth.filters import scope_where
    if registry is None:
        from ..store import Registry
        registry = Registry()
    if store is None:
        from ..store import get_store
        store = get_store()
    where = scope_where(scope)
    pdf = {pb.id: pb.source_pdf for pb in playbooks}
    chunks: list[Any] = []
    problems: list[str] = []
    seen: set[tuple[str, int]] = set()
    for o in ranked:
        source = Path(str(pdf.get(o.playbook) or "")).name
        page = o.pdf_page
        if not source or not isinstance(page, int) or isinstance(page, bool):
            problems.append(f"{o.action}: no playbook page to cite")
            continue
        if (source, page) in seen:
            continue
        seen.add((source, page))
        ids = [r[0] for r in registry.db.execute("SELECT id FROM chunk WHERE source = ? AND page = ? ORDER BY id",
                                                 (source, page)).fetchall()]
        got = [c for c in store.get(ids) if not c.superseded
               and all(str(getattr(c, f, "")) in allowed for f, allowed in where.items())]
        if not got:
            problems.append(f"{o.action}: page {page} of {source} is not in the index")
            continue
        chunks.append(got[0])
        if len(chunks) >= MAX_EXPLAINED:
            break
    return chunks, problems


def _shape_problems(text: str, n: int, stop_reason: Any = "") -> list[str]:
    """What the citation check cannot see. A reply cut off by the length limit is refused whatever it holds
    (on 3 Nov a cut-off fragment of the model's own reasoning carried valid markers and passed the check), and
    the explanation must be one numbered item per ranked option, 1 to n in order, and nothing numbered beyond."""
    import re
    if str(stop_reason or "").lower() in ("length", "max_tokens"):
        return ["the reply was cut off by the length limit"]
    items = [int(m) for m in re.findall(r"(?m)^\s*(\d{1,2})[.)]\s", text)]
    if items != list(range(1, n + 1)):
        return [f"expected one numbered item per option, 1 to {n} in order; found {items or 'none'}"]
    return []


def explain(prediction_id: int, scope: Scope, *, opts: Optional[list[Option]] = None,
            playbooks: Optional[list[Playbook]] = None, model: Any = None, registry: Any = None, store: Any = None,
            path: Optional[Path] = None, user: str = "") -> Explanation:
    """Rank the ready options for one open file and have the executive-tier model explain that order, verified.

    No model call when nothing is ready, the prediction is not the viewer's, or no playbook page can be cited.
    The text is kept only when every marker resolves and every number is in a cited source; one stricter
    retry, then ok False with the reasons (the ranked list is still returned)."""
    from ..agent.citations import normalize_markers, sources_block, verify
    from ..config import prompt, settings

    out = Explanation(prediction_id)
    p = _prediction(prediction_id, scope, path=path)
    if p is None:
        out.problems.append("this file has no open risk level you may see")
        return out
    if playbooks is None:
        playbooks = load_playbooks()
    if opts is None:
        opts = options(scope, playbooks=playbooks, path=path)
    out.ranked = rank([o for o in opts if o.prediction_id == prediction_id])[:MAX_EXPLAINED]
    if not out.ranked:
        out.problems.append("no option is ready to recommend for this file")
        return out
    chunks, missing = _page_chunks(out.ranked, playbooks, scope, registry=registry, store=store)
    out.problems += missing
    if not chunks:
        out.problems.append("no playbook page could be cited, so nothing is explained")
        return out
    results = [_level_source(prediction_id, p), _options_source(out.ranked)]
    block, refs = sources_block(chunks, results)
    out.references = refs
    nf = settings().get("answer.not_found_text", "System doesn't have the data.")
    ask = (f"{block}\n\nFile {p['subject']} at {p['factory']} is at risk level {p['level']}. "
           f"Explain the options in [D2] in exactly that order.\n{prompt('recommend_explain')}")
    if model is None:
        from ..llm import get_model
        model = get_model("recommend")
    problems: list[str] = []
    for attempt in (1, 2):
        content = ask if attempt == 1 else (ask + "\n\nYour previous text was refused: " + "; ".join(problems)
                                           + ". Use only markers listed in <sources> and only numbers that appear "
                                             "in the sources you cite.")
        # Gemini counts its thinking against max_tokens: on 3 Nov every live reply at 900 and four in five at 2048
        # stopped on the length limit after under 250 tokens of visible text (llm.max_tokens_recommend).
        reply = model.chat([{"role": "user", "content": content}], system=prompt("recommend_system"),
                           max_tokens=int(settings().get("llm.max_tokens_recommend", 4096)), temperature=0.0,
                           purpose="recommend", sent_chunk_ids=[c.id for c in chunks],
                           sent_rows=sum(len(r.rows) for r in results), user=user)
        text = (reply.text or "").strip()
        ok, problems = verify(text, chunks, results, nf)
        if ok and text.startswith(nf):
            ok, problems = False, ["the model found nothing to explain"]
        elif ok:
            problems = _shape_problems(text, len(out.ranked), getattr(reply, "stop_reason", ""))
            ok = not problems
        if ok:
            out.text, out.ok = normalize_markers(text), True
            return out
    out.problems.append("the explanation could not be verified: " + "; ".join(problems))
    return out
