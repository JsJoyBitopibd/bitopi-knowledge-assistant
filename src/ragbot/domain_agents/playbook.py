"""Stage-3 playbooks (Phase N1a): config/playbooks/<name>.yaml — a risk pattern and the candidate actions a
department has written down for it, each with the parameters a person chooses and the calculators that give
the action's cost and effect.

NOTHING IN THE APP CALLS THIS MODULE. The owner kept the ladder gate (docs/AGENTS_DESIGN.md §4): no
recommendation reaches staff until the Stage-2 backtest has passed and real cost data exists. What is built
here is the part that needs neither — the file format, its validator (printed by scripts/check_catalog.py),
the readiness check and the calculators — so the department that writes the first playbook gets a precise
problem list instead of a silent mismatch. tests/test_decisions.py keeps it that way: only this module,
domain_agents/decisions.py and scripts/check_catalog.py may import it.

Three rules shape the format.

No number is written by a model, and none is written in a playbook either. A cost is a named calculator
(CALCULATORS) over named inputs: a parameter a person chooses (`param.*`), a rate from the git-ignored
config/costs.yaml (`cost.*`, which carries its owner, source and date) or a count of the subject being judged
(`subject.*`). A literal number as an input is refused, because it would be a number nobody owns. There are
no expression strings and nothing is evaluated.

A typo in `when:` fails at load. `when` decides which risks a playbook answers, and an unknown key there
(`factories:` for `factory:`) would be ignored by a tolerant loader and silently widen the match to every
factory — the same reason data/catalog.py raises on `restricted_to`.

Only red and amber are ever matched. Green is on target, and `info` means too little was checked to judge;
neither is a problem to act on, whatever a file says.
"""
from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

from ..config import ROOT
from .risk import _SUMS as COUNTS

STATUSES = ("draft", "approved", "retired")
MATCH_LEVELS = ("red", "amber")
PARAM_TYPES = ("integer", "number", "boolean", "choice")
NUMERIC = ("integer", "number")
# What `subject.<name>` may name: the counts a Stage-2 assessment carries, and its window.
SUBJECT_FIELDS: tuple[str, ...] = ("days", "window_days") + tuple(COUNTS)

PLAYBOOK_KEYS = ("playbook", "title", "owner", "author", "status", "approved_by", "approved_on", "source_pdf",
                 "when", "actions")
WHEN_KEYS = ("dimension", "rule", "level", "factory")
ACTION_KEYS = ("id", "title", "pdf_page", "params", "cost", "effect")
PARAM_KEYS = ("type", "min", "max", "unit", "choices")
CALC_KEYS = ("calculator", "inputs")
COST_KEYS = ("unit", "value", "owner", "source", "as_of")

_SLUG = re.compile(r"^[a-z][a-z0-9_]*$")
_FACTORY = re.compile(r"^[A-Z0-9]{1,8}$")            # the codes auth.models.Scope accepts
_DIGIT = re.compile(r"\d")                            # \d is Unicode: Bangla digits are digits too
_NUMBER = re.compile(r"[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?")
_PLACEHOLDER = re.compile(r"<[^<>]+>")
_REF_HELP = "param.<name>, cost.<source> or subject.<count>"


# ---------------------------------------------------------------- calculators
def _quantity_x_rate(i: dict[str, float]) -> float:
    return i["quantity"] * i["rate"]


def _none(_i: dict[str, float]) -> None:
    return None


# name -> (the inputs it needs, the function). A registry of plain functions, never an expression language:
# a new calculator is a reviewed line of Python with a test, not a string in a YAML file.
CALCULATORS: dict[str, tuple[tuple[str, ...], Callable[[dict[str, float]], Optional[float]]]] = {
    "quantity_x_rate": (("quantity", "rate"), _quantity_x_rate),
    "none": ((), _none),
}


# ---------------------------------------------------------------- the parsed file
@dataclass(frozen=True)
class Param:
    name: str
    type: str
    min: Optional[float] = None
    max: Optional[float] = None
    unit: str = ""
    choices: tuple[Any, ...] = ()


