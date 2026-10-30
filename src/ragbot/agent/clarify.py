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
    if not r or len(r.split()) > 6:
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
    elif slot == "period":
        from ..data.tools import PERIOD
        if re.fullmatch(rf"(?:for\s+|in\s+|of\s+)?(?:{PERIOD})", r, re.I):
            merged = _insert_before_period(question, r)
    if merged is None:
        return None
    from ..data.tools import needs_clarification
    return merged if needs_clarification(merged) is None else None
