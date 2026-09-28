"""Schema index (Phase C3, docs/ROADMAP.md): pick the few discovered tables a question needs.

A database has hundreds of tables (BitopiSplint: ~1,300); the SQL prompt can show perhaps ten. Each
offered table (catalog.Catalog.offered_tables) becomes one short document — name, description,
column names split into words, sample values — searched two ways and merged with RRF, exactly
like document retrieval:
- dense: bge-m3 vectors, precomputed by scripts/index_schema.py into data/index/schema/<db>.npz.
  Stored as one NumPy matrix rather than a Chroma collection: ~1,300 x 1,024 floats is ~5 MB and a
  dot product over it takes a millisecond. The file records the SHA-256 of the discovered JSON and
  the embedding model; when either changes the vectors are ignored (not rebuilt mid-question).
- sparse: BM25 over the same documents, built in memory (fast), so exact column names still match
  even with no vector file.
Then 1-hop foreign-key neighbours of the hits are added, so a join partner is in the prompt too.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ..config import settings
from ..retrieve.hybrid import rrf
from ..retrieve.keyword import KeywordIndex
from .catalog import Catalog, Table
from .sensitive import words


def _with_singulars(text: str) -> str:
    """Append singular forms of plural-looking words ('suppliers' -> 'supplier', 'factories' ->
    'factory') so BM25's exact-token match links a question's plural to a table's singular name."""
    extra = []
    for w in re.findall(r"[a-z]{4,}", text.lower()):
        if w.endswith("ies"):
            extra.append(w[:-3] + "y")
        elif w.endswith("s") and not w.endswith("ss"):
            extra.append(w[:-1])
    return text + ("\n" + " ".join(extra) if extra else "")


_STOP = {"the", "all", "for", "and", "with", "list", "show", "how", "many", "much", "which", "what", "who",
          "does", "did", "have", "has", "each", "our", "we", "do", "of", "in", "a", "an", "to", "is", "are",
          "tbl", "vw", "view", "dbo", "info", "master", "details", "data"}


def _name_matches(question: str, names: list[str], k: int) -> list[str]:
    """Tables ranked by how much of their OWN name the question mentions ('buyer' + 'info' ->
    tblBuyerInfo). Column-level BM25 dilutes this: dozens of tables have a BuyerName column."""
    qw = {w for w in words(_with_singulars(question)) if w not in _STOP}
    scored = []
    for n in names:
        nw = [w for w in words(n.split(".")[-1]) if w not in _STOP] or words(n.split(".")[-1])
        hit = sum(1 for w in nw if w in qw or (w.endswith("s") and w[:-1] in qw))
        if hit:
            scored.append((hit / len(nw), hit, -len(nw), n))
    scored.sort(reverse=True)
    return [n for *_, n in scored[:k]]


def table_document(t: Table) -> str:
    """The text a table is found by. Sensitive columns are left out, like in the prompt."""
    cols = [c for c in t.columns if not c.get("sensitive")]
    parts = [t.name, " ".join(words(t.name.split(".")[-1]))]
    if t.description:
        parts.append(t.description)
    for c in cols:
        bits = [c["name"], " ".join(words(c["name"]))]
        if c.get("description"):
            bits.append(c["description"])
        if c.get("samples"):
            bits.append(" ".join(c["samples"][:10]))
        parts.append(" ".join(bits))
    return "\n".join(parts)


def _json_path(cat: Catalog) -> Path:
    return settings().path("catalog_dir") / "discovered" / f"{cat.database}.json"


def vectors_path(database: str) -> Path:
    return settings().path("index_dir") / "schema" / f"{database}.npz"


def json_sha(cat: Catalog) -> str:
    f = _json_path(cat)
    return hashlib.sha256(f.read_bytes()).hexdigest() if f.exists() else ""


@dataclass
class SchemaIndex:
    names: list[str]
    keyword: KeywordIndex
    vectors: Optional[np.ndarray] = None     # rows aligned with names, unit length

    def search(self, question: str, k: int, qvec: Optional[list[float]] = None) -> list[str]:
        pool = max(k * 4, 30)
        sparse = [n for n, _ in self.keyword.search(_with_singulars(question), pool)]
        lists = [sparse, _name_matches(question, self.names, pool)]
        if self.vectors is not None and qvec is not None and len(self.names):
            scores = self.vectors @ np.asarray(qvec, dtype=np.float32)
            top = np.argsort(-scores)[:pool]
            lists.insert(0, [self.names[i] for i in top])
        return rrf(lists, int(settings().get("retrieval.rrf_k", 60)))[:k]


