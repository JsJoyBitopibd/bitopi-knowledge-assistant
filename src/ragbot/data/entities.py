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


# ---------------------------------------------------------------- what a question names (Phase Q)
# One reading of a question's nouns, shared by the router (is this a database question?), the fixed tools
# (`type: factory|line|unit|style` parameters), the orchestrator (a factory outside the scope; a failed query
# on a question that names a record) and the SQL prompt. Pure regex over a closed vocabulary — no model call,
# no database call. On 2026-10-11 "What style is running on TAL line 2102?" was understood by none of them.
_EO_CODE = re.compile(r"(?<![\w-])([A-Z]{2,4})-(\d{2,4}-\d{2,5}(?:-\d+)?)(?![\w-])", re.I)
_PO_CODE = re.compile(r"\b(?:PO|Export\s*PO|Buyer\s*PO)\b\s*(?:number|no\.?|#)?\s*:?\s*"
                      r"((?=[A-Z0-9/_-]*\d)[A-Z0-9][A-Z0-9/_-]{2,30})\b", re.I)
# "line 2102", "line no. 2102", "L-2102", "2102 line", and a list: "lines 2102 and 2103"
_LINE = re.compile(r"\blines?\s*(?:no\.?|number|#)?\s*[-:]?\s*(\d{2,4}(?:\s*(?:,|and|&|\+)\s*\d{2,4})*)(?![\w-])"
                   r"|\bL-(\d{2,4})(?![\w-])"
                   r"|(?<![\w-])(\d{3,4})\s+lines?\b", re.I)
_NUMS = re.compile(r"\d{2,4}")
_UNIT = re.compile(r"\b(?:units?|blocks?)\s*(?:no\.?|#)?\s*[-:]?\s*(\d{1,2})(?![\w-])", re.I)
# Phase R: "employee 1023", "emp id 250109595", "employee code 1023", "staff id 1023". An employee code is
# digits here; a name is not an identifier a query can filter on reliably.
_EMPLOYEE = re.compile(r"""\b(?:employees?|emp|staff|worker)\s*(?:id|code|no\.?|number|#)?\s*[-:#]?\s*"""
                       r"""["']?(\d{3,12})["']?(?![\w-])""", re.I)
_STYLE = re.compile(r"\bstyles?\s*(?:no\.?|number|#|code)?\s*[:#-]?\s*"
                    r"((?=[A-Za-z0-9/._-]*\d)[A-Za-z0-9][A-Za-z0-9/._-]{1,30})(?![\w-])", re.I)
_ALL_FACTORIES = re.compile(r"\ball\s+(?:the\s+)?factor(?:y|ies)\b|\bevery\s+factor(?:y|ies)\b"
                            r"|\ball\s+(?:the\s+)?units?\b|\bgroup[\s-]wide\b", re.I)


@dataclass(frozen=True)
class QuestionEntities:
    """The records a question names. Empty tuples when it names none."""
    factories: tuple[str, ...] = ()
    all_factories: bool = False
    lines: tuple[str, ...] = ()
    units: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    employees: tuple[str, ...] = ()
    eos: tuple[str, ...] = ()
    pos: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    company: Optional[Company] = None

    @property
    def names_a_record(self) -> bool:
        """True when the question names something only a database holds — a line, a unit, a style, an export
        order, a PO or a file. A factory alone is NOT one of these: "What is the TAL holiday policy?" is a
        document question, and a factory name says nothing about where the answer lives."""
        return bool(self.lines or self.units or self.styles or self.employees or self.eos or self.pos
                    or self.files)

    def kinds(self) -> set[str]:
        return {k for k, v in (("factory", self.factories), ("line", self.lines), ("unit", self.units),
                               ("style", self.styles), ("employee", self.employees),
                               ("eo", self.eos), ("po", self.pos), ("file", self.files),
                               ("company", (self.company,) if self.company else ())) if v}


def _first_groups(pattern: re.Pattern, text: str, split_numbers: bool = False) -> tuple[str, ...]:
    """The first non-empty group of every match, in order, without repeats. `split_numbers` reads a written
    list ("lines 2102 and 2103") as each of its numbers."""
    out: list[str] = []
    for m in pattern.finditer(text):
        v = next((g for g in m.groups() if g), None)
        if not v:
            continue
        for one in (_NUMS.findall(v) if split_numbers else [v]):
            if one not in out:
                out.append(one)
    return tuple(out)


def _names_company_itself(text: str, c: Company) -> bool:
    """Whether the question really names this company — by code ("companyid=02", "company 02") or by its full
    name or an alias — and not only by its short name, which for TAL/RHL/BGL is also the factory code."""
    for m in [*_CODE_JOINED.finditer(text), *_CODE.finditer(text)]:
        if str(m.group(1)).zfill(2) == c.code:
            return True
    return any(n and len(n) >= 3 and _word_re(n).search(text) for n in (c.name, *c.aliases))