@dataclass
class Calc:
    calculator: str
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Action:
    id: str
    title: str
    pdf_page: Any = None
    params: dict[str, Param] = field(default_factory=dict)
    cost: Optional[Calc] = None
    effect: Optional[Calc] = None


@dataclass(frozen=True)
class When:
    dimension: str = ""
    rule: str = ""
    level: tuple[str, ...] = ()
    factory: tuple[str, ...] = ()          # empty = every factory


@dataclass
class Playbook:
    id: str
    title: str
    owner: str
    author: str
    status: str
    approved_by: Optional[str]
    approved_on: Optional[date]
    source_pdf: Optional[str]
    when: When
    actions: list[Action]
    file: str                               # the file name, for problems about a playbook without an id
    sha: str                                # SHA-256 of the file: stored with every recommendation it produces
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def action(self, action_id: str) -> Optional[Action]:
        return next((a for a in self.actions if a.id == action_id), None)


def playbooks_dir() -> Path:
    return ROOT / "config" / "playbooks"


def costs_path() -> Path:
    return ROOT / "config" / "costs.yaml"


def _stamp(folder: Path) -> tuple:
    return tuple(sorted((f.name, f.stat().st_mtime_ns) for f in folder.glob("*.yaml"))) if folder.exists() else ()


def load_playbooks(folder: Optional[Path] = None) -> list[Playbook]:
    """Every playbook under config/playbooks/, skipping *.example.yaml. A list, not a dict by id, so that
    two files claiming one id reach validate() instead of one silently replacing the other. Cached on the
    folder's file mtimes, like the risk rules, so an edited playbook is picked up without a restart."""
    folder = folder or playbooks_dir()
    return list(_load(folder, _stamp(folder)))


@lru_cache(maxsize=4)
def _load(folder: Path, _stamp: tuple) -> tuple[Playbook, ...]:
    if not folder.exists():
        return ()
    return tuple(load_file(f) for f in sorted(folder.glob("*.yaml")) if not f.name.endswith(".example.yaml"))


def load_file(path: Path) -> Playbook:
    """One playbook file. Tolerant of everything validate() can report, loud about `when`: a key there that
    is not understood would change which risks the playbook matches without anyone noticing."""
    data = Path(path).read_bytes()
    d = yaml.safe_load(data.decode("utf-8")) or {}
    name = Path(path).name
    if not isinstance(d, dict):
        raise RuntimeError(f"{name}: a playbook file is one mapping ({', '.join(PLAYBOOK_KEYS)})")
    actions = d.get("actions")
    return Playbook(
        id=str(d.get("playbook") or "").strip(), title=str(d.get("title") or "").strip(),
        owner=str(d.get("owner") or "").strip(), author=str(d.get("author") or "").strip(),
        status=str(d.get("status") or "").strip(),
        approved_by=str(d["approved_by"]).strip() if d.get("approved_by") is not None else None,
        approved_on=_as_date(d.get("approved_on")),
        source_pdf=str(d["source_pdf"]).strip() if d.get("source_pdf") is not None else None,
        when=_when(name, d.get("when")),
        actions=[_action(a) for a in actions] if isinstance(actions, list) else [],
        file=name, sha=hashlib.sha256(data).hexdigest(), raw=d)


def _when(name: str, raw: Any) -> When:
    if raw is None:
        return When()
    if not isinstance(raw, dict):
        raise RuntimeError(f"{name}: when must be a mapping of {', '.join(WHEN_KEYS)}")
    unknown = sorted(str(k) for k in raw if k not in WHEN_KEYS)
    if unknown:
        raise RuntimeError(f"{name}: unknown key(s) under when: {', '.join(unknown)} (known: "
                           f"{', '.join(WHEN_KEYS)}) — when decides which risks this playbook answers, and an "
                           f"ignored key would silently widen the match")
    return When(dimension=str(raw.get("dimension") or "").strip(), rule=str(raw.get("rule") or "").strip(),
                level=_words(name, "level", raw.get("level"), str.lower),
                factory=_words(name, "factory", raw.get("factory"), str.upper))


