"""Vector store (Chroma behind an interface) and the document registry (SQLite).

The registry is the system of record: documents, pages, chunks, ingest state. The vector store
can always be rebuilt from the registry + source PDFs.
"""
from __future__ import annotations

import hashlib
import sqlite3
from abc import ABC, abstractmethod
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


def _chunk_from_record(cid: str, doc: str, meta: dict[str, Any], score: float = 0.0) -> Chunk:
    return Chunk(id=cid, text=doc, score=score, **{k: meta.get(k, Chunk.model_fields[k].default)
                                                   for k in ("source", "title", "page", "section", "kind", "category",
                                                             "doc_hash", "superseded", "embed_model", "ingested_at")})


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

    def count(self):
        return self.col.count()

    def set_superseded(self, source, superseded):
        res = self.col.get(where={"source": source}, include=[])
        ids = res["ids"]
        if ids:
            self.col.update(ids=ids, metadatas=[{"superseded": superseded}] * len(ids))


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


def text_hash(text: str) -> str:
    """Key of the embedding cache: the exact chunk text (prefix included) that gets embedded."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Registry:
    def __init__(self, path: Path | None = None):
        path = path or settings().path("index_dir") / "registry.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        # registries created before E3 lack chunk.text_hash (which ties cache rows to live chunks)
        if "text_hash" not in {r[1] for r in self.db.execute("PRAGMA table_info(chunk)")}:
            self.db.execute("ALTER TABLE chunk ADD COLUMN text_hash TEXT")
            self.db.commit()

    def known_hash(self, source: str) -> Optional[str]:
        r = self.db.execute("SELECT doc_hash FROM document WHERE source=?", (source,)).fetchone()
        return r[0] if r else None

    def all_sources(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT source FROM document")}

    def upsert_document(self, **f: Any) -> None:
        cols = ",".join(f); ph = ",".join("?" * len(f))
        upd = ",".join(f"{k}=excluded.{k}" for k in f if k != "source")
        self.db.execute(f"INSERT INTO document({cols}) VALUES({ph}) ON CONFLICT(source) DO UPDATE SET {upd}", tuple(f.values()))
        self.db.commit()

    def replace_chunks(self, source: str, chunks: list[Chunk]) -> None:
        self.db.execute("DELETE FROM chunk WHERE source=?", (source,))
        self.db.executemany("INSERT INTO chunk(id, source, page, section, kind, chars, text_hash) VALUES(?,?,?,?,?,?,?)",
                            [(c.id, c.source, c.page, c.section, c.kind, len(c.text), text_hash(c.text))
                             for c in chunks])
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

    def prune_embedding_cache(self) -> int:
        """Drop cache rows no current chunk uses (removed documents, replaced text). Returns rows removed."""
        n = self.db.execute("DELETE FROM embedding_cache WHERE hash NOT IN "
                            "(SELECT text_hash FROM chunk WHERE text_hash IS NOT NULL)").rowcount
        self.db.commit()
        return n

    def remove_document(self, source: str) -> None:
        self.db.execute("DELETE FROM chunk WHERE source=?", (source,))
        self.db.execute("DELETE FROM document WHERE source=?", (source,))
        self.db.commit()

    def set_superseded(self, source: str, superseded: bool) -> None:
        self.db.execute("UPDATE document SET superseded=? WHERE source=?", (int(superseded), source))
        self.db.commit()

    def all_titles(self) -> dict[str, str]:
        """{source: title} for every ok document — used to group revisions by filename."""
        return {r[0]: r[1] for r in self.db.execute("SELECT source, title FROM document WHERE status='ok'")}

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
