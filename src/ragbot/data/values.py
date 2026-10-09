"""Value index (Phase P2.1): the names recorded in the data, so a question's spelling can become a real filter.

The 8 Oct 2026 closing-balance question said "mantainance store of misami"; the store is recorded as
'Maintenance Store (Misami)'. Nothing in the pipeline knew the recorded names, so the model copied the user's
spelling into the SQL and got NULL. Now the nightly aggregates refresh (scripts/refresh_aggregates.py) copies the
low-cardinality name columns named in config/aggregates.yaml — every spec with an `entity:` key (stores, companies,
item categories, store groups, issue causes) — into data/index/aggregates.db, and this module matches a question
against them: exact whole-phrase first, the store aliases of config/reference/entities.yaml, then a close match
(difflib) above `data.values.fuzzy_cutoff`. A hit is a recorded value, bound as a parameter by the fixed tools
(`type: value`), offered to the SQL model as a "Known values" line, and scored by the database router.

Scope: a value belongs to a company (a store's name carries it in brackets) and is shown only to users who may
see that company's rows; a company with no factory code (Misami) is visible to all-factory users only. No model
call, no database call at question time.
"""
from __future__ import annotations

import difflib
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..auth.models import Scope
from ..config import settings
from . import aggregates
from .entities import Entities, company_from_question, load_entities

GROUP = "GROUP"


@dataclass(frozen=True)
class ValueEntry:
    entity: str                 # store | company | item_category | store_group | issue_cause …
    value: str                  # as recorded: 'Maintenance Store (Misami)'
    code: str = ""              # its code, when the spec names one: '2'
    factory: str = GROUP        # scope.factory_codes code that may see it; GROUP = everyone; '' = all-factory users only
    database: str = ""          # the catalog it belongs to (router evidence)
    target: str = ""            # column the SQL model should filter on: 'rag.vw_StockBalance.StoreName'

    @property
    def base(self) -> str:
        """The name without its bracketed company: 'Maintenance Store (Misami)' -> 'Maintenance Store'."""
        return re.sub(r"\s*\([^)]*\)\s*$", "", self.value).strip() or self.value

    @property
    def bracket(self) -> str:
        m = re.search(r"\(([^)]*)\)\s*$", self.value)
        return m.group(1).strip() if m else ""


@dataclass
class ValueHit:
    entity: str
    value: str
    code: str
    span: str                   # the question's words that matched
    score: float                # 1.0 exact / alias, else the close-match ratio
    database: str = ""
    target: str = ""
    ambiguous: list[str] = field(default_factory=list)   # the other recorded values the span could mean


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9&]+", " ", s.lower()).strip()


def _padded(s: str) -> str:
    return " " + _norm(s) + " "


# ---------------------------------------------------------------- loading
_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {}
_RECHECK_SECONDS = 5.0


def _factory_for(bracket: str, ents: Entities) -> str:
    """The scope code of the company a store's bracket names: '(Misami)' -> '' (no factory code yet),
    '(Tarasima)' -> 'TAL', no bracket -> GROUP (shared by every company)."""
    if not bracket:
        return GROUP
    b = _padded(bracket)
    for c in ents.companies:
        for name in (c.name, c.short, *c.aliases):
            if name and len(name) >= 3 and _padded(name) in b or (name and _padded(b.strip()) in _padded(name)):
                return c.factory or ""
    return ""          # a company the dictionary does not know: all-factory users only


def _entries_from_rows(spec: dict[str, Any], cols: list[str], rows: list[list[Any]], ents: Entities) -> list[ValueEntry]:
    low = [c.lower() for c in cols]
    vi = low.index(spec["value_column"].lower()) if spec.get("value_column", "").lower() in low else None
    if vi is None:
        return []
    ci = low.index(spec["code_column"].lower()) if spec.get("code_column", "").lower() in low else None
    si = low.index(spec["scope_column"].lower()) if spec.get("scope_column", "").lower() in low else None
    out: list[ValueEntry] = []
    for r in rows:
        v = r[vi]
        if v is None or not str(v).strip():
            continue
        e = ValueEntry(spec["entity"], str(v).strip(), str(r[ci]).strip() if ci is not None and r[ci] is not None else "",
                       GROUP, spec.get("database", ""), spec.get("target", ""))
        factory = str(r[si]).strip().upper() if si is not None and r[si] is not None else GROUP
        if spec["entity"] == "store":
            factory = _factory_for(e.bracket, ents)
        elif factory and factory != GROUP and factory not in {c.factory for c in ents.companies if c.factory}:
            factory = ""        # a company short name that is not a scope code: all-factory users only
        out.append(ValueEntry(e.entity, e.value, e.code, factory, e.database, e.target))
    return out


