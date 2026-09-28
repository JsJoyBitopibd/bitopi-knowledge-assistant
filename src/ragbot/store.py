"""Vector store (Chroma behind an interface) and the document registry (SQLite).

The registry is the system of record: documents, pages, chunks, ingest state. The vector store
can always be rebuilt from the registry + source PDFs.
"""
from __future__ import annotations

import hashlib
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import settings
from .embed import EMBED_MODEL
from .models import Chunk


# ---------------------------------------------------------------- vector store
class VectorStore(ABC):
    @abstractmethod
    def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None: ...
    @abstractmethod
    def delete_by_source(self, source: str) -> None: ...
    @abstractmethod
    def query(self, vector: list[float], k: int, where: Optional[dict] = None) -> list[Chunk]: ...
    @abstractmethod
    def get(self, ids: list[str]) -> list[Chunk]: ...
    @abstractmethod
    def all_ids_and_texts(self) -> Iterable[tuple[str, str]]: ...
    @abstractmethod
    def count(self) -> int: ...
    @abstractmethod
    def set_superseded(self, source: str, superseded: bool) -> None: ...
    @abstractmethod
    def set_attributes(self, source: str, attrs: dict[str, Any]) -> None: ...


# Access attributes of every chunk and document (PRD section 7; ingest/meta.py, auth/filters.py)
ACCESS_FIELDS = ("factory", "department", "confidentiality", "buyer_code")
# what a pass may change without re-embedding when a file moves or its meta.yaml changes
TAG_FIELDS = ("category", *ACCESS_FIELDS)


def _chunk_from_record(cid: str, doc: str, meta: dict[str, Any], score: float = 0.0) -> Chunk:
    fields = {k: meta.get(k, Chunk.model_fields[k].default)
              for k in ("source", "title", "page", "section", "kind", "category", "doc_hash", "superseded",
                        "embed_model", "ingested_at")}
    # A record without access tags (indexed before F1) must not inherit the model's permissive defaults
    # (factory ALL, internal): "" matches no scope filter, so retriever._passes rejects it for scoped users.
    fields.update({k: meta.get(k) or "" for k in ACCESS_FIELDS})
    return Chunk(id=cid, text=doc, score=score, **fields)


class ChromaStore(VectorStore):
    def __init__(self, path: Path | None = None, collection: str = "chunks"):
        import chromadb
        path = path or settings().path("index_dir") / "chroma"
        self.client = chromadb.PersistentClient(path=str(path))
        # embedding_function=None: vectors always come from embed.py (bge-m3); without it chroma 1.x binds
        # its default ONNX MiniLM function to the collection and tries to download it.
        self.col = self.client.get_or_create_collection(
            name=collection, configuration={"hnsw": {"space": "cosine"}}, metadata={"embed_model": EMBED_MODEL},
            embedding_function=None)
        stored = (self.col.metadata or {}).get("embed_model")
        if stored and stored != EMBED_MODEL:
            raise RuntimeError(f"Index was built with embed_model={stored!r} but config says {EMBED_MODEL!r}. "
                               "Run scripts/reindex.py instead of changing the model.")

    def upsert(self, chunks, vectors):
        if not chunks:
            return
        self.col.upsert(ids=[c.id for c in chunks], documents=[c.text for c in chunks],
                        embeddings=vectors, metadatas=[c.metadata() for c in chunks])

    def delete_by_source(self, source):
        self.col.delete(where={"source": source})

    def query(self, vector, k, where=None):
        res = self.col.query(query_embeddings=[vector], n_results=k, where=where or None,
                             include=["documents", "metadatas", "distances"])
        out = []
        for cid, doc, meta, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]):
            out.append(_chunk_from_record(cid, doc, meta, score=1.0 - float(dist)))
        return out

    def get(self, ids):
        if not ids:
            return []
        res = self.col.get(ids=ids, include=["documents", "metadatas"])
        by = {cid: _chunk_from_record(cid, d, m) for cid, d, m in zip(res["ids"], res["documents"], res["metadatas"])}
        return [by[i] for i in ids if i in by]

    def all_ids_and_texts(self):
        n = self.count()
        for off in range(0, n, 5000):
            res = self.col.get(limit=5000, offset=off, include=["documents"])
            yield from zip(res["ids"], res["documents"])

    def all_ids_and_metadata(self):
        """(id, metadata) for every chunk — for scripts/inspect.py --check."""
        n = self.count()
        for off in range(0, n, 5000):
            res = self.col.get(limit=5000, offset=off, include=["metadatas"])
            yield from zip(res["ids"], res["metadatas"])

    def count(self):
        return self.col.count()

    def all_texts_and_vectors(self, batch: int = 2000):
        """(id, text, vector) for every chunk — seeds the embedding cache without re-embedding (E3)."""
        n = self.count()
        for off in range(0, n, batch):
            res = self.col.get(limit=batch, offset=off, include=["documents", "embeddings"])
            yield from zip(res["ids"], res["documents"], res["embeddings"])

    def set_superseded(self, source, superseded):
        self.set_attributes(source, {"superseded": superseded})

    def set_attributes(self, source: str, attrs: dict[str, Any]) -> None:
        """Merge `attrs` into the metadata of every chunk of `source` (no re-embedding)."""
        ids = self.col.get(where={"source": source}, include=[])["ids"]
        for i in range(0, len(ids), 5000):
            part = ids[i:i + 5000]
            self.col.update(ids=part, metadatas=[dict(attrs)] * len(part))


