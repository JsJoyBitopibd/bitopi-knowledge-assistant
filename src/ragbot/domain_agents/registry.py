"""Agent registry: config/agents/<name>.yaml, one file per domain agent.

    name: finance_lc
    title: Finance/LC agent
    description: one line, what the agent covers
    stage: 1                       # trust stage reached (docs/AGENTS_DESIGN.md §4)
    catalogs: [BitopiSplint, FM]   # databases the agent reads (config/catalog/*.yaml `database:`)
    keywords: [lc, back-to-back]   # words and phrases staff use for the domain (whole-word match)
    charter: agents/finance_lc     # prompts/agents/finance_lc.txt
    status: optional note on what blocks the agent

Fixed tools name their agent with `agent:` in config/fixed_tools.yaml, so a tool hit decides the agent.
Otherwise the agent whose keywords the question mentions most wins; a tie or no match means no agent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from ..config import prompt, settings


@dataclass(frozen=True)
class Agent:
    name: str
    title: str
    description: str = ""
    stage: int = 1
    catalogs: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    charter: str = ""
    status: str = ""
    extra: dict[str, Any] = field(default_factory=dict, compare=False, hash=False)


def _folder() -> Path:
    return settings().path("agents_dir", "config/agents")


def _stamp(folder: Path) -> tuple:
    return tuple(sorted((p.name, p.stat().st_mtime_ns) for p in folder.glob("*.yaml"))) if folder.exists() else ()


def load_agents(folder: Optional[Path] = None) -> dict[str, Agent]:
    """Every agent under config/agents/, cached on the files' mtimes (an edit is picked up without a restart)."""
    folder = folder or _folder()
    return dict(_load(folder, _stamp(folder)))


@lru_cache(maxsize=4)
def _load(folder: Path, _stamp: tuple) -> dict[str, Agent]:
    out: dict[str, Agent] = {}
    for p in sorted(folder.glob("*.yaml")) if folder.exists() else []:
        d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        name = d.get("name") or p.stem
        if name in out:
            raise RuntimeError(f"{p}: agent {name!r} defined twice")
        known = {"name", "title", "description", "stage", "catalogs", "keywords", "charter", "status"}
        out[name] = Agent(name=name, title=d.get("title") or name, description=d.get("description", ""),
                          stage=int(d.get("stage", 1)), catalogs=tuple(d.get("catalogs") or ()),
                          keywords=tuple(str(k).lower() for k in d.get("keywords") or ()),
                          charter=d.get("charter", ""), status=d.get("status", ""),
                          extra={k: v for k, v in d.items() if k not in known})
    return out


def _keyword_re(word: str) -> re.Pattern:
    # whole words; a phrase's spaces and hyphens match any run of spaces or hyphens ("back-to-back" = "back to back")
    body = r"[\s-]+".join(re.escape(w) for w in re.split(r"[\s-]+", word.strip()) if w)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])")


@lru_cache(maxsize=1024)
def _compiled(word: str) -> re.Pattern:
    return _keyword_re(word)


def keyword_hits(question: str, agent: Agent) -> int:
    q = question.lower()
    return sum(1 for w in agent.keywords if _compiled(w).search(q))


def pick_agent(question: str, tool: Optional[dict[str, Any]] = None,
               agents: Optional[dict[str, Agent]] = None) -> Optional[Agent]:
    """The agent for a data question, without a model call: the fixed tool's `agent:` when a tool
    matched, else the single agent whose keywords the question mentions most (None on a tie or none)."""
    agents = load_agents() if agents is None else agents
    if tool is not None and tool.get("agent") in agents:
        return agents[tool["agent"]]
    scored = sorted(((keyword_hits(question, a), a.name) for a in agents.values()), reverse=True)
    if not scored or scored[0][0] == 0 or (len(scored) > 1 and scored[1][0] == scored[0][0]):
        return None
    return agents[scored[0][1]]


def charter_text(name: str, agents: Optional[dict[str, Agent]] = None) -> str:
    """The agent's charter (prompts/<charter>.txt) as a block to append to a system prompt, or ""."""
    agents = load_agents() if agents is None else agents
    a = agents.get(name) if name else None
    if a is None or not a.charter:
        return ""
    try:
        body = prompt(a.charter).strip()
    except FileNotFoundError:
        return ""
    return f"Domain notes ({a.title}):\n{body}" if body else ""


def validate_agents(agents: dict[str, Agent], catalogs: dict[str, Any], tools: list[dict[str, Any]],
                    prompts_dir: Optional[Path] = None) -> list[str]:
    """Problems in config/agents and the tools' `agent:` tags (scripts/check_catalog.py, tests)."""
    problems: list[str] = []
    prompts_dir = prompts_dir or settings().path("prompts_dir")
    for a in agents.values():
        for c in a.catalogs:
            if c not in catalogs:
                problems.append(f"agent {a.name}: catalog {c!r} is not loaded")
        if not a.charter:
            problems.append(f"agent {a.name}: no charter")
        elif not (prompts_dir / f"{a.charter}.txt").exists():
            problems.append(f"agent {a.name}: charter prompts/{a.charter}.txt is missing")
        if not a.keywords:
            problems.append(f"agent {a.name}: no keywords")
    for t in tools:
        tag = t.get("agent")
        if not tag:
            problems.append(f"tool {t.get('name')}: no agent")
        elif tag not in agents:
            problems.append(f"tool {t.get('name')}: unknown agent {tag!r}")
        elif t.get("database") not in agents[tag].catalogs:
            problems.append(f"tool {t.get('name')}: database {t.get('database')!r} is not one of agent {tag}'s catalogs")
    return problems
