"""Pick the database(s) a question is about before any SQL is written (Phase J1).

With two catalogs the SQL prompt could show every curated view of every database plus the tables the
schema index selected in each; with seventeen that prompt would be ten times longer, slower and less
accurate, and tables from databases the question is not about would crowd out the right ones. So one
cheap step, with no model call, ranks the catalogs and the SQL model is shown only the best
`data.max_databases_per_question` (default 2). It still names its choice (`DATABASE:`), so a close call
between two databases is settled with both catalogs in view.

Evidence per catalog, all computed locally in a few milliseconds:
- keywords: the catalog's hand-written `keywords:` found in the question as whole words (3 points each),
  and the names of its curated views and their columns (1 point each);
- table names: how much of a discovered table's own name the question mentions, best table, 0..1
  (schema_index._name_scores; "GRN" alone names Inventory's GRN tables) — up to 2 points;
- vectors: the best cosine between the question and the database's table documents, when the schema
  index has vectors for it (scripts/index_schema.py). Cosines are comparable across databases (same
  model, same question): the closest database gets 2 points, the runner-up 1 — but only once most
  catalogs have vectors, so a database still waiting for its file is not out-scored by default.
A catalog with curated views gets half a point on a tie: those are the databases staff ask about most.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from ..config import settings
from .catalog import Catalog


@dataclass
class Evidence:
    database: str
    keywords: int                  # keyword / view-column hits
    name_share: float              # 0..1, best discovered table
    cosine: Optional[float]        # None without vectors
    score: float = 0.0


_SEP = re.compile(r"[^a-z0-9&]+")


def _padded(text: str) -> str:
    """' how many grn were received ': whole-word (and whole-phrase) membership by substring test, which
    is far cheaper than one regex per keyword for ~650 keywords and column names per question."""
    return " " + _SEP.sub(" ", text.lower()).strip() + " "


# T-SQL words a pasted statement is made of. They are not vocabulary: "ORDER BY" sent a GRN statement to the
# order agent and BitopiSplint on 8 Oct 2026. Stripped before any keyword scoring (P1).
_SQL_WORDS = {"select", "from", "where", "join", "inner", "left", "right", "outer", "full", "cross", "on", "and",
              "or", "not", "as", "top", "order", "by", "group", "having", "with", "distinct", "union", "all",
              "case", "when", "then", "else", "end", "in", "is", "null", "like", "between", "exists", "asc",
              "desc", "count", "sum", "min", "max", "avg", "over", "partition", "cast", "convert", "isnull",
              "coalesce", "nullif", "dbo", "limit", "offset", "fetch", "next", "rows", "only", "set", "declare"}
_STATEMENT = re.compile(r"^\s*(select|with)\b", re.I)


def _strip_sql(question: str) -> str:
    """A question that is itself a SQL statement, with its reserved words removed: the table and column names it
    mentions still count as evidence (name_share, compound words), the SQL grammar never does."""
    if not _STATEMENT.search(question):
        return question
    return " ".join(w for w in re.split(r"[\s(),;=<>*+/-]+", question) if w and w.lower() not in _SQL_WORDS)


_COMPOUNDS: dict[tuple, dict[str, str]] = {}


def compound_map(cat: Catalog) -> dict[str, str]:
    """{'companyid': 'company id', 'itemcode': 'item code', 'grnchild': 'grn child'}: every offered table and column
    name of two or more words, joined -> spaced, so the way staff type a column ("companyid=02", "itemcode") matches
    the catalog's keywords ("company", "item") and the schema index's words. Memoised per catalog tables list."""
    from .sensitive import words
    stamp = (cat.database, id(cat.tables), len(cat.tables))
    hit = _COMPOUNDS.get(stamp)
    if hit is not None:
        return hit
    out: dict[str, str] = {}
    for t in cat.offered_tables:
        for name in [t.name.split(".")[-1], *(c["name"] for c in t.columns if not c.get("sensitive"))]:
            ws = words(name)
            if len(ws) >= 2 and sum(len(w) for w in ws) >= 5:
                out.setdefault("".join(ws), " ".join(ws))
    for v in cat.views:
        for name in [v.name.split(".")[-1], *v.columns]:
            ws = words(name)
            if len(ws) >= 2:
                out.setdefault("".join(ws), " ".join(ws))
    _COMPOUNDS.clear() if len(_COMPOUNDS) > 64 else None
    _COMPOUNDS[stamp] = out
    return out


def split_compounds(question: str, cat: Catalog) -> str:
    """The question with its compound tokens spelled as words ('companyid=02' -> 'company id=02'), appended so the
    original text is still there for the regexes that need it."""
    cmap = compound_map(cat)
    if not cmap:
        return question
    extra = [cmap[w] for w in dict.fromkeys(re.findall(r"[a-z][a-z0-9]{4,}", question.lower())) if w in cmap]
    return question + (" " + " ".join(extra) if extra else "")


def prepared(question: str, cat: Catalog) -> str:
    """What this catalog is scored and searched on: SQL grammar stripped, compound tokens split, synonyms added."""
    return cat.expand(split_compounds(_strip_sql(question), cat))


def keyword_score(question: str, cat: Catalog) -> int:
    """Hand-written keywords (3 each) and curated view/column names (1 each) present in the question as
    whole words — 'po' must not score 'policy', nor 'tal' 'total'."""
    q = _padded(prepared(question, cat))
    kw = sum(3 for w in cat.keywords if w and _padded(w) in q)
    cols = sum(1 for v in cat.views for w in [v.name.split(".")[-1], *v.columns] if w and _padded(w) in q)
    return kw + cols


FACTORY_POINTS = 4.0


def factory_points(question: str, cat: Catalog, codes: list[str]) -> float:
    """For a catalog that holds one factory's data (`factory:`): +4 when the question names that factory, −4 when
    it names only other factories, 0 otherwise — 'RHL defects' reaches RHL's own system, 'TAL defects' never does."""
    if not cat.factory:
        return 0.0
    q = _padded(question)
    named = {c.upper() for c in codes if c and _padded(c) in q}
    if cat.factory in named:
        return FACTORY_POINTS
    return -FACTORY_POINTS if named else 0.0


