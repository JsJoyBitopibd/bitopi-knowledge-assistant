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
from functools import lru_cache
from typing import Any, Optional, Sequence

from ..config import prompt
from ..models import QueryResult


@lru_cache(maxsize=1)
def _period_re() -> re.Pattern:
    from ..data.tools import PERIOD     # lazy: the data tools pull in the DB and SQL libraries
    return re.compile(rf"\b(?:{PERIOD})\b", re.I)


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
    """The window label as it follows "I checked VistaQ": 'for today', 'for the last 7 days', 'on 6 Oct 2026',
    'in October 2026', 'from 1 Oct 2026 to 5 Oct 2026'."""
    if re.match(r"(?:on|in|from) ", window):
        return window
    return f"for the {window}" if re.match(r"(?:last|next) \d+ days$", window) else f"for {window}"


def _label(database: str) -> str:
    from ..data.catalog import load_catalogs
    cat = load_catalogs().get(database)
    return cat.display_name if cat is not None else database


def empty(results: Sequence[QueryResult]) -> Optional[str]:
    """The sentence for a fixed-tool query that ran and found nothing, or None. A model-written query's empty
    result is left to general(): it more often means the SQL missed than that the records are not there."""
    # "lookup" is a DISTINCT probe the empty-result repair ran (P2), not the user's question: an empty one
    # must not be reported as "I checked <system> and found no matching records" (seen live 2026-10-11).
    r = next((x for x in results if not x.error and not x.denied and not x.rows
              and x.tool not in ("generated", "lookup")), None)
    if r is None:
        return None
    window = str(r.params.get("window") or "")
    if window:
        return prompt("not_found_empty_period").strip().format(source=_label(r.database), period=_period(window))
    return prompt("not_found_empty").strip().format(source=_label(r.database))


def empty_filter(results: Sequence[QueryResult]) -> Optional[str]:
    """P2: a model-written statement with filters ran, the recorded values were looked up, and still nothing
    matched: say what was checked in the user's own words (the literals the statement filtered on — never a
    column, table or recorded value; those go to the follow-up chips)."""
    from .templated import is_blank_result
    r = next((x for x in results if not x.error and not x.denied and x.tool == "generated" and x.filters
              and x.checked and is_blank_result(x.columns, x.rows)), None)
    if r is None:
        return None
    filters = ", ".join(f"'{v}'" for v in dict.fromkeys(r.filters.values()))
    return prompt("not_found_empty_filter").strip().format(source=_label(r.database), filters=filters)


_DB_ERROR = re.compile(r"\('[0-9A-Z]{5}'|timeout|timed out", re.I)


def failed(results: Sequence[QueryResult]) -> Optional[str]:
    """Phase Q: the query itself failed (a timeout, a type conversion) rather than returning nothing. Say what
    happened in one sentence and point at Refresh data, instead of searching the documents for production rows
    they do not hold. A guard refusal is not a database failure: None, so the agent's `where:` sentence speaks."""
    from ..data.errors import friendly_db
    r = next((x for x in results if x.error and not x.denied and _DB_ERROR.search(x.error)), None)
    if r is None:
        return None
    return prompt("not_found_db_error").strip().format(reason=friendly_db(r.error), source=_label(r.database))


def _agent_is_readable(agent, scope) -> bool:
    """Whether any database this agent speaks for is one the user may read (Phase S)."""
    from ..domain_agents import agent_readable
    try:
        from ..data.catalog import load_catalogs
        cats = load_catalogs()
    except Exception:          # the rule cannot be applied: fail closed, the general sentence describes nothing
        return False
    return agent_readable(agent, {n for n, c in cats.items() if scope.allows_database(c.restricted_to)}, cats)


def _denied_for(scope) -> Optional[frozenset[str]]:
    """The databases `scope` may not read, or None when the catalogs cannot be read (Phase S4). The chips fail
    closed on None: a chip is a question the assistant offers, and offering one about a database it cannot vouch
    the user may read is the leak S4 closes."""
    try:
        from ..data.catalog import denied_databases
        return denied_databases(scope)
    except Exception:
        return None


def general(question: str, scope=None) -> str:
    from ..domain_agents import pick_agent
    try:
        agent = pick_agent(question)
    except Exception:          # a broken agents folder must not break a not-found reply
        agent = None
    # Phase S: never describe a database this user may not read. The HR agent's sentence opens "People data I
    # can read is the HRMS — the employee master, daily attendance …", which for a denied user would be both
    # a description of data they have no access to and simply untrue.
    if agent is not None and scope is not None and not _agent_is_readable(agent, scope):
        agent = None
    where = str(agent.extra.get("where") or "").strip() if agent is not None else ""
    return where or prompt("not_found_general").strip()


def unverified() -> str:
    return prompt("not_found_unverified").strip()


def _wider(question: str, window: str) -> list[str]:
    out = []
    for w in _WIDER.get(window, ()):
        if _period_re().search(question):
            out.append(_period_re().sub(w, question, count=1))
        else:
            q = question.rstrip()
            out.append(f"{q[:-1].rstrip()} {w}?" if q.endswith("?") else f"{q} {w}")
    return out


def suggestions(question: str, results: Sequence[QueryResult] = (), limit: int = 3,
                tools: Optional[list[dict[str, Any]]] = None, *, scope) -> list[str]:
    """`scope` is required (Phase S4) so no caller can forget it: a fixed tool's example over a database the
    user may not read ("What is the number of manpower present today?") is never offered, nor a recorded value
    from one, nor the picked agent's bonus when that agent is not theirs. None = scripts: nothing is denied."""
    from ..data.tools import load_fixed_tools
    from ..domain_agents import agent_readable, pick_agent
    from ..ingest.meta import factory_codes
    denied = _denied_for(scope)
    out: list[str] = []
    for r in results:
        if not r.error and not r.rows and r.tool != "generated" and r.params.get("window"):
            out += _wider(question, str(r.params["window"]))
            break
    for r in results:
        # P2: the recorded values a lookup found where the question's word matched nothing — the question with
        # that word replaced, as chips (never as a sentence: a value is a fact, and this reply cites nothing)
        if denied is None or r.database in denied:
            continue
        if r.tool == "generated" and r.candidates and r.filters:
            lit = next((v for v in r.filters.values() if not v.strip().isdigit() and v.lower() in question.lower()), None)
            if lit:
                pat = re.compile(re.escape(lit), re.I)
                out += [pat.sub(lambda m, c=c: c, question, count=1) for c in r.candidates[:3]]
            break
    tools = load_fixed_tools() if tools is None else tools
    if denied is None:
        tools = []                              # fail closed: no example the catalogs cannot vouch for
    else:
        tools = [t for t in tools if t.get("database") not in denied
                 and (t.get("companion") or {}).get("database") not in denied]
    try:
        agent = pick_agent(question)
    except Exception:
        agent = None
    if agent is not None and denied:
        try:
            from ..data.catalog import load_catalogs
            known = load_catalogs()
            if not agent_readable(agent, set(known) - denied, known):
                agent = None                    # its tools are gone already; no bonus for the rest either
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
