"""Questions that say "my" or "our", read with the signed-in person's identity (Phase U2).

The ERP sign-in (Phase T0) knows who is asking: employee code, company, unit and the factory they point at. A question
that says "my attendance this month" or "our orders shipping next week" is rewritten here, without a model, with
those records in place — "attendance of employee E0456 this month", "TAL orders shipping next week" — and then answered
like any other question: the fixed tools match it, the SQL model is given the code, and every cache keys on the
rewritten text, so one person's "my" answer is never served to another. The progress line shows how it was read.

What this never does: widen access. A factory is put in only when the user's scope already allows it, and only when
the question names no factory itself; an employee code opens no database the scope does not (the people databases'
`restricted_to:` rule decides that, Phase S). Without a known identity (open access, LDAP, a local account without an
`identity:` block) the question is left as it is, and config/clarify.yaml asks for the employee code when it needs one.
"""
from __future__ import annotations

import re
from typing import Optional

from ..auth.models import Identity, Scope

_PERSONAL = re.compile(r"\b(?:my|me|mine|myself|our|ours|us)\b", re.I)
_PEOPLE = re.compile(r"\b(?:attendance|present|absent|absence|salary|pay|payslip|wage|overtime|ot|leave|leaves|"
                     r"in[\s-]?time|out[\s-]?time|punch|punches|shift|holiday|bonus|increment|designation|grade)\b", re.I)
_SITE = re.compile(r"\b(?:my|our)\s+(?:own\s+)?(?:factory|factories|unit|units|plant|company|site|floor)(?:'s)?\b", re.I)
_OURS = re.compile(r"\b(?:my|our)\s+(?=(?:own\s+)?(?:lines?|orders?|production|quality|output|buyers?|styles?|pcds?|"
                   r"shipments?|stock|store|stores|defects?|dhu|cutting|target|plans?|manpower|team)\b)", re.I)


def personal(question: str) -> bool:
    return bool(_PERSONAL.search(question or ""))


def personalize(question: str, me: Optional[Identity], scope: Scope) -> str:
    """The question with the asker's own records in place of "my" / "our", or the question unchanged."""
    q = question or ""
    if me is None or not me.known() or not personal(q):
        return q
    out = q
    if me.employee_code and _PEOPLE.search(q) and re.search(r"\b(?:my|me|mine|myself)\b", q, re.I):
        # "my attendance this month" -> "attendance of employee E0456 this month"
        out = re.sub(r"\bmy\s+(?=\w)", "", out, count=1, flags=re.I)
        out = re.sub(r"\b(?:me|myself)\b", f"employee {me.employee_code}", out, count=1, flags=re.I)
        if me.employee_code not in out:
            out = f"{out.rstrip(' ?.!')} of employee {me.employee_code}" + ("?" if q.rstrip().endswith("?") else "")
        return re.sub(r"\s+", " ", out).strip()
    factory = (me.home_factory or "").upper()
    if not factory or not scope.allows_factory(factory):
        return q                                              # never a factory the user may not see
    try:
        from ..data.entities import question_entities
        if question_entities(q).factories:
            return q                                          # the question names its own factory
    except Exception:
        pass
    if _SITE.search(out):
        out = _SITE.sub(factory, out, count=1)
    elif _OURS.search(out):
        out = _OURS.sub(f"{factory} ", out, count=1)
    return re.sub(r"\s+", " ", out).strip()