VALUE_POINTS = 3            # a recorded name of a database found in the question counts like one of its keywords


def value_hits(question: str, cats: dict[str, Catalog]) -> dict[str, int]:
    """{database: recorded values of that database the question names} from the value index (P2.1); {} when
    the index is off or has no copies yet. Ranking only: nothing from the index is shown here."""
    if not settings().get("data.values.enabled", True):
        return {}
    try:
        from ..auth.models import Scope
        from .values import lookup
        counts: dict[str, int] = {}
        for h in lookup(question, Scope.unrestricted()):
            if h.database in cats:
                counts[h.database] = counts.get(h.database, 0) + 1
        return counts
    except Exception:
        return {}


def evidence(question: str, cats: dict[str, Catalog], qvec: Optional[list[float]] = None) -> list[Evidence]:
    """Every catalog scored, best first."""
    from .schema_index import get_index   # lazy: numpy + the index build are not needed by the fixed-tool path
    out: list[Evidence] = []
    values = value_hits(question, cats)
    for name, cat in cats.items():
        share, cos = 0.0, None
        if cat.offered_tables:
            idx = get_index(cat)
            share, cos = idx.best_name_share(prepared(question, cat)), idx.best_cosine(qvec)
        out.append(Evidence(name, keyword_score(question, cat) + VALUE_POINTS * values.get(name, 0), share, cos))
    with_vectors = sorted((e for e in out if e.cosine is not None), key=lambda e: -e.cosine)
    bonus: dict[str, float] = {}
    if len(with_vectors) * 2 >= len(out):
        bonus = {e.database: b for e, b in zip(with_vectors[:2], (2.0, 1.0))}
    codes = [str(c) for c in settings().get("scope.factory_codes", []) or []]
    for e in out:
        e.score = e.keywords + 2 * e.name_share + bonus.get(e.database, 0.0) + (0.5 if cats[e.database].views else 0.0)
        e.score += factory_points(question, cats[e.database], codes)
    # ties: the database whose table names the question uses, then the one with more keyword hits, then the name
    # (FM and Inventory tied 6/6 on 8 Oct 2026 and the alphabet put FM first)
    out.sort(key=lambda e: (-e.score, -e.name_share, -e.keywords, e.database))
    return out


# The vector bonus alone (2 points) or the curated half-point may separate two databases whose vocabulary the
# question uses equally. Then the runner-up is shown too, up to this many catalogs (P1).
TIE_MARGIN = 2.0


def pick_catalogs(question: str, cats: dict[str, Catalog], qvec: Optional[list[float]] = None,
                  k: Optional[int] = None) -> dict[str, Catalog]:
    """The catalogs to show the SQL model for this question, best first; all of them when there are no
    more than k (the pre-J behaviour, which the small test catalogs rely on). One more than k when the next
    catalog has at least as many keyword hits as the last pick and trails it by no more than TIE_MARGIN."""
    s = settings()
    k = k or int(s.get("data.max_databases_per_question", 2))
    if len(cats) <= k:
        return dict(cats)
    ev = evidence(question, cats, qvec)
    picked = ev[:k]
    cap = int(s.get("data.max_databases_per_question_max", k + 1))
    if len(ev) > k and len(picked) < cap and ev[k].keywords > 0 and ev[k].keywords >= ev[k - 1].keywords \
            and ev[k - 1].score - ev[k].score <= TIE_MARGIN:
        picked.append(ev[k])
    return {e.database: cats[e.database] for e in picked}


def explain(question: str, cats: dict[str, Catalog], qvec: Optional[list[float]] = None) -> str:
    """One line for logs and scripts/index_schema.py --try: 'HR 7.0 (kw 6, name 0.50, cos 0.61) > …'."""
    parts = []
    for e in evidence(question, cats, qvec)[:5]:
        cos = f", cos {e.cosine:.2f}" if e.cosine is not None else ""
        parts.append(f"{e.database} {e.score:.1f} (kw {e.keywords}, name {e.name_share:.2f}{cos})")
    return " > ".join(parts)
