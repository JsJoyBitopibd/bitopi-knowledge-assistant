"""A clarifying question that remembers it was asked (Phase T6).

config/clarify.yaml asks "For which factory?" or "Which export order?" instead of guessing. Until v1.14.0 the
reply ("TAL") arrived as a new message with nothing tying it to the question, and leaned on the rewrite model to put
the two together. Now the answer that asked carries `clarify: {slot, choices}`, the conversation state keeps it as
the pending clarification, and the reply is merged into the original question here, with no model call:

    "how many PPM meetings this week?"  +  "TAL"  ->  "how many PPM meetings at TAL this week?"

A reply that is not an answer to the question (more than a few words, or no value of the asked kind) is left alone
and treated as a new message; the pending clarification is dropped either way.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..auth.models import Scope
from ..config import settings

_ORDER = re.compile(r"\b[A-Z]{2,4}-\d{2,4}-\d{2,5}(?:-\d+)?\b", re.I)
_ALL = re.compile(r"^(?:for\s+|in\s+|at\s+)?(?:all(?:\s+(?:factories|units|of\s+them))?|every\s+factory)$", re.I)


def choices(rule: dict[str, Any], scope: Scope) -> list[str]:
    """The reply chips for a rule: `choices: factories` is the factories this user may see (never wider), a list is
    itself; nothing else is offered."""
    spec = rule.get("choices")
    if spec == "factories":
        codes = [str(c) for c in settings().get("scope.factory_codes", []) or []]
        mine = [c for c in codes if scope.allows_factory(c)]
        return mine + (["All factories"] if scope.all_factories and len(mine) > 1 else [])
    if isinstance(spec, list):
        return [str(x) for x in spec]
    return []


def _insert_before_period(question: str, phrase: str) -> str:
    """'how many PPM meetings this week?' + 'at TAL' -> 'how many PPM meetings at TAL this week?'"""
    from ..data.tools import PERIOD
    q = question.strip()
    end = q[-1] if q and q[-1] in "?.!" else ""
    body = q[:-1].rstrip() if end else q
    m = None
    for m in re.finditer(rf"\b(?:{PERIOD})\b", body, re.I):
        pass                                                  # the last period phrase in the question
    if m is None:
        return f"{body} {phrase}{end}"
    return f"{body[:m.start()].rstrip()} {phrase} {body[m.start():]}{end}"


def merge_reply(pending: Optional[dict[str, Any]], reply: str) -> Optional[str]:
    """The original question completed with the reply, or None when the reply does not answer what was asked."""
    if not pending or not pending.get("question"):
        return None
    r = re.sub(r"\s+", " ", reply or "").strip().strip("?.!").strip()
    # a click on a chip is always a reply, whatever its length ("All tests of the last 90 days"); typed text of more
    # than a few words is a message of its own
    chip = r.casefold() in {str(c).casefold() for c in pending.get("choices") or []}
    if not r or (len(r.split()) > 6 and not chip):
        return None
    question, slot = str(pending["question"]), pending.get("slot")
    merged: Optional[str] = None
    if slot == "factory":
        if _ALL.match(r):
            merged = _insert_before_period(question, "in all factories")
        else:
            from ..data.entities import question_entities
            found = question_entities(r).factories
            listed = [c for c in pending.get("choices") or [] if c.lower() == r.lower().removeprefix("for ").strip()]
            code = found[0] if len(found) == 1 else (listed[0] if listed else None)
            if code and code.lower() != "all factories":
                merged = _insert_before_period(question, f"at {code.upper()}")
    elif slot == "order":
        m = _ORDER.search(r)
        if m:
            eo = m.group(0).upper()
            sub = re.sub(r"\b(?:the|my|this|that|our|an?)\s+(?:export\s+)?(?:order|eo)\b", f"export order {eo}",
                         question, count=1, flags=re.I)
            merged = sub if sub != question else _insert_before_period(question, f"for export order {eo}")
    elif slot == "employee":
        # Phase U2: "E0456", "my code is E0456" -> the question about that employee
        m = re.search(r"\b([A-Za-z]{0,4}-?\d{2,})\b", r)
        if m:
            code = m.group(1).upper()
            body = re.sub(r"\b(?:my|myself)\s*", "", question, count=1, flags=re.I).strip()
            end = body[-1] if body and body[-1] in "?.!" else ""
            merged = f"{body[:-1] if end else body} of employee {code}{end}"
    elif slot == "period":
        from ..data.tools import PERIOD
        if re.fullmatch(rf"(?:for\s+|in\s+|of\s+)?(?:{PERIOD})", r, re.I):
            merged = _insert_before_period(question, r)
    elif slot in ("buyer", "file", "buyer_or_file"):
        merged = _merge_key(pending, question, r)
    elif slot == "source":
        merged = _merge_source(pending, question, r)
    if merged is None:
        return None
    from ..data.tools import needs_clarification
    return merged if needs_clarification(merged) is None else None


_FILE = re.compile(r"\bFR-\d{2}-\d{1,6}\b", re.I)


def _pick(choices: list[str], reply: str) -> Optional[str]:
    """The chip the reply names: the same text, or the start of exactly one chip ("Decor" for "Decor (Target USA)"
    when no other chip starts so)."""
    low = reply.casefold().removeprefix("for ").strip()
    same = [c for c in choices if c.casefold() == low]
    if same:
        return same[0]
    starts = [c for c in choices if c.casefold().startswith(low)]
    return starts[0] if len(starts) == 1 else None


def _merge_key(pending: dict[str, Any], question: str, r: str) -> Optional[str]:
    """Phase W: a buyer or a file for a fixed tool that asked (data/tools.py ClarificationNeeded). "FR-26-2118" ->
    "… for file FR-26-2118"; a chip or a recorded buyer name -> "… for Decor (Target USA)", which the tool's value
    parameter binds as the buyer's code; the "all" chip -> the question over the tool's own wider period."""
    slot = pending.get("slot")
    choices = [str(c) for c in pending.get("choices") or []]
    all_label = str(pending.get("all") or "")
    if all_label and (r.casefold() == all_label.casefold() or _ALL.match(r) or r.casefold() in ("everything", "any")):
        period = str(pending.get("all_period") or "")
        return _insert_before_period(question, period) if period else None
    if slot in ("file", "buyer_or_file"):
        m = _FILE.search(r)
        if m:
            return _insert_before_period(question, f"for file {m.group(0).upper()}")
    if slot in ("buyer", "buyer_or_file"):
        listed = _pick([c for c in choices if c != all_label], r)
        if listed:
            return _insert_before_period(question, f"for {listed}")
        try:
            from ..auth.models import Scope as _Scope
            from ..data.values import resolve
            hit, _ = resolve(r, _Scope.unrestricted(), str(pending.get("entity") or "ppc_buyer"))
        except Exception:
            hit = None
        if hit is not None:
            return _insert_before_period(question, f"for {hit.value}")
    return None


def _merge_source(pending: dict[str, Any], question: str, r: str) -> Optional[str]:
    """Phase W: which system a word like "goods receipt" means. The clarify.yaml rule that asked names the phrase to
    swap (`replace:`) and what each chip swaps it for (`replace_with:`): "Goods Receipt yesterday" + "Fabric received
    in PPC" -> "fabric received in PPC yesterday". A reply that is one distinctive word of a chip ("ppc", "store")
    picks it too."""
    from ..data.tools import clarification_rule_named
    rule = clarification_rule_named(str(pending.get("rule") or ""))
    phrases = (rule or {}).get("replace_with") or {}
    if not rule or not rule.get("replace") or not isinstance(phrases, dict):
        return None
    key = _pick([str(k) for k in phrases], r)
    if key is None:
        words = set(re.findall(r"[a-z]{3,}", r.casefold()))
        hits = [k for k in phrases if words & set(re.findall(r"[a-z]{3,}", str(k).casefold())) - {"the", "and"}]
        key = hits[0] if len(hits) == 1 else None
    if key is None:
        return None
    swapped = re.sub(str(rule["replace"]), str(phrases[key]), question, count=1)
    return swapped if swapped != question else None