def load_index(path: Optional[Path] = None, specs: Optional[list[dict[str, Any]]] = None) -> list[ValueEntry]:
    """Every value of every `entity:` spec whose copy exists, cached on the copies' as-of times (re-checked at most
    every few seconds, like the schema index)."""
    path = path or aggregates.db_path()
    specs = [s for s in (specs if specs is not None else aggregates.load_specs()) if s.get("entity")]
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(str(path))
        if hit and now - hit["checked"] < _RECHECK_SECONDS:
            return hit["entries"]
    key = tuple((s["name"], aggregates.available(s["name"], path)) for s in specs)
    with _LOCK:
        hit = _CACHE.get(str(path))
        if hit and hit["key"] == key:
            hit["checked"] = now
            return hit["entries"]
    ents = load_entities()
    entries: list[ValueEntry] = []
    seen: set[tuple[str, str]] = set()
    for s in specs:
        if aggregates.available(s["name"], path) is None:
            continue
        try:
            cols, rows = aggregates.run_local(f'SELECT * FROM {aggregates._q(s["name"])}', {}, max_rows=5000, path=path)
        except Exception:
            continue
        for e in _entries_from_rows(s, cols, rows, ents):
            # the same name recorded twice (a store that is also an issue destination) is one value
            if (e.entity, _norm(e.value)) not in seen:
                seen.add((e.entity, _norm(e.value)))
                entries.append(e)
    with _LOCK:
        _CACHE[str(path)] = {"key": key, "checked": now, "entries": entries}
    return entries


def clear_cache() -> None:
    with _LOCK:
        _CACHE.clear()


# ---------------------------------------------------------------- matching
def _visible(e: ValueEntry, scope: Scope) -> bool:
    if scope.all_factories or e.factory == GROUP:
        return True
    return bool(e.factory) and e.factory in scope.db_factories()


def _grams(question: str, n: int) -> list[str]:
    ws = _norm(question).split()
    return [" ".join(ws[i:i + n]) for i in range(len(ws) - n + 1)] if n >= 1 else []