def _words(name: str, key: str, raw: Any, case: Callable[[str], str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise RuntimeError(f"{name}: when.{key} must be a list of words")
    return tuple(case(x.strip()) for x in raw)


def _action(raw: Any) -> Action:
    a = raw if isinstance(raw, dict) else {}
    params = a.get("params") if isinstance(a.get("params"), dict) else {}
    return Action(id=str(a.get("id") or "").strip(), title=str(a.get("title") or "").strip(),
                  pdf_page=a.get("pdf_page"),
                  params={str(k): _param(str(k), v) for k, v in params.items()},
                  cost=_calc(a.get("cost")), effect=_calc(a.get("effect")))


def _param(name: str, raw: Any) -> Param:
    p = raw if isinstance(raw, dict) else {}
    choices = p.get("choices")
    return Param(name=name, type=str(p.get("type") or "").strip(), min=_num(p.get("min")), max=_num(p.get("max")),
                 unit=str(p.get("unit") or "").strip(),
                 choices=tuple(choices) if isinstance(choices, list) else ())


def _calc(raw: Any) -> Optional[Calc]:
    if not isinstance(raw, dict):
        return None
    inputs = raw.get("inputs")
    return Calc(calculator=str(raw.get("calculator") or "").strip(),
                inputs=dict(inputs) if isinstance(inputs, dict) else {})


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _num(v: Any) -> Optional[float]:
    return v if _is_number(v) else None


def _as_date(v: Any) -> Optional[date]:
    """A YAML date (`2026-10-15` parses as one) or an ISO string; None when it is neither."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip())
        except ValueError:
            return None
    return None


def load_costs(path: Optional[Path] = None) -> dict[str, Any]:
    """{source name: {unit, value, owner, source, as_of}} from the git-ignored config/costs.yaml, or {} when
    it does not exist yet. Rates are commercial data and the repository is public, so only
    config/costs.example.yaml (every value null) is committed."""
    path = Path(path) if path else costs_path()
    if not path.exists():
        return {}
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(d, dict):
        raise RuntimeError(f"{path.name}: the cost file is a mapping of source name -> {', '.join(COST_KEYS)}")
    return {str(k): (dict(v) if isinstance(v, dict) else v) for k, v in d.items()}


def load_and_validate(rule_sets: dict[str, Any], folder: Optional[Path] = None,
                      costs_file: Optional[Path] = None) -> tuple[list[Playbook], dict[str, Any], list[str]]:
    """(playbooks, cost sources, problems) — what scripts/check_catalog.py prints. Each playbook file is loaded on
    its own: one that does not load (an unknown key under `when:`, a YAML error) is a problem line, and every other
    file is still loaded and validated. A cost file that does not load is one problem, not a "not declared" line
    for every reference to it."""
    folder = folder or playbooks_dir()
    problems: list[str] = []
    playbooks: list[Playbook] = []
    for f in sorted(folder.glob("*.yaml")) if folder.exists() else []:
        if f.name.endswith(".example.yaml"):
            continue
        try:
            playbooks.append(load_file(f))
        except Exception as e:
            problems.append(f"playbook file {f.name}: {e.__class__.__name__}: {e}")
    try:
        costs = load_costs(costs_file)
    except Exception as e:
        problems.append(f"costs: {e.__class__.__name__}: {e}")
        return playbooks, {}, problems + [line for line in validate(playbooks, rule_sets, {})
                                          if "is not a declared cost source" not in line]
    return playbooks, costs, problems + validate(playbooks, rule_sets, costs)


# ---------------------------------------------------------------- validation
def validate(playbooks: list[Playbook], rule_sets: dict[str, Any], costs: dict[str, Any]) -> list[str]:
    """Problems in the playbooks and the cost sources, as lines check_catalog prints the way it prints the
    risk rules: "playbook {id}: …", "playbook {id}.{action}: …", "costs {source}: …".

    A cost source without a value is NOT a problem here: nobody has supplied the rates yet, and that is
    exactly what ready() reports. A problem is something wrong in what has been written.
    """
    problems = _validate_costs(costs)
    seen: set[str] = set()
    for pb in playbooks:
        problems += _validate_playbook(pb, rule_sets, costs, seen)
    return problems


def _unknown(d: dict[str, Any], known: tuple[str, ...]) -> list[str]:
    return sorted(str(k) for k in d if k not in known)


def _validate_costs(costs: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for name, src in sorted(costs.items()):
        def p(msg: str) -> None:
            out.append(f"costs {name}: {msg}")
        if not _SLUG.match(name):
            p("a source name is lower-case letters, digits and underscores")
        if not isinstance(src, dict):
            p(f"must be a mapping of {', '.join(COST_KEYS)}")
            continue
        for k in _unknown(src, COST_KEYS):
            p(f"unknown key {k!r} (known: {', '.join(COST_KEYS)})")
        if not str(src.get("unit") or "").strip():
            p("missing unit")
        v = src.get("value")
        if v is None:
            continue                       # no value yet: ready() says so; nothing is wrong in the file
        if _PLACEHOLDER.search(str(src.get("unit") or "")):
            p("a value needs a unit with no placeholder: the number means nothing without it")
        if not _is_number(v):
            p(f"value {v!r} is not a number")
        elif v < 0:
            p("value cannot be negative")
        for k, why in (("owner", "the person or department who stands behind the number"),
                       ("source", "where the number comes from: a contract, a rate card, a report")):
            s = str(src.get(k) or "").strip()
            if not s or _PLACEHOLDER.search(s):
                p(f"a value needs {k} ({why})")
        if _as_date(src.get("as_of")) is None:
            p("a value needs as_of, the date it was true")
    return out


def _validate_playbook(pb: Playbook, rule_sets: dict[str, Any], costs: dict[str, Any], seen: set[str]) -> list[str]:
    d = pb.raw
    key = pb.id or pb.file
    out: list[str] = []

    def p(msg: str) -> None:
        out.append(f"playbook {key}: {msg}")

    for k in _unknown(d, PLAYBOOK_KEYS):
        p(f"unknown key {k!r} (known: {', '.join(PLAYBOOK_KEYS)})")
    if not pb.id or not _SLUG.match(pb.id):
        p("needs an id (playbook:) of lower-case letters, digits and underscores")
    elif pb.id in seen:
        p("the id is used by another playbook")
    seen.add(pb.id)
    for k in ("title", "owner", "author"):
        if not str(d.get(k) or "").strip():
            p(f"missing {k}")
    if _DIGIT.search(pb.title):
        p("the title holds a digit; a number comes from a calculator, never from text")
    if pb.status not in STATUSES:
        p(f"status {pb.status!r} is not one of {', '.join(STATUSES)}")
    approved = pb.status == "approved"
    if d.get("approved_on") is not None and pb.approved_on is None:
        p(f"approved_on {d.get('approved_on')!r} is not a date")
    if pb.source_pdf is not None and not (pb.source_pdf.startswith("data/pdfs/") and pb.source_pdf.endswith(".pdf")):
        p("source_pdf is the playbook PDF under data/pdfs/ it was written from (data/pdfs/<folder>/<file>.pdf)")
    if approved:
        if not pb.approved_by:
            p("approved needs approved_by, the named person who approved it")
        if pb.approved_on is None:
            p("approved needs approved_on, the date it was approved")
        if not pb.source_pdf:
            p("approved needs source_pdf, the playbook PDF a recommendation cites")
        for k in ("owner", "author", "approved_by", "source_pdf"):
            if _PLACEHOLDER.search(str(d.get(k) or "")):
                p(f"approved, but {k} is still a placeholder")

    if "when" not in d:
        p("missing when")
    else:
        out += _validate_when(key, pb.when, rule_sets)

    actions = d.get("actions")
    if not isinstance(actions, list) or not actions:
        p("needs at least one action")
        return out
    ids: set[str] = set()
    for i, (raw, act) in enumerate(zip(actions, pb.actions), start=1):
        out += _validate_action(key, i, raw, act, approved, costs, ids)
    return out


def _validate_when(key: str, w: When, rule_sets: dict[str, Any]) -> list[str]:
    out: list[str] = []

    def p(msg: str) -> None:
        out.append(f"playbook {key}: {msg}")

    rs = rule_sets.get(w.dimension)
    if rs is None:
        p(f"when.dimension {w.dimension!r} has no rule set in config/rules/")
    elif rs.rule(w.rule) is None:
        p(f"when.rule {w.rule!r} is not a rule in config/rules/{w.dimension}.yaml")
    if not w.level:
        p("when.level must name red, amber or both")
    bad = [lv for lv in w.level if lv not in MATCH_LEVELS]
    if bad:
        p(f"when.level {', '.join(bad)} cannot be matched: only red and amber are risks to act on (green is on "
          f"target; info means too little was checked to judge)")
    wrong = [f for f in w.factory if not _FACTORY.match(f)]
    if wrong:
        p(f"when.factory {', '.join(wrong)} is not a factory code (empty means every factory)")
    return out


def _validate_action(key: str, i: int, raw: Any, act: Action, approved: bool, costs: dict[str, Any],
                     ids: set[str]) -> list[str]:
    out: list[str] = []
    aid = act.id or f"#{i}"

    def q(msg: str) -> None:
        out.append(f"playbook {key}.{aid}: {msg}")

    if not isinstance(raw, dict):
        q("an action is a mapping of " + ", ".join(ACTION_KEYS))
        return out
    for k in _unknown(raw, ACTION_KEYS):
        q(f"unknown key {k!r} (known: {', '.join(ACTION_KEYS)})")
    if not act.id or not _SLUG.match(act.id):
        q("needs an id of lower-case letters, digits and underscores")
    elif act.id in ids:
        q("the id is used by another action in this playbook")
    ids.add(act.id)
    if not act.title:
        q("missing title")
    elif _DIGIT.search(act.title):
        q("the title holds a digit; a number comes from a calculator, never from text")
    page = raw.get("pdf_page")
    if page is not None and not (isinstance(page, int) and not isinstance(page, bool) and page > 0):
        q(f"pdf_page {page!r} is not a page number")
    if approved and page is None:
        q("approved needs pdf_page, the page of source_pdf this action is written on")

    params = raw.get("params")
    if params is not None and not isinstance(params, dict):
        q("params must be a mapping of name -> type, min, max, unit")
        params = None
    for name, spec in (params or {}).items():
        out += [f"playbook {key}.{aid}: param {name}: {m}" for m in _param_problems(spec, approved)]

    if raw.get("cost") is None:
        q("every action needs a cost block (calculator and inputs)")
    else:
        out += [f"playbook {key}.{aid}: cost{m}" for m in _calc_problems(raw.get("cost"), act.params, costs)]
    if raw.get("effect") is not None:
        out += [f"playbook {key}.{aid}: effect{m}" for m in _calc_problems(raw.get("effect"), act.params, costs)]
    return out


def _param_problems(spec: Any, approved: bool) -> list[str]:
    if not isinstance(spec, dict):
        return ["must be a mapping of " + ", ".join(PARAM_KEYS)]
    out = [f"unknown key {k!r} (known: {', '.join(PARAM_KEYS)})" for k in _unknown(spec, PARAM_KEYS)]
    kind = spec.get("type")
    if kind not in PARAM_TYPES:
        out.append(f"type {kind!r} is not one of {', '.join(PARAM_TYPES)}")
        return out
    lo, hi = spec.get("min"), spec.get("max")
    if kind in NUMERIC:
        for k, v in (("min", lo), ("max", hi)):
            if v is not None and not _is_number(v):
                out.append(f"{k} {v!r} is not a number")
        if _is_number(lo) and _is_number(hi) and lo > hi:
            out.append("min is greater than max")
        if approved and (lo is None or hi is None):
            out.append("approved needs both min and max: a chosen quantity is checked against them")
    elif lo is not None or hi is not None:
        out.append("min and max apply to integer and number parameters only")
    choices = spec.get("choices")
    if kind == "choice":
        if not isinstance(choices, list) or not choices:
            out.append("a choice parameter needs a non-empty list of choices")
    elif choices is not None:
        out.append("choices apply to a choice parameter only")
    return out


def _calc_problems(raw: Any, params: dict[str, Param], costs: dict[str, Any]) -> list[str]:
    """Problems in a cost or effect block, each starting with the text that follows `cost` / `effect`."""
    if not isinstance(raw, dict):
        return [" must be a mapping of calculator and inputs"]
    out = [f": unknown key {k!r} (known: {', '.join(CALC_KEYS)})" for k in _unknown(raw, CALC_KEYS)]
    name = raw.get("calculator")
    if name not in CALCULATORS:
        return out + [f".calculator {name!r} is not one of {', '.join(CALCULATORS)}"]
    needs = CALCULATORS[name][0]
    inputs = raw.get("inputs") if raw.get("inputs") is not None else {}
    if not isinstance(inputs, dict):
        return out + [".inputs must be a mapping of input name -> " + _REF_HELP]
    for n in needs:
        if n not in inputs:
            out.append(f".inputs: {name} needs {n}")
    for n, ref in inputs.items():
        if n not in needs:
            out.append(f".inputs.{n}: {name} takes no input {n!r} (it takes {', '.join(needs) or 'none'})")
            continue
        msg = _ref_problem(ref, params, costs)
        if msg:
            out.append(f".inputs.{n}: {msg}")
    return out


def _literal(ref: Any) -> bool:
    return _is_number(ref) or (isinstance(ref, str) and bool(_NUMBER.fullmatch(ref.strip())))


def _ref_problem(ref: Any, params: dict[str, Param], costs: dict[str, Any]) -> Optional[str]:
    if _literal(ref):
        return (f"{ref!r} is a literal number; an input names {_REF_HELP}, so every number has an owner "
                f"and a source")
    if not isinstance(ref, str) or "." not in ref:
        return f"{ref!r} must be {_REF_HELP}"
    kind, _, name = ref.strip().partition(".")
    if kind == "param":
        prm = params.get(name)
        if prm is None:
            return f"param.{name} is not a parameter of this action"
        if prm.type not in NUMERIC:
            return f"param.{name} is a {prm.type or 'untyped'} parameter; a calculator input is an integer or a number"
    elif kind == "cost":
        if name not in costs:
            return f"cost.{name} is not a declared cost source (config/costs.yaml)"
    elif kind == "subject":
        if name not in SUBJECT_FIELDS:
            return f"subject.{name} is not one of {', '.join(SUBJECT_FIELDS)}"
    else:
        return f"{ref!r} must be {_REF_HELP}"
    return None


# ---------------------------------------------------------------- readiness, matching, computing
def _refs(action: Action) -> list[str]:
    # stripped, as validate() and compute() read them: " cost.x" must be the same source to all three
    return [str(r).strip() for c in (action.cost, action.effect) if c is not None for r in c.inputs.values()]


def ready(pb: Playbook, costs: dict[str, Any], today: Optional[date] = None) -> list[str]:
    """Why this playbook cannot produce a recommendation today; [] when it can.

    Separate from validate() on purpose: a draft, or a rate nobody has supplied yet, is not an error in the
    file — it is the honest state of Stage 3 today, and check_catalog stays at 0 problems while it lasts.
    """
    today = today or date.today()
    out: list[str] = []
    if pb.status != "approved":
        out.append(f"status is {pb.status or 'missing'}, not approved")
    else:
        if not pb.approved_by:
            out.append("approved, but nobody is named in approved_by")
        if pb.approved_on is None:
            out.append("approved, but approved_on is not a date")
        elif pb.approved_on > today:
            out.append(f"approved_on {pb.approved_on.isoformat()} is after today")
        if not pb.source_pdf:
            out.append("no source_pdf to cite")
    if not pb.actions:
        out.append("no actions")
    for a in pb.actions:
        if pb.status == "approved" and not (isinstance(a.pdf_page, int) and not isinstance(a.pdf_page, bool)
                                            and a.pdf_page > 0):
            out.append(f"{a.id}: no pdf_page to cite")
        if a.cost is None:
            out.append(f"{a.id}: no cost block")
        for c in (a.cost, a.effect):
            if c is not None and c.calculator not in CALCULATORS:
                out.append(f"{a.id}: calculator {c.calculator!r} does not exist")
        for prm in a.params.values():
            if prm.type in NUMERIC and (prm.min is None or prm.max is None):
                out.append(f"{a.id}: param {prm.name} has no min and max to check a chosen value against")
        for ref in _refs(a):
            if _literal(ref):
                out.append(f"{a.id}: {ref!r} is a literal number")
                continue
            kind, _, name = ref.partition(".")
            if kind != "cost":
                continue
            src = costs.get(name)
            if not isinstance(src, dict):
                out.append(f"cost source {name} is not declared in config/costs.yaml")
            elif src.get("value") is None:
                out.append(f"cost source {name} has no value yet (owner: {src.get('owner') or 'not named'})")
            elif not _is_number(src.get("value")):
                out.append(f"cost source {name} has a value that is not a number")
            elif src.get("value") < 0:
                out.append(f"cost source {name} has a negative value")
            elif not (src.get("owner") and src.get("source")):
                out.append(f"cost source {name} has a value without an owner and a source")
            elif any(_PLACEHOLDER.search(str(src.get(k) or "")) for k in ("owner", "source", "unit")):
                out.append(f"cost source {name} still has a placeholder in its owner, source or unit")
            elif _as_date(src.get("as_of")) is None:
                out.append(f"cost source {name} has a value without as_of")
            elif _as_date(src.get("as_of")) > today:
                out.append(f"cost source {name} is dated after today")
    return list(dict.fromkeys(out))


def pattern_of(pb: Playbook, assessment: Any) -> Optional[str]:
    """The risk pattern this playbook answers in `assessment` ("quality.file_dhu=red"), or None.

    Matched on the named rule's own level, not the subject's overall level: a playbook for DHU must not fire
    because the reject rate is red. Green and info never match, whatever the file says.
    """
    w = pb.when
    if pb.status == "retired" or not w.dimension or w.dimension != getattr(assessment, "dimension", None):
        return None
    if w.factory and str(getattr(assessment, "factory", "")).strip().upper() not in w.factory:
        return None
    m = next((m for m in getattr(assessment, "measures", []) if m.rule == w.rule), None)
    if m is None or m.level not in MATCH_LEVELS or m.level not in w.level:
        return None
    return f"{w.dimension}.{w.rule}={m.level}"


def match(assessment: Any, playbooks: list[Playbook]) -> list[Playbook]:
    """The playbooks (any status but retired) whose `when` matches this assessment. Approval and readiness
    are checked where a recommendation is written (decisions.write_recommendation), not here."""
    return [pb for pb in playbooks if pattern_of(pb, assessment)]


def check_params(action: Action, params: dict[str, Any]) -> dict[str, Any]:
    """The chosen parameters, checked against the action's declarations. Raises ValueError."""
    chosen = dict(params or {})
    extra = sorted(set(chosen) - set(action.params))
    if extra:
        raise ValueError(f"{action.id}: unknown parameter(s) {', '.join(extra)}")
    for name, prm in action.params.items():
        if name not in chosen:
            raise ValueError(f"{action.id}: parameter {name} was not given")
        v = chosen[name]
        ok = {"integer": isinstance(v, int) and not isinstance(v, bool),
              "number": _is_number(v),
              "boolean": isinstance(v, bool),
              "choice": v in prm.choices}.get(prm.type)
        if ok is None:
            raise ValueError(f"{action.id}: parameter {name} has no valid type")
        if not ok:
            raise ValueError(f"{action.id}: parameter {name} = {v!r} is not a valid {prm.type}")
        if prm.type in NUMERIC:
            if prm.min is None or prm.max is None:
                raise ValueError(f"{action.id}: parameter {name} has no min and max to check {v!r} against")
            if not prm.min <= v <= prm.max:
                raise ValueError(f"{action.id}: parameter {name} = {v!r} is outside {prm.min}..{prm.max}")
    return chosen


def _resolve(ref: Any, action: Action, chosen: dict[str, Any], costs: dict[str, Any],
             assessment: Any) -> tuple[float, dict[str, Any]]:
    if _literal(ref):
        raise ValueError(f"{action.id}: {ref!r} is a literal number")
    kind, _, name = str(ref).strip().partition(".")
    info: dict[str, Any] = {"ref": str(ref)}
    if kind == "param" and name in action.params:
        v, info["unit"] = chosen.get(name), action.params[name].unit
    elif kind == "cost":
        src = costs.get(name)
        if not isinstance(src, dict) or src.get("value") is None:
            raise ValueError(f"{action.id}: cost source {name} has no value")
        v = src["value"]
        as_of = _as_date(src.get("as_of"))
        info.update(unit=str(src.get("unit") or ""), owner=src.get("owner"), source=src.get("source"),
                    as_of=as_of.isoformat() if as_of else None)
    elif kind == "subject" and name in SUBJECT_FIELDS:
        totals = getattr(assessment, "totals", {}) or {}
        if name not in ("days", "window_days") and name not in totals:
            # a 0 here would be a computed cost no source holds (an older or partial prediction stores '{}')
            raise ValueError(f"{action.id}: the prediction holds no {name} count")
        v = getattr(assessment, name) if name in ("days", "window_days") else totals[name]
        info.update(subject=getattr(assessment, "subject", ""), factory=getattr(assessment, "factory", ""))
    else:
        raise ValueError(f"{action.id}: {ref!r} must be {_REF_HELP}")
    if not _is_number(v):
        raise ValueError(f"{action.id}: {ref} is not a number")
    info["value"] = v
    return float(v), info


def _run(calc: Calc, action: Action, chosen: dict[str, Any], costs: dict[str, Any],
         assessment: Any) -> dict[str, Any]:
    if calc.calculator not in CALCULATORS:
        raise ValueError(f"{action.id}: calculator {calc.calculator!r} does not exist")
    needs, fn = CALCULATORS[calc.calculator]
    if set(calc.inputs) != set(needs):
        raise ValueError(f"{action.id}: {calc.calculator} takes {', '.join(needs) or 'no inputs'}, "
                         f"not {', '.join(sorted(map(str, calc.inputs))) or 'none'}")
    values: dict[str, float] = {}
    used: dict[str, Any] = {}
    for n in needs:
        values[n], used[n] = _resolve(calc.inputs[n], action, chosen, costs, assessment)
    result = fn(values)
    return {"calculator": calc.calculator, "inputs": used,
            "value": None if result is None else round(float(result), 2)}


def compute(action: Action, params: dict[str, Any], costs: dict[str, Any], assessment: Any) -> dict[str, Any]:
    """{"cost": …, "effect": …} for one action, computed here in Python and never by a model
    (non-negotiable #2). Each block names its calculator, every input with where it came from, and the
    value (None for the `none` calculator: no effect is claimed). Raises ValueError on anything missing."""
    if action.cost is None:
        raise ValueError(f"{action.id}: no cost block")
    chosen = check_params(action, params)
    return {"cost": _run(action.cost, action, chosen, costs, assessment),
            "effect": _run(action.effect or Calc("none"), action, chosen, costs, assessment)}
