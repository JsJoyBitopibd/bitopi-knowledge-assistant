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


def keyword_score(question: str, cat: Catalog) -> int:
    """Hand-written keywords (3 each) and curated view/column names (1 each) present in the question as
    whole words — 'po' must not score 'policy', nor 'tal' 'total'."""
    q = _padded(question)
    kw = sum(3 for w in cat.keywords if w and _padded(w) in q)
    cols = sum(1 for v in cat.views for w in [v.name.split(".")[-1], *v.columns] if w and _padded(w) in q)
    return kw + cols


def evidence(question: str, cats: dict[str, Catalog], qvec: Optional[list[float]] = None) -> list[Evidence]:
    """Every catalog scored, best first."""
    from .schema_index import get_index   # lazy: numpy + the index build are not needed by the fixed-tool path
    out: list[Evidence] = []
    for name, cat in cats.items():
        share, cos = 0.0, None
        if cat.offered_tables:
            idx = get_index(cat)
            share, cos = idx.best_name_share(question), idx.best_cosine(qvec)
        out.append(Evidence(name, keyword_score(question, cat), share, cos))
    with_vectors = sorted((e for e in out if e.cosine is not None), key=lambda e: -e.cosine)
    bonus: dict[str, float] = {}
    if len(with_vectors) * 2 >= len(out):
        bonus = {e.database: b for e, b in zip(with_vectors[:2], (2.0, 1.0))}
    for e in out:
        e.score = e.keywords + 2 * e.name_share + bonus.get(e.database, 0.0) + (0.5 if cats[e.database].views else 0.0)
    out.sort(key=lambda e: (-e.score, e.database))
    return out


def pick_catalogs(question: str, cats: dict[str, Catalog], qvec: Optional[list[float]] = None,
                  k: Optional[int] = None) -> dict[str, Catalog]:
    """The catalogs to show the SQL model for this question, best first; all of them when there are no
    more than k (the pre-J behaviour, which the small test catalogs rely on)."""
    k = k or int(settings().get("data.max_databases_per_question", 2))
    if len(cats) <= k:
        return dict(cats)
    return {e.database: cats[e.database] for e in evidence(question, cats, qvec)[:k]}


def explain(question: str, cats: dict[str, Catalog], qvec: Optional[list[float]] = None) -> str:
    """One line for logs and scripts/index_schema.py --try: 'HR 7.0 (kw 6, name 0.50, cos 0.61) > …'."""
    parts = []
    for e in evidence(question, cats, qvec)[:5]:
        cos = f", cos {e.cosine:.2f}" if e.cosine is not None else ""
        parts.append(f"{e.database} {e.score:.1f} (kw {e.keywords}, name {e.name_share:.2f}{cos})")
    return " > ".join(parts)