def lookup(question: str, scope: Scope, entity: Optional[str] = None, entries: Optional[list[ValueEntry]] = None,
           cutoff: Optional[float] = None) -> list[ValueHit]:
    """The recorded values the question names, best first. A store asked with a company ('maintenance store of
    misami', 'companyid=02') resolves to that company's store; without one, every company's 'Maintenance Store'
    is returned as one hit per value with the others listed as `ambiguous`."""
    entries = load_index() if entries is None else entries
    cutoff = float(settings().get("data.values.fuzzy_cutoff", 0.85)) if cutoff is None else cutoff
    ents = load_entities()
    q = _padded(question)
    # alias -> canonical base ('mantainance store' -> 'maintenance store')
    aliases = {_norm(a): _norm(base) for base, al in ents.store_aliases.items() for a in al}
    found_alias = {aliases[a]: a for a in aliases if " " + a + " " in q}
    company = company_from_question(question, ents)
    # a value that is only a company's name ('MISAMI (SAMPLE)' -> base 'MISAMI') must not match the company word
    company_words = {_norm(x) for c in ents.companies for x in (c.name, c.short, *c.aliases) if x}
    hits: list[ValueHit] = []
    by_base: dict[tuple[str, str], list[tuple[ValueEntry, str, float]]] = {}
    for e in entries:
        if entity and e.entity != entity or not _visible(e, scope):
            continue
        base = _norm(e.base)
        full = _norm(e.value)
        span, score = "", 0.0
        if " " + full + " " in q:
            span, score = e.value, 1.0
        elif base in company_words:
            continue
        elif " " + base + " " in q:
            # a one-word value ('Maintenance', 'Production') is weaker evidence than a whole phrase
            span, score = e.base, 1.0 if len(base.split()) >= 2 else 0.95
        elif base in found_alias:
            span, score = found_alias[base], 1.0
        elif len(base.split()) >= 2:
            # close spellings only for phrases: a single word is too easily a different word ('nonproduction')
            n = len(base.split())
            best = 0.0
            for k in {max(1, n - 1), n, n + 1}:
                for g in _grams(question, k):
                    sm = difflib.SequenceMatcher(None, base, g)
                    if sm.quick_ratio() >= cutoff and sm.ratio() > best:
                        best, span = sm.ratio(), g
            if best >= cutoff:
                score = best
        if score:
            by_base.setdefault((e.entity, base), []).append((e, span, score))
    for (ent, base), group in by_base.items():
        chosen = group
        if company is not None and ent == "store" and any(g[0].bracket for g in group):
            # the same store name at several companies: the company named in the question decides; when it
            # names one and none of the visible stores is that company's, there is no hit at all (a TAL user
            # asking about Misami's store must not be handed Tarasima's)
            names = [_norm(x) for x in (company.name, company.short, *company.aliases) if x and len(x) >= 3]
            chosen = [g for g in group if g[0].bracket and any(
                " " + n + " " in _padded(g[0].bracket) or _padded(g[0].bracket).strip() in n for n in names)]
        if len(chosen) > 1:
            in_q = [g for g in chosen if g[0].bracket and _padded(g[0].bracket) in q]
            chosen = in_q or chosen
        for e, span, score in chosen:
            hits.append(ValueHit(e.entity, e.value, e.code, span, score, e.database, e.target,
                                 ambiguous=[g[0].value for g in chosen if g[0] is not e]))
    hits.sort(key=lambda h: (-h.score, -len(h.span.split()), h.value))
    return hits


def resolve(question: str, scope: Scope, entity: str, entries: Optional[list[ValueEntry]] = None) -> tuple[Optional[ValueHit], list[str]]:
    """One recorded value of `entity` the question names, or None with the candidates when it names none or
    several (a fixed tool then runs for all of them and lists them)."""
    hits = [h for h in lookup(question, scope, entity, entries) if h.entity == entity]
    if not hits:
        return None, []
    top = [h for h in hits if h.score == hits[0].score and len(h.span.split()) == len(hits[0].span.split())]
    if len(top) == 1 and not top[0].ambiguous:
        return top[0], []
    return None, list(dict.fromkeys(v for h in top for v in [h.value, *h.ambiguous]))


def known_values_lines(question: str, scope: Scope, catalogs: list[str], limit: int = 10,
                       entries: Optional[list[ValueEntry]] = None) -> list[str]:
    """The 'Known values' lines for the SQL prompt: the question's words that are recorded names in the
    catalogs shown, with the column to filter on — never more than `limit`, nothing the user may not see."""
    out: list[str] = []
    for h in lookup(question, scope, None, entries):
        if h.database and h.database not in catalogs:
            continue
        col = h.target.rsplit(".", 1)[-1] if h.target else h.entity
        view = h.target.rsplit(".", 1)[0] if "." in h.target else h.database
        line = f"'{h.span}' in the question = {col} '{h.value}'" + (f" (code {h.code})" if h.code else "") + f" in {view}"
        if h.ambiguous:
            line += " — or " + ", ".join(f"'{v}'" for v in h.ambiguous[:4])
        out.append(line)
        if len(out) >= limit:
            break
    company = company_from_question(question, load_entities())
    if company is not None and len(out) < limit:
        out.append(f"the company named in the question = CompanyID '{company.code}' ({company.name})")
    return out


def as_of(path: Optional[Path] = None) -> Optional[datetime]:
    """When the newest value copy was refreshed (for the admin caption and the runbook)."""
    stamps = [aggregates.available(s["name"], path) for s in aggregates.load_specs() if s.get("entity")]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None