# ---------------------------------------------------------------- registry
SCHEMA = """
CREATE TABLE IF NOT EXISTS document(
  source TEXT PRIMARY KEY, title TEXT, category TEXT, doc_hash TEXT, pages INTEGER,
  chunks INTEGER, tables_ INTEGER, ocr_pages INTEGER, superseded INTEGER DEFAULT 0,
  status TEXT, error TEXT, ingested_at TEXT);
CREATE TABLE IF NOT EXISTS chunk(
  id TEXT PRIMARY KEY, source TEXT, page INTEGER, section TEXT, kind TEXT, chars INTEGER);
CREATE INDEX IF NOT EXISTS ix_chunk_source ON chunk(source);
CREATE TABLE IF NOT EXISTS ingest_run(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT,
  added INTEGER, updated INTEGER, removed INTEGER, failed INTEGER);
CREATE TABLE IF NOT EXISTS embedding_cache(
  hash TEXT, model TEXT, vec BLOB, PRIMARY KEY(hash, model));
"""

# Phase D1: the keyword index, maintained per document (no full rebuild). `body` holds the tokens of
# retrieve.keyword.tokenize() joined by spaces, so the FTS 'ascii' tokenizer (whitespace/ASCII
# punctuation split, non-ASCII kept inside tokens) sees exactly those tokens — 'PCD-02' -> 'pcd 02',
# Bangla words intact (unicode61 would split them at vowel signs). The access columns (F1) let the
# keyword search apply the user's scope inside the query.
FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  id UNINDEXED, source UNINDEXED, category UNINDEXED, superseded UNINDEXED,
  factory UNINDEXED, department UNINDEXED, confidentiality UNINDEXED, buyer_code UNINDEXED,
  body, tokenize='ascii');