def build(cat: Catalog, embed: bool = True) -> SchemaIndex:
    """Build the in-memory index; with embed=True also compute and save the vectors (slow: minutes)."""
    tables = cat.offered_tables
    names = [t.name for t in tables]
    docs = [table_document(t) for t in tables]
    idx = SchemaIndex(names, KeywordIndex(names, [_with_singulars(d) for d in docs]))
    if embed and docs:
        from ..embed import EMBED_MODEL, get_embedder
        vecs = np.asarray(get_embedder().embed(docs), dtype=np.float32)
        p = vectors_path(cat.database)
        p.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(p, names=np.array(names), vectors=vecs, sha=json_sha(cat), model=EMBED_MODEL)
        idx.vectors = vecs
    return idx


_CACHE: dict[str, tuple[str, SchemaIndex]] = {}
_LOCK = threading.Lock()


def get_index(cat: Catalog) -> SchemaIndex:
    """Cached per database, keyed on the discovered JSON's hash. Loads saved vectors only if they
    were built from the same JSON with the same embedding model; otherwise keyword-only."""
    sha = json_sha(cat)
    with _LOCK:
        hit = _CACHE.get(cat.database)
        if hit and hit[0] == sha:
            return hit[1]
    idx = build(cat, embed=False)
    p = vectors_path(cat.database)
    if p.exists():
        from ..embed import EMBED_MODEL
        z = np.load(p, allow_pickle=False)
        if str(z["sha"]) == sha and str(z["model"]) == EMBED_MODEL and list(z["names"]) == idx.names:
            idx.vectors = z["vectors"]
    with _LOCK:
        _CACHE[cat.database] = (sha, idx)
    return idx


def fk_neighbours(names: list[str], cat: Catalog) -> list[str]:
    """Tables one foreign key away from any of `names` (either direction), plus the tables their code
    columns point at (catalog `hints:`, e.g. Contact_Master for a Buyer code) — offered ones only."""
    offered = {t.name.lower(): t.name for t in cat.offered_tables}
    want = {n.lower() for n in names}
    out: list[str] = []
    # hint targets first: select_tables keeps only the first few neighbours, and the table that names a
    # selected table's codes is the one a "which buyer ..." question cannot do without
    for n in names:
        for h in cat.hints_for(n):
            if h.target_table.lower() in offered:
                out.append(offered[h.target_table.lower()])
    for t in cat.offered_tables:
        for fk in t.foreign_keys:
            a, b = t.name.lower(), fk["ref_table"].lower()
            if a in want and b in offered:
                out.append(offered[b])
            elif b in want and a in offered:
                out.append(offered[a])
    return [n for n in dict.fromkeys(out) if n.lower() not in want]


def select_tables(question: str, cat: Catalog, k: int = 8, qvec: Optional[list[float]] = None,
                  max_extra: int = 6) -> list[str]:
    """The discovered tables to show the SQL model for this question: top-k by hybrid search, plus up
    to `max_extra` FK neighbours. [] when the catalog has no discovered tier."""
    if not cat.offered_tables:
        return []
    hits = get_index(cat).search(question, k, qvec)
    return hits + fk_neighbours(hits, cat)[:max_extra]


def join_hints(names: list[str], cat: Catalog) -> list[str]:
    """Likely join conditions between selected tables that have no declared foreign key: the catalog's
    code-column hints (a Buyer code -> Contact_Master.ContactID, with where the name is), then a column of
    one table with the same name as another table's single-column primary key (e.g. BuyerID)."""
    tabs = [t for t in (cat.table(n) for n in names) if t]
    selected = {t.name.lower() for t in tabs}
    hints = [f"{t.name}.{h.column} = {h.target}" + (f"  ({h.note})" if h.note else "")
             for t in tabs for h in cat.hints_for(t.name) if h.target_table.lower() in selected]
    for b in tabs:
        if len(b.primary_key) != 1:
            continue
        key = b.primary_key[0]
        if not re.search(r"(id|code|no)$", key, re.IGNORECASE):
            continue
        for a in tabs:
            if a is b:
                continue
            if any(c["name"].lower() == key.lower() for c in a.columns):
                hints.append(f"{a.name}.{key} = {b.name}.{key}")
    return list(dict.fromkeys(hints))
