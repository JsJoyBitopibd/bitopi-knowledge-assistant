"""What the assistant can answer, for this user (Phase T7).

"Which database access do you have?", "tell me what information I can get from the Inventory?" and "hi" all got the
one fixed paragraph in prompts/chitchat.txt, whoever asked. Here the answer is built from the configuration that
decides what is answered — the domain agents (config/agents/*.yaml: title, description, `where:`) and the fixed
tools' example questions — limited to the databases this user may read (Phase S4: a restricted database is never
described to someone it will not answer). No table, view or column name is ever part of it.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..config import prompt

_TECH = re.compile(r"\brag\.|\bdbo\.|\bvw_|\b[A-Za-z]+_[A-Za-z]+\b|\b[a-z]+[A-Z][A-Za-z]+\b")


def _plain(text: str) -> str:
    """The sentences of a description that name no table, view or column (rag.vw_X, dbo.Y, Snake_case, camelCase)."""
    sentences = re.split(r"(?<=[.;])\s+", (text or "").strip())
    return " ".join(s for s in sentences if s and not _TECH.search(s)).strip()


def _readable(scope) -> tuple[list[Any], frozenset[str]]:
    """(the agents this user may use, the databases they may not read). Fails closed: nothing on an error."""
    try:
        from ..data.catalog import denied_databases, load_catalogs
        from ..domain_agents import agent_readable, load_agents
        known = load_catalogs()
        denied = denied_databases(scope, known)
        readable = {n: c for n, c in known.items() if n not in denied}
        agents = [a for a in load_agents().values() if agent_readable(a, readable, known)]
        return agents, denied
    except Exception:
        return [], frozenset({"*"})


def examples(scope, agent: str = "", limit: int = 3) -> list[str]:
    """Fixed tools' example questions over databases the user may read — each one answered without a model call."""
    agents, denied = _readable(scope)
    if "*" in denied:
        return []
    try:
        from ..data.tools import load_fixed_tools, match_fixed_tool
        tools = load_fixed_tools()
    except Exception:
        return []
    general, specific = [], []
    for t in tools:
        if not t.get("example") or t.get("database") in denied or (t.get("companion") or {}).get("database") in denied:
            continue
        if agent and t.get("agent") != agent:
            continue
        if len(t["example"]) > 100 or not match_fixed_tool(t["example"], tools, denied=denied):
            continue                                      # a chip is read at a glance: no long pasted questions
        # an example naming one order or item is a demonstration, offered only when there are too few others
        (specific if re.search(r"\d{3,}", t["example"]) else general).append(t["example"])
    return list(dict.fromkeys(sorted(general, key=len) + sorted(specific, key=len)))[:limit]


def sheet(scope) -> str:
    """Plain lines for the social reply's prompt: each area the user may ask about, in staff's words."""
    agents, _ = _readable(scope)
    lines = [f"- {a.title}: {_plain(a.description)}" for a in agents if _plain(a.description)]
    return "\n".join(lines) or "- (no database areas are open to this user)"


_TECHNICAL = re.compile(r"[_.]|[a-z][A-Z]|[A-Z]{2,}[a-z]")


def names(scope=None) -> set[str]:
    """The view and table names of every catalog that read as identifiers (vw_ExportOrder, DaywisePayHour, rag.x),
    lower case: what a social reply must never contain. A table called plainly "Buyer" or "Item" is left out — the
    words themselves are fine to say."""
    try:
        from ..data.catalog import load_catalogs
        out: set[str] = set()
        for c in load_catalogs().values():
            for full in list(c.view_names) + [t.name for t in c.offered_tables]:
                for n in (full, full.split(".")[-1]):
                    if len(n) > 3 and _TECHNICAL.search(n):
                        out.add(n.lower())
        return out
    except Exception:
        return set()


def _topic_agent(topic: str, agents: list[Any]) -> Optional[Any]:
    t = (topic or "").lower()
    words = set(re.findall(r"[a-z]{2,}", t))
    best, score = None, 0
    for a in agents:
        s = 3 * (a.name.lower() in words or a.name.lower() in t)
        s += sum(1 for k in a.keywords if re.search(rf"\b{re.escape(k.lower())}\b", t))
        s += sum(1 for w in re.findall(r"[a-z]{4,}", a.title.lower()) if w in words)
        if s > score:
            best, score = a, s
    return best


def describe(topic: str, scope) -> tuple[str, list[str]]:
    """(the reply, up to three example questions as chips) for a question about what the assistant can tell."""
    agents, _ = _readable(scope)
    agent = _topic_agent(topic, agents) if topic else None
    if agent is not None:
        where = _plain(str(agent.extra.get("where") or ""))
        text = (prompt("meta_capability").strip().replace("{title}", agent.title)
                .replace("{description}", _plain(agent.description)).replace("{where}", where))
        return re.sub(r"\n{3,}", "\n\n", text).strip(), examples(scope, agent.name)
    lines = "\n".join(f"- **{a.title}** — {_plain(a.description)}" for a in agents if _plain(a.description))
    return prompt("meta_capability_all").strip().replace("{lines}", lines or "-"), examples(scope)