"""


def fts_body(text: str) -> str:
    from .retrieve.keyword import tokenize
    return " ".join(tokenize(text))


def text_hash(text: str) -> str:
    """Key of the embedding cache: the exact chunk text (prefix included) that gets embedded."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Registry:
    def __init__(self, path: Path | None = None):
        path = path or settings().path("index_dir") / "registry.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        # A keyword index from before F1 lacks the access columns, and FTS5 tables cannot add columns:
        # recreate it empty; seed_fts() refills it from the vector store (ingest, or the app's first search).
        fts_cols = {r[1] for r in self.db.execute("PRAGMA table_info(chunk_fts)")}
        if fts_cols and "factory" not in fts_cols:
            self.db.execute("DROP TABLE chunk_fts")
            self.db.commit()
        self.db.executescript(FTS_DDL)
        # Documents from before F1 get NULL access attributes (not the defaults), so the first ingest pass
        # sees them as different from their folder's and retags them; until then scoped searches skip them.
        doc_cols = {r[1] for r in self.db.execute("PRAGMA table_info(document)")}
        for col in ACCESS_FIELDS:
            if col not in doc_cols:
                self.db.execute(f"ALTER TABLE document ADD COLUMN {col} TEXT")
        self.db.commit()
        # registries created before E3 lack chunk.text_hash (which ties cache rows to live chunks)
        if "text_hash" not in {r[1] for r in self.db.execute("PRAGMA table_info(chunk)")}:
            self.db.execute("ALTER TABLE chunk ADD COLUMN text_hash TEXT")
            self.db.commit()
        # registries created before H0 lack document.missed_scans (the two-scan removal rule)
        if "missed_scans" not in {r[1] for r in self.db.execute("PRAGMA table_info(document)")}:
            self.db.execute("ALTER TABLE document ADD COLUMN missed_scans INTEGER DEFAULT 0")
            self.db.commit()
        # H3: doc_hash is the version that is indexed (None if none is); a failed attempt is recorded in
        # failed_hash + attempts instead, so the indexed version stays known and failures are retried.
        doc_cols = {r[1] for r in self.db.execute("PRAGMA table_info(document)")}
        if "failed_hash" not in doc_cols:
            self.db.execute("ALTER TABLE document ADD COLUMN failed_hash TEXT")
            self.db.execute("ALTER TABLE document ADD COLUMN attempts INTEGER DEFAULT 0")
            # Before H3 a failed file kept the hash of the version that failed in doc_hash and was never
            # retried. Its indexed version is unknown (a failed update had already deleted the old vectors),
            # so its hash moves to failed_hash and the next pass indexes the file afresh.
            self.db.execute("UPDATE document SET failed_hash = doc_hash, doc_hash = NULL, attempts = 1 "
                            "WHERE status = 'failed'")
            self.db.commit()
        if "status" not in {r[1] for r in self.db.execute("PRAGMA table_info(ingest_run)")}:
            self.db.execute("ALTER TABLE ingest_run ADD COLUMN status TEXT")
            self.db.commit()
        # H3: where the indexed file is (relative to pdf_root) and its size + mtime when it was hashed, so a
        # pass reads only the files that look changed, and the app serves exactly the indexed file.
        # dirty = 1 while a document's chunks are being replaced in the stores: a pass killed then leaves it
        # set, and the next pass writes that document again (even if the file did not change).
        if "rel_path" not in doc_cols:
            self.db.execute("ALTER TABLE document ADD COLUMN rel_path TEXT")
            self.db.execute("ALTER TABLE document ADD COLUMN file_size INTEGER")
            self.db.execute("ALTER TABLE document ADD COLUMN file_mtime INTEGER")
            self.db.execute("ALTER TABLE document ADD COLUMN dirty INTEGER DEFAULT 0")
            self.db.commit()

    def known_hash(self, source: str) -> Optional[str]:
        """The hash of the version of `source` that is indexed, or None when none is."""
        r = self.db.execute("SELECT doc_hash FROM document WHERE source=?", (source,)).fetchone()
        return r[0] if r else None

    def document_row(self, source: str) -> Optional[dict[str, Any]]:
        """Everything an ingest pass needs to decide about one file, in one read: the indexed hash, the
        status, where and how big the indexed file was, its tags."""
        cols = ("doc_hash", "status", "rel_path", "file_size", "file_mtime", *TAG_FIELDS)
        r = self.db.execute(f"SELECT {', '.join(cols)} FROM document WHERE source=?", (source,)).fetchone()
        return dict(zip(cols, r)) if r else None

    def set_stamp(self, source: str, stamp: tuple[str, int, int]) -> None:
        """Record (relative path, size, mtime_ns) of the indexed file, as it was when hashed."""
        self.db.execute("UPDATE document SET rel_path=?, file_size=?, file_mtime=? WHERE source=?", (*stamp, source))
        self.db.commit()

    def mark_dirty(self, source: str) -> None:
        """Before a document's chunks are replaced; the document row written after them clears it."""
        self.db.execute("UPDATE document SET dirty=1 WHERE source=?", (source,))
        self.db.commit()

    def dirty_sources(self) -> set[str]:
        """Documents whose last write to the stores did not finish (a crash or a store error)."""
        return {r[0] for r in self.db.execute("SELECT source FROM document WHERE dirty=1")}

    def indexed_path(self, source: str) -> Optional[str]:
        """Where the indexed version of `source` was read from, relative to pdf_root (None if unknown)."""
        r = self.db.execute("SELECT rel_path FROM document WHERE source=? AND doc_hash IS NOT NULL", (source,)).fetchone()
        return r[0] if r else None

    def failed_attempts(self, source: str, h: str) -> int:
        """How many times the content with hash `h` has failed to index (0 if it has not)."""
        r = self.db.execute("SELECT attempts FROM document WHERE source=? AND failed_hash=?", (source, h)).fetchone()
        return int(r[0] or 0) if r else 0

    def record_failure(self, source: str, title: str, category: str, error: str, h: Optional[str] = None) -> int:
        """Mark the latest attempt at `source` as failed without touching the indexed version (doc_hash).
        With `h` (the content that failed) the attempt counts toward the retry limit; returns the count."""
        attempts = self.failed_attempts(source, h) + 1 if h else 0
        self.db.execute(
            "INSERT INTO document(source, title, category, status, error, failed_hash, attempts, ingested_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source) DO UPDATE SET status=excluded.status, error=excluded.error, "
            "failed_hash=excluded.failed_hash, attempts=excluded.attempts, ingested_at=excluded.ingested_at",
            (source, title, category, "failed", error[:500], h, attempts, datetime.now().isoformat(timespec="minutes")))
        self.db.commit()
        return attempts

    def clear_failure(self, source: str) -> bool:
        """The indexed version is current again (a transient read error passed, a meta.yaml was fixed, a
        failed update was reverted): drop the failure mark. Returns whether there was one."""
        n = self.db.execute("UPDATE document SET status='ok', error=NULL, failed_hash=NULL, attempts=0 "
                            "WHERE source=? AND status='failed' AND doc_hash IS NOT NULL", (source,)).rowcount
        self.db.commit()
        return n > 0

    def start_run(self, started: str) -> tuple[int, list[str]]:
        """Open an ingest_run row; returns its id and the start times of earlier runs that never finished
        (a crash), which are marked interrupted."""
        stale = [r[0] for r in self.db.execute("SELECT started_at FROM ingest_run WHERE status='running'")]
        self.db.execute("UPDATE ingest_run SET status='interrupted' WHERE status='running'")
        cur = self.db.execute("INSERT INTO ingest_run(started_at, status) VALUES(?, 'running')", (started,))
        self.db.commit()
        return int(cur.lastrowid), stale

    def finish_run(self, run_id: int, finished: str, added: int, updated: int, removed: int, failed: int) -> None:
        self.db.execute("UPDATE ingest_run SET finished_at=?, added=?, updated=?, removed=?, failed=?, status='done' "
                        "WHERE id=?", (finished, added, updated, removed, failed, run_id))
        # a worker pass every few minutes adds ~100k rows a year: keep 90 days of passes that changed nothing
        self.db.execute("DELETE FROM ingest_run WHERE status='done' AND added=0 AND updated=0 AND removed=0 "
                        "AND failed=0 AND finished_at < date(?, '-90 days')", (finished,))
        self.db.commit()

    def all_sources(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT source FROM document")}

    def record_scan(self, seen: set[str], count_missing: bool = True) -> dict[str, int]:
        """Two-scan removal rule (PRD FR-2.14): reset the miss counter of every known source this scan
        saw and, unless count_missing is False, add one to every known source it missed. Returns
        {missing source: consecutive scans it has been missing} (empty when count_missing is False)."""
        back = {r[0] for r in self.db.execute("SELECT source FROM document WHERE missed_scans > 0")} & seen
        self.db.executemany("UPDATE document SET missed_scans=0 WHERE source=?", [(s,) for s in back])
        missing = self.all_sources() - seen
        if count_missing:
            self.db.executemany("UPDATE document SET missed_scans=COALESCE(missed_scans,0)+1 WHERE source=?",
                                [(s,) for s in missing])
        self.db.commit()
        if not count_missing or not missing:
            return {}
        return {src: n for src, n in self.db.execute("SELECT source, missed_scans FROM document WHERE missed_scans > 0")
                if src in missing}

    def upsert_document(self, **f: Any) -> None:
        cols = ",".join(f); ph = ",".join("?" * len(f))
        upd = ",".join(f"{k}=excluded.{k}" for k in f if k != "source")
        self.db.execute(f"INSERT INTO document({cols}) VALUES({ph}) ON CONFLICT(source) DO UPDATE SET {upd}", tuple(f.values()))
        self.db.commit()

    def replace_chunks(self, source: str, chunks: list[Chunk]) -> None:
        self.db.execute("DELETE FROM chunk WHERE source=?", (source,))
        self.db.execute("DELETE FROM chunk_fts WHERE source=?", (source,))
        self.db.executemany("INSERT INTO chunk(id, source, page, section, kind, chars, text_hash) VALUES(?,?,?,?,?,?,?)",
                            [(c.id, c.source, c.page, c.section, c.kind, len(c.text), text_hash(c.text))
                             for c in chunks])
        self.db.executemany(
            "INSERT INTO chunk_fts(id, source, category, superseded, factory, department, confidentiality, buyer_code, body) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(c.id, c.source, c.category, int(bool(c.superseded)), c.factory, c.department, c.confidentiality,
              c.buyer_code, fts_body(c.text)) for c in chunks])
        self.db.commit()

    def seed_fts(self, store) -> int:
        """One-off upgrade for a registry from before D1 (or F1, which recreates the table): fill chunk_fts
        from the vector store's texts, the other columns from the document table. No-op when it has rows."""
        if self.db.execute("SELECT 1 FROM chunk_fts LIMIT 1").fetchone() or \
                not self.db.execute("SELECT 1 FROM chunk LIMIT 1").fetchone():
            return 0
        meta = {r[0]: r[1:] for r in self.db.execute(
            "SELECT c.id, c.source, d.category, d.superseded, d.factory, d.department, d.confidentiality, d.buyer_code "
            "FROM chunk c JOIN document d ON d.source = c.source")}
        rows = [(cid, m[0], m[1], int(bool(m[2])), *m[3:], fts_body(text))
                for cid, text in store.all_ids_and_texts() if (m := meta.get(cid))]
        self.db.executemany(
            "INSERT INTO chunk_fts(id, source, category, superseded, factory, department, confidentiality, buyer_code, body) "
            "VALUES(?,?,?,?,?,?,?,?,?)", rows)
        self.db.commit()
        return len(rows)

    def document_attributes(self, source: str) -> Optional[dict[str, Any]]:
        """The access attributes recorded for `source` (values None for a document from before F1)."""
        r = self.db.execute(f"SELECT {', '.join(ACCESS_FIELDS)} FROM document WHERE source=?", (source,)).fetchone()
        return dict(zip(ACCESS_FIELDS, r)) if r else None

    def retag(self, source: str, attrs: dict[str, str]) -> None:
        """New tags (TAG_FIELDS) for a document whose content did not change (moved folder, edited
        meta.yaml, or a document from before F1): the registry and the keyword index. The caller updates
        the vector store's metadata (ChromaStore.set_attributes); nothing is re-embedded."""
        keys = [k for k in TAG_FIELDS if k in attrs]          # column names come from the fixed list only
        sets = ", ".join(f"{k}=?" for k in keys)
        vals = [attrs[k] for k in keys]
        self.db.execute(f"UPDATE document SET {sets} WHERE source=?", (*vals, source))
        self.db.execute(f"UPDATE chunk_fts SET {sets} WHERE source=?", (*vals, source))
        self.db.commit()

    # ---- embedding cache (Phase E3): a revised PDF re-embeds only the chunks whose text changed
    def cached_vectors(self, hashes: list[str], model: str) -> dict[str, list[float]]:
        import numpy as np
        out: dict[str, list[float]] = {}
        for i in range(0, len(hashes), 500):          # stay under SQLite's bound-parameter limit
            part = hashes[i:i + 500]
            q = f"SELECT hash, vec FROM embedding_cache WHERE model=? AND hash IN ({','.join('?' * len(part))})"
            for h, blob in self.db.execute(q, (model, *part)):
                out[h] = np.frombuffer(blob, dtype=np.float32).tolist()
        return out

    def store_vectors(self, pairs: list[tuple[str, list[float]]], model: str) -> None:
        import numpy as np
        self.db.executemany("INSERT OR REPLACE INTO embedding_cache(hash, model, vec) VALUES(?,?,?)",
                            [(h, model, np.asarray(v, dtype=np.float32).tobytes()) for h, v in pairs])
        self.db.commit()

    def seed_embedding_cache(self, store, model: str) -> int:
        """One-off upgrade for an index built before E3: its chunks have no text_hash and the cache is
        empty, so the first revision of any existing PDF would re-embed it whole. The vectors are
        already in the store — copy them into the cache and fill text_hash. No-op once done."""
        if not self.db.execute("SELECT 1 FROM chunk WHERE text_hash IS NULL LIMIT 1").fetchone():
            return 0
        seeded = 0
        for cid, text, vec in store.all_texts_and_vectors():
            h = text_hash(text)
            self.db.execute("UPDATE chunk SET text_hash=? WHERE id=?", (h, cid))
            self.store_vectors([(h, list(vec))], model)
            seeded += 1
        self.db.commit()
        return seeded

    def prune_embedding_cache(self) -> int:
        """Drop cache rows no current chunk uses (removed documents, replaced text). Returns rows removed."""
        n = self.db.execute("DELETE FROM embedding_cache WHERE hash NOT IN "
                            "(SELECT text_hash FROM chunk WHERE text_hash IS NOT NULL)").rowcount
        self.db.commit()
        return n

    def remove_document(self, source: str) -> None:
        self.db.execute("DELETE FROM chunk WHERE source=?", (source,))
        self.db.execute("DELETE FROM chunk_fts WHERE source=?", (source,))
        self.db.execute("DELETE FROM document WHERE source=?", (source,))
        self.db.commit()

    def set_superseded(self, source: str, superseded: bool) -> None:
        self.db.execute("UPDATE document SET superseded=? WHERE source=?", (int(superseded), source))
        self.db.execute("UPDATE chunk_fts SET superseded=? WHERE source=?", (int(superseded), source))
        self.db.commit()

    def all_titles(self) -> dict[str, str]:
        """{source: title} for every document with an indexed version — used to group revisions by
        filename. (A file whose latest update failed is still indexed in its previous version.)"""
        return {r[0]: r[1] for r in self.db.execute("SELECT source, title FROM document WHERE doc_hash IS NOT NULL")}

    def stats(self) -> dict[str, Any]:
        q = self.db.execute
        return {
            "documents": q("SELECT COUNT(*) FROM document").fetchone()[0],
            "failed": q("SELECT COUNT(*) FROM document WHERE status='failed'").fetchone()[0],
            "pages": q("SELECT COALESCE(SUM(pages),0) FROM document").fetchone()[0],
            "chunks": q("SELECT COUNT(*) FROM chunk").fetchone()[0],
            "tables": q("SELECT COUNT(*) FROM chunk WHERE kind='table'").fetchone()[0],
            "ocr_pages": q("SELECT COALESCE(SUM(ocr_pages),0) FROM document").fetchone()[0],
        }


@lru_cache(maxsize=1)
def get_store() -> VectorStore:
    """One ChromaStore per process: opening a PersistentClient costs ~100 ms, and retrieve()
    used to pay that on every question. scripts/reindex.py clears this after deleting the index."""
    return ChromaStore()
