"""What follows "System doesn't have the data." and which answerable questions to offer instead.

CLAUDE.md fixes the first line and asks for one sentence on where the information might be found. That
sentence is for staff, not developers: it never names a view, table, SQL or source id.
- empty():      the user's fixed-tool query ran and returned no rows: name the system and the period
                ("I checked VistaQ for today and found no matching records; …");
- general():    nothing covered the question: the likely domain agent's `where:` sentence
                (config/agents/*.yaml), else prompts/not_found_general.txt;
- unverified(): sources came back but no answer passed verification twice: prompts/not_found_unverified.txt.
suggestions() offers questions that do answer: the same question over a wider period when a period came back
empty, then fixed-tool example questions close to the question. Each reaches a fixed tool, so a click answers
from the data without a model call. An example naming a factory is offered only when the question names it
too, so a suggestion never leads a user outside their scope.
"""
from __future__ import annotations

import re
from typing import Any, Optional, Sequence

from ..config import prompt
from ..models import QueryResult

_PERIOD = re.compile(r"\b(?:today|toady|yesterday|this week|last week|this month|last month|last \d+ days)\b", re.I)
_WIDER = {
    "today": ("yesterday", "this week"),
    "yesterday": ("this week",),
    "this week": ("last week", "this month"),
    "last week": ("this month",),
    "this month": ("last month",),
}
# words that say nothing about the topic: a question and an example sharing only these are not related
_STOP = frozenset("""
a about all an and any are as at be by can could did do does each every find for from get give had has have how i
in into is it its list live me most my of on or our per please show tell than that the their them there these
this those to was we were what when where which who whose why will with you your today yesterday week month
last next many much number total from vistaq data system one two
""".split())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2 and w not in _STOP}


def _period(window: str) -> str:
    """'last 7 days' -> 'the last 7 days'; 'today', 'this week' … unchanged."""
    return f"the {window}" if re.match(r"(?:last|next) \d+ days$", window) else window


def _label(database: str) -> str:
    from ..data.catalog import load_catalogs
    cat = load_catalogs().get(database)
    return cat.display_name if cat is not None else database


def empty(results: Sequence[QueryResult]) -> Optional[str]:
    """The sentence for a fixed-tool query that ran and found nothing, or None. A model-written query's empty
    result is left to general(): it more often means the SQL missed than that the records are not there."""
    r = next((x for x in results if not x.error and not x.denied and not x.rows and x.tool != "generated"), None)
    if r is None:
        return None
    window = str(r.params.get("window") or "")
    if window:
        return prompt("not_found_empty_period").strip().format(source=_label(r.database), period=_period(window))
    return prompt("not_found_empty").strip().format(source=_label(r.database))


def general(question: str) -> str:
    from ..domain_agents import pick_agent
    try:
        agent = pick_agent(question)
    except Exception:          # a broken agents folder must not break a not-found reply
        agent = None
    where = str(agent.extra.get("where") or "").strip() if agent is not None else ""
    return where or prompt("not_found_general").strip()


def unverified() -> str:
    return prompt("not_found_unverified").strip()


def _wider(question: str, window: str) -> list[str]:
    out = []
    for w in _WIDER.get(window, ()):
        if _PERIOD.search(question):
            out.append(_PERIOD.sub(w, question, count=1))
        else:
            q = question.rstrip()
            out.append(f"{q[:-1].rstrip()} {w}?" if q.endswith("?") else f"{q} {w}")
    return out


def suggestions(question: str, results: Sequence[QueryResult] = (), limit: int = 3,
                tools: Optional[list[dict[str, Any]]] = None) -> list[str]:
    from ..data.tools import load_fixed_tools
    from ..domain_agents import pick_agent
    from ..ingest.meta import factory_codes
    out: list[str] = []
    for r in results:
        if not r.error and not r.rows and r.tool != "generated" and r.params.get("window"):
            out += _wider(question, str(r.params["window"]))
            break
    tools = load_fixed_tools() if tools is None else tools
    try:
        agent = pick_agent(question)
    except Exception:
        agent = None
    codes = factory_codes()
    asked = {c for c in codes if re.search(rf"\b{re.escape(c)}\b", question, re.I)}
    qw = _words(question)
    scored = []
    for i, t in enumerate(tools):
        ex = str(t.get("example") or "")
        if not ex or re.search(r"\d{3,}", ex):     # an example about one PO / order id is no use to someone else
            continue
        named = {c for c in codes if re.search(rf"\b{re.escape(c)}\b", ex)}
        if named - asked:
            continue
        score = 2 * len(qw & _words(ex)) + len(qw & _words(str(t.get("description") or "")))
        if agent is not None and t.get("agent") == agent.name:
            score += 2
        if score >= 2:
            scored.append((-score, i, ex))
    asked_q = question.strip().lower()
    for _, _, ex in sorted(scored):
        if ex.strip().lower() != asked_q and ex not in out:
            out.append(ex)
    return [q for q in out if q.strip().lower() != asked_q][:limit]
