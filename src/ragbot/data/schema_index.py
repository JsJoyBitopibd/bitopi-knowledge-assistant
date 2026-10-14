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
import time
from dataclasses import dataclass, field
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


def _name_words(name: str) -> list[str]:
    """The words of a table's own name, stop words removed (all of them if nothing else is left)."""
    return [w for w in words(name.split(".")[-1]) if w not in _STOP] or words(name.split(".")[-1])


def _name_scores(question: str, names: list[str],
                 name_words: Optional[list[list[str]]] = None) -> list[tuple[float, int, int, str]]:
    """(share of the table's own name words the question mentions, hits, -name length, name), best first.
    `name_words` (SchemaIndex.name_words) saves re-splitting ~3,700 names on every question."""
    qw = {w for w in words(_with_singulars(question)) if w not in _STOP}
    name_words = name_words or [_name_words(n) for n in names]
    scored = []
    for n, nw in zip(names, name_words):
        hit = sum(1 for w in nw if w in qw or (w.endswith("s") and w[:-1] in qw))
        if hit:
            scored.append((hit / len(nw), hit, -len(nw), n))
    scored.sort(reverse=True)
    return scored


def _name_matches(question: str, names: list[str], k: int, name_words: Optional[list[list[str]]] = None) -> list[str]:
    """Tables ranked by how much of their OWN name the question mentions ('buyer' + 'info' ->
    tblBuyerInfo). Column-level BM25 dilutes this: dozens of tables have a BuyerName column."""
    return [n for *_, n in _name_scores(question, names, name_words)[:k]]


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


_SHA: dict[tuple, str] = {}


def json_sha(cat: Catalog) -> str:
    """SHA-256 of the discovered JSON, memoised on (path, size, mtime): hashing all 17 files (~11 MB)
    on every question cost more than the routing itself."""
    f = _json_path(cat)
    if not f.exists():
        return ""
    st = f.stat()
    key = (str(f), st.st_size, st.st_mtime_ns)
    sha = _SHA.get(key)
    if sha is None:
        sha = _SHA[key] = hashlib.sha256(f.read_bytes()).hexdigest()
    return sha


def _column_scores(question: str, names: list[str], col_words: list[frozenset[str]]) -> list[str]:
    """Tables ranked by how many distinct question words are words of their columns (P1): "closing balance of
    item code for company id and store name" names CompanyID, LocationName, ItemCode and ClosingStock of
    dbo.InventoryStock. Three hits at least — one or two shared words ('date', 'id', 'style') say nothing and at two
    the list crowded out the right table for 'fabric booking summary for a style' (2026-10-09) — then the table
    with fewer columns first, so a wide table does not win on bulk."""
    qw = {w for w in words(_with_singulars(question)) if w not in _STOP and len(w) >= 2}
    scored = []
    for n, cw in zip(names, col_words):
        hit = len(qw & cw)
        if hit >= 3:
            scored.append((hit, -len(cw), n))
    scored.sort(reverse=True)
    return [n for *_, n in scored]


@dataclass
class SchemaIndex:
    names: list[str]
    keyword: KeywordIndex
    vectors: Optional[np.ndarray] = None     # rows aligned with names, unit length
    name_words: list[list[str]] = field(default_factory=list)   # words of each name, for _name_scores
    col_words: list[frozenset[str]] = field(default_factory=list)   # words of each table's column names (P1)

    def __post_init__(self) -> None:
        if not self.name_words:
            self.name_words = [_name_words(n) for n in self.names]
        if not self.col_words:
            self.col_words = [frozenset() for _ in self.names]

    def search(self, question: str, k: int, qvec: Optional[list[float]] = None,
               kinds: Optional[set[str]] = None) -> list[str]:
        pool = max(k * 4, 30)
        sparse = [n for n, _ in self.keyword.search(_with_singulars(question), pool)]
        lists = [sparse, _name_matches(question, self.names, pool, self.name_words)]
        cols = _column_scores(question, self.names, self.col_words)[:pool]
        if cols:
            lists.append(cols)
        ents = _entity_scores(kinds or set(), self.names, self.col_words)[:pool] if kinds else []
        if ents:
            lists.append(ents)
        if self.vectors is not None and qvec is not None and len(self.names):
            scores = self.vectors @ np.asarray(qvec, dtype=np.float32)
            top = np.argsort(-scores)[:pool]
            lists.insert(0, [self.names[i] for i in top])
        return rrf(lists, int(settings().get("retrieval.rrf_k", 60)))[:k]

    def best_cosine(self, qvec: Optional[list[float]]) -> Optional[float]:
        """The closest table document's cosine to the question — comparable across databases (same
        model, same question), which is what db_router needs. None without vectors."""
        if self.vectors is None or qvec is None or not len(self.names):
            return None
        return float(np.max(self.vectors @ np.asarray(qvec, dtype=np.float32)))

    def best_name_share(self, question: str) -> float:
        """How much of the best-matching table's own name the question mentions (0..1)."""
        scored = _name_scores(question, self.names, self.name_words)
        return scored[0][0] if scored else 0.0


