"""Entity dictionaries (Phase P1): the names staff use for companies and stores, and what they mean in the data.

`config/reference/entities.yaml` (bind-mounted, no rebuild) holds the group's companies — code, name, short name,
aliases, and the factory code a company's rows carry in `scope.factory_codes` when it has one — read once from
`SystemManager.dbo.Company` on 2026-10-09, plus store-name aliases and metric synonyms. The 8 Oct 2026
closing-balance question said "companyid=02" and "misami" and nothing in the code knew they were the same thing.

Used by the fixed tools' `type: entity` parameters (a company named in the question becomes a bound parameter,
never text in the SQL), by the router (a company alias counts for the catalogs that hold company data) and, in
P2, by the SQL prompt's "Known values" line. No model call; no database call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

from ..config import settings


@dataclass(frozen=True)
class Company:
    code: str
    name: str
    short: str = ""
    aliases: tuple[str, ...] = ()
    factory: str = ""                 # the code in scope.factory_codes whose users may see this company's rows


@dataclass
class Entities:
    companies: list[Company] = field(default_factory=list)
    store_aliases: dict[str, list[str]] = field(default_factory=dict)
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)

    def company(self, code: str) -> Optional[Company]:
        return next((c for c in self.companies if c.code == str(code).zfill(2)), None)


def _folder() -> Path:
    return settings().path("reference_dir", "config/reference")


def _stamp(f: Path) -> tuple:
    return (str(f), f.stat().st_mtime_ns) if f.exists() else (str(f), 0)


def load_entities(folder: Optional[Path] = None) -> Entities:
    f = (folder or _folder()) / "entities.yaml"
    return _load(f, _stamp(f))


@lru_cache(maxsize=4)
def _load(f: Path, _stamp: tuple) -> Entities:
    if not f.exists():
        return Entities()
    d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    companies = [Company(str(c["code"]).zfill(2), str(c.get("name", "")), str(c.get("short", "")),
                         tuple(str(a).lower() for a in c.get("aliases") or ()), str(c.get("factory", "") or ""))
                 for c in d.get("companies") or []]
    return Entities(companies, {str(k).lower(): [str(s) for s in v] for k, v in (d.get("store_aliases") or {}).items()},
                    d.get("metrics") or {})


def _word_re(phrase: str) -> re.Pattern:
    body = r"[\s.&-]+".join(re.escape(w) for w in re.split(r"[\s.&-]+", phrase.strip()) if w)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.I)


_CODE = re.compile(r"\bcompany\s*(?:id|code|no\.?)?\s*(?:=|:|is|#)?\s*'?(\d{1,2})\b", re.I)
_CODE_JOINED = re.compile(r"\bcompanyid\s*(?:=|:)?\s*'?(\d{1,2})\b", re.I)


def company_from_question(question: str, ents: Optional[Entities] = None) -> Optional[Company]:
    """The one company a question names — by code ("companyid=02", "company 02") or by name, short name or alias
    ("misami", "MGL", "tarasima") — or None when it names none or two different ones (then nothing is assumed)."""
    ents = ents or load_entities()
    found: list[Company] = []
    for m in [*_CODE_JOINED.finditer(question), *_CODE.finditer(question)]:
        c = ents.company(m.group(1))
        if c and c not in found:
            found.append(c)
    for c in ents.companies:
        names = [c.name, c.short, *c.aliases]
        if any(n and len(n) >= 3 and _word_re(n).search(question) for n in names) and c not in found:
            found.append(c)
    return found[0] if len(found) == 1 else None