@lru_cache(maxsize=512)
def _parse(question: str, codes: tuple[str, ...]) -> QuestionEntities:
    ents = load_entities()
    # Identifiers first, and blanked out of the working text afterwards: "TAL-26-1985-1" is an export order,
    # not the TAL factory, and "FR-26-1985" is a file (the fixed tools guard the same way, fixed_tools.yaml).
    eos: list[str] = []
    files: list[str] = []
    for m in _EO_CODE.finditer(question):
        code = m.group(0)
        (files if m.group(1).upper() == "FR" else eos).append(code)
    pos = [m.group(1) for m in _PO_CODE.finditer(question)]
    rest = _EO_CODE.sub(" ", question)
    rest = _PO_CODE.sub(" ", rest)

    factories: list[str] = []
    for c in codes:
        if c and re.search(rf"(?<![\w/-]){re.escape(c)}(?![\w/-])", rest, re.I):
            factories.append(c.upper())
    company = company_from_question(rest, ents)
    if company is not None and company.factory and company.factory not in factories:
        factories.append(company.factory)       # "tarasima line 2102", "closing balance at Remi Holdings"
    if company is not None and company.factory in factories and not _names_company_itself(rest, company):
        # The word was the factory code (TAL, RHL, BGL), which is also that company's short name. A VistaQ or
        # planning question names a factory, not a company; claiming both put a useless "CompanyID '06'" line
        # in every TAL prompt. A question that really names the company ("company 02", "tarasima") keeps it.
        company = None
    return QuestionEntities(
        factories=tuple(factories), all_factories=bool(_ALL_FACTORIES.search(rest)),
        lines=_first_groups(_LINE, rest, split_numbers=True), units=_first_groups(_UNIT, rest),
        styles=_first_groups(_STYLE, rest), employees=_first_groups(_EMPLOYEE, rest),
        eos=tuple(dict.fromkeys(eos)), pos=tuple(dict.fromkeys(pos)),
        files=tuple(dict.fromkeys(files)), company=company)


# Which curated-view column holds each kind of entity. A convention over the 30 views, not per-catalog config:
# a view that breaks it simply gets no line (the model still has the view's own column descriptions).
_ENTITY_COLUMNS: dict[str, re.Pattern] = {
    "factory": re.compile(r"^factory(id)?$", re.I),
    "unit": re.compile(r"^unit(name)?$", re.I),
    "line": re.compile(r"^line(number|no|name)?$", re.I),
    "style": re.compile(r"^style(number|no|code)?$", re.I),
    "employee": re.compile(r"^(employee|emp)(code|id|no)$", re.I),
    "eo": re.compile(r"^exportorderid$", re.I),
    "po": re.compile(r"^(po|pono|ponumber|exportpono)$", re.I),
    "file": re.compile(r"^(fileno|filerefno|filerefid)$", re.I),
    "company": re.compile(r"^company(id|name)?$", re.I),
}


def has_company_column(catalogs: Any) -> bool:
    """Whether any of these catalogs' curated views carries a company column — the "Known values" company line
    is noise for VistaQ or planning questions, where TAL is a factory and not company 06."""
    return any(_ENTITY_COLUMNS["company"].match(c) for cat in catalogs for v in cat.views for c in v.columns)


def entity_lines(question: str, catalogs: Any, limit: int = 8) -> list[str]:
    """One line per entity the question names, saying which column of the views shown holds it and of what
    kind, so the SQL model compares it correctly: "line '2102' = Line (text) in rag.vw_PlanDay, …"."""
    ents = question_entities(question)
    found = [("factory", ents.factories), ("line", ents.lines), ("unit", ents.units), ("style", ents.styles),
             ("employee", ents.employees), ("eo", ents.eos), ("po", ents.pos), ("file", ents.files)]
    out: list[str] = []
    for kind, values in found:
        if not values:
            continue
        pat = _ENTITY_COLUMNS[kind]
        by_column: dict[str, list[str]] = {}
        for cat in catalogs:
            for v in cat.views:
                for col, _desc in v.columns.items():
                    if pat.match(col):
                        by_column.setdefault(col, [])
                        if v.name not in by_column[col]:
                            by_column[col].append(v.name)
        value = ", ".join(f"'{x}'" for x in values)
        if by_column:
            where = "; ".join(f"{col} in {', '.join(views[:6])}" for col, views in by_column.items())
            out.append(f"{kind} {value} = {where}")
        else:
            out.append(f"{kind} {value} = no column of the views shown; see the tables listed below")
        if len(out) >= limit:
            break
    return out


def question_entities(question: str, codes: Optional[list[str]] = None) -> QuestionEntities:
    """What this question names (factories, lines, units, styles, export orders, POs, files, a company).
    `codes` defaults to settings scope.factory_codes. Cached: every caller parses the same question."""
    if codes is None:
        codes = [str(c) for c in (settings().get("scope.factory_codes", []) or [])]
    return _parse(question or "", tuple(codes))