def build(cat: Catalog, embed: bool = True) -> SchemaIndex:
    """Build the in-memory index; with embed=True also compute and save the vectors (slow: minutes)."""
    tables = cat.offered_tables
    names = [t.name for t in tables]
    docs = [table_document(t) for t in tables]
    col_words = [frozenset(w for c in t.columns if not c.get("sensitive") for w in words(c["name"])) for t in tables]
    idx = SchemaIndex(names, KeywordIndex(names, [_with_singulars(d) for d in docs]), col_words=col_words)
    if embed and docs:
        from ..embed import EMBED_MODEL, get_embedder
        vecs = np.asarray(get_embedder().embed(docs), dtype=np.float32)
        p = vectors_path(cat.database)
        p.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(p, names=np.array(names), vectors=vecs, sha=json_sha(cat), model=EMBED_MODEL)
        idx.vectors = vecs
    return idx


_CACHE: dict[str, tuple[tuple, SchemaIndex]] = {}
_CHECKED: dict[str, float] = {}      # database -> when its files were last stat'ed
_RECHECK_SECONDS = 5.0               # a new discovery or vector file is seen within this long
_LOCK = threading.Lock()


def get_index(cat: Catalog) -> SchemaIndex:
    """Cached per database, keyed on the discovered JSON's hash and the vector file's mtime — so vectors
    written by scripts/index_schema.py while the app runs are picked up at the next question instead of
    after a restart. The files are stat'ed at most every _RECHECK_SECONDS: with 17 databases on a Docker
    bind mount the checks alone cost ~70 ms per question. Loads saved vectors only if they were built
    from the same JSON with the same embedding model; otherwise keyword-only."""
    now = time.monotonic()
    with _LOCK:
        hit = _CACHE.get(cat.database)
        if hit and now - _CHECKED.get(cat.database, 0.0) < _RECHECK_SECONDS:
            return hit[1]
    p = vectors_path(cat.database)
    # the catalog's table_notes reach the BM25 documents through Table.description: an edited note must rebuild
    # the in-memory index too, not only the prompt (the vectors are keyed on the JSON sha and stay valid)
    # The sensitive list belongs in the key too (Phase R): it decides which tables and columns are indexed at
    # all, and editing only `sensitive_patterns:` changes neither the discovery JSON's hash nor the notes.
    key = (json_sha(cat), p.stat().st_mtime_ns if p.exists() else 0, hash(tuple(sorted(cat.table_notes.items()))),
           cat.sensitive_patterns)
    with _LOCK:
        _CHECKED[cat.database] = now
        hit = _CACHE.get(cat.database)
        if hit and hit[0] == key:
            return hit[1]
    idx = build(cat, embed=False)
    if p.exists():
        from ..embed import EMBED_MODEL
        z = np.load(p, allow_pickle=False)
        if str(z["sha"]) == key[0] and str(z["model"]) == EMBED_MODEL and list(z["names"]) == idx.names:
            idx.vectors = z["vectors"]
    with _LOCK:
        _CACHE[cat.database] = (key, idx)
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


# Which column words carry each kind of record a question can name (Phase Q). A table that covers two or more
# of the kinds in the question is the table that question is about: "What style is running on TAL line 2102?"
# names a factory, a line and a style, and dbo.ProductionPlan has all three — it was not selected at all on
# 11 Oct (logs/schema_select.csv), while twelve tables with one of the words were.
_ENTITY_WORDS: dict[str, frozenset[str]] = {
    "factory": frozenset({"factory", "factories"}),
    "line": frozenset({"line"}),
    "unit": frozenset({"unit", "building", "block"}),
    "style": frozenset({"style"}),
    "buyer": frozenset({"buyer", "customer", "brand"}),
    "po": frozenset({"po", "purchase"}),
    "eo": frozenset({"export"}),
    "file": frozenset({"file", "fileref"}),
    "item": frozenset({"item"}),
    "company": frozenset({"company"}),
}


def _entity_scores(kinds: set[str], names: list[str], col_words: list[frozenset[str]]) -> list[str]:
    """Tables ranked by how many of the question's record kinds their column names cover."""
    wanted = {k: _ENTITY_WORDS[k] for k in kinds if k in _ENTITY_WORDS}
    if len(wanted) < 2:
        return []
    scored = []
    for n, cw in zip(names, col_words):
        covered = sum(1 for words_ in wanted.values() if cw & words_)
        if covered >= 2:
            scored.append((covered, "date" in cw, -len(cw), n))
    scored.sort(reverse=True)
    return [n for *_, n in scored]


def select_tables(question: str, cat: Catalog, k: int = 8, qvec: Optional[list[float]] = None,
                  max_extra: int = 6, max_total: Optional[int] = None) -> list[str]:
    """The discovered tables to show the SQL model for this question: top-k by hybrid search (the catalog's own
    `schema_rag_k` when it sets one), plus up to `max_extra` FK neighbours, at most `max_total` in all. The
    question is searched with its compound tokens split and the catalog's synonyms added (db_router.prepared).
    [] when the catalog has no discovered tier."""
    if not cat.offered_tables:
        return []
    from .db_router import prepared
    k = cat.schema_rag_k or k
    try:
        from .entities import question_entities
        kinds = question_entities(question).kinds()
    except Exception:
        kinds = set()
    hits = get_index(cat).search(prepared(question, cat), k, qvec, kinds=kinds)
    out = hits + fk_neighbours(hits, cat)[:max_extra]
    return out[:max_total] if max_total else out


# A primary key named only "Id" (or Code, No, Key) says nothing about which table it belongs to. Every table
# in VistaQ has one, and every table has an `Id` column, so the old same-name rule proposed `dbo.Style.Id =
# dbo.Buyer.Id` for all 66 pairs of a 12-table selection — joins that are simply false, offered to the model
# under a heading that licenses them ("Join only along a listed key … or a Likely joins line").
_GENERIC_KEY = frozenset({"id", "code", "no", "key", "sl", "slno", "serial", "rowid", "uid", "guid"})


def _fk_pairs(cat: Catalog) -> set[tuple[str, str]]:
    """Table pairs that already have a declared foreign key — Table.render() prints those as "Joins:"."""
    out: set[tuple[str, str]] = set()
    for t in cat.tables:
        for fk in t.foreign_keys:
            a, b = t.name.lower(), str(fk.get("ref_table", "")).lower()
            out.add((a, b))
            out.add((b, a))
    return out


def _singulars(name: str) -> list[str]:
    """dbo.Styles -> ['Styles', 'Style']; dbo.VqBuyers -> ['VqBuyers', 'VqBuyer', 'Buyers', 'Buyer']."""
    bare = name.split(".")[-1]
    out = [bare]
    for n in list(out):
        if n.lower().endswith("ies"):
            out.append(n[:-3] + "y")
        elif n.lower().endswith("s") and not n.lower().endswith("ss"):
            out.append(n[:-1])
    for n in list(out):
        for prefix in ("tbl", "Vq"):
            if n.lower().startswith(prefix.lower()) and len(n) > len(prefix) + 2:
                out.append(n[len(prefix):])
    return list(dict.fromkeys(out))


def join_hints(names: list[str], cat: Catalog) -> list[str]:
    """Likely join conditions between selected tables that have no declared foreign key: the catalog's
    code-column hints (a Buyer code -> Contact_Master.ContactID, with where the name is), then either a column
    named exactly like another table's distinctive primary key (dbo.ExportOrder.FileRefID = dbo.FileRef.FileRefID)
    or, when that key is a bare `Id`, a column that names the table it points at (dbo.ProductionPlan.StyleId =
    dbo.Style.Id). Never `a.Id = b.Id`."""
    tabs = [t for t in (cat.table(n) for n in names) if t]
    selected = {t.name.lower() for t in tabs}
    hints = [f"{t.name}.{h.column} = {h.target}" + (f"  ({h.note})" if h.note else "")
             for t in tabs for h in cat.hints_for(t.name) if h.target_table.lower() in selected]
    declared = _fk_pairs(cat)
    for b in tabs:
        if len(b.primary_key) != 1:
            continue
        key = b.primary_key[0]
        if not re.search(r"(id|code|no)$", key, re.IGNORECASE):
            continue
        generic = key.lower() in _GENERIC_KEY
        targets = [re.compile(rf"^(?:{re.escape(s)})\w*?{re.escape(key)}$", re.I) for s in _singulars(b.name)]
        for a in tabs:
            if a is b or (a.name.lower(), b.name.lower()) in declared:
                continue        # the declared foreign key is already rendered on the table itself
            for c in a.columns:
                name = c["name"]
                if generic:
                    if any(p.match(name) for p in targets):
                        hints.append(f"{a.name}.{name} = {b.name}.{key}")
                elif name.lower() == key.lower():
                    hints.append(f"{a.name}.{key} = {b.name}.{key}")
    return list(dict.fromkeys(hints))
