"""Ingestion: data/pdfs/**/*.pdf -> chunks -> embeddings -> vector store + registry + BM25.

Idempotent: unchanged files (same SHA-256) are skipped; changed files are replaced whole;
removed files are deleted. Streams one document at a time so memory stays flat.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pymupdf

from ..config import log_dir, settings
from ..embed import EMBED_MODEL, get_embedder
from ..models import Chunk
from ..retrieve.keyword import rebuild_keyword_index
from ..store import Registry, get_store, text_hash
from .chunker import chunk_page
from .pdf_text import clean_pages
from .tables import tables_on_page

log = logging.getLogger("ingest")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def category_for(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    return rel.parts[0] if len(rel.parts) > 1 else "General"


# "Bitopi_IT_SOP_Manual_v2" -> base "Bitopi_IT_SOP_Manual", revision 2. Also matches "Policy Rev3",
# "Manual_ver_10", "Handbook-v1.2" (the fractional part is dropped: compared as a tuple of ints).
_REVISION = re.compile(r"^(?P<base>.+?)[ _-]?(?:v|ver|rev|version)\.?\s*(?P<n>\d+(?:\.\d+)?)$", re.IGNORECASE)


def _version_key(n: str) -> tuple[int, ...]:
    """'2' -> (2,); '1.10' -> (1, 10) — compared as a tuple so v1.10 correctly outranks v1.2
    (a plain float() would get this backwards: float('1.10') == float('1.1') < float('1.2'))."""
    return tuple(int(p) for p in n.split("."))


def superseded_sources(titles: dict[str, str]) -> set[str]:
    """{source: title} -> the set of sources that are an OLDER revision of some other source with the
    same base name. Pure and re-computed on every ingest run, so it is naturally idempotent — this
    never depends on ingestion order or on state left over from a previous run."""
    groups: dict[str, list[tuple[tuple[int, ...], str]]] = {}
    for source, title in titles.items():
        m = _REVISION.match(title.strip())
        if not m:
            continue
        base = m.group("base").strip().lower()
        groups.setdefault(base, []).append((_version_key(m.group("n")), source))
    old: set[str] = set()
    for members in groups.values():
        if len(members) < 2:
            continue
        newest = max(n for n, _ in members)
        old |= {source for n, source in members if n < newest}
    return old


_BAD_TITLE = re.compile(r"^(microsoft word|untitled|document\d*)\b|\.(docx?|pdf)$", re.I)


def display_title(doc: pymupdf.Document, fallback: str) -> str:
    """Human title for the chunk prefix: the PDF metadata title when it is a real one, else the file stem."""
    t = ((doc.metadata or {}).get("title") or "").strip()
    if not t or len(t) > 120 or _BAD_TITLE.search(t):
        return fallback
    return t


def chunks_for(path: Path, root: Path, doc_hash: str) -> Iterator[tuple[Chunk, dict]]:
    """Yield (chunk, doc_stats) lazily for one PDF. The last yielded dict holds the totals."""
    s = settings()
    doc = pymupdf.open(path)
    if doc.is_encrypted and not doc.authenticate(""):
        raise ValueError("encrypted PDF")
    pages, ocr_pages, toc_pages = clean_pages(doc, s["ingest.ocr_min_text_chars"], s["ingest.header_footer_min_share"])
    title, source = path.stem, path.name                 # ids and Chunk.title use the file stem (REFERENCE_FORMAT)
    shown = display_title(doc, title)                     # the "<doc title> › <section>" prefix uses the real title
    base = dict(source=source, title=title, category=category_for(path, root), doc_hash=doc_hash,
                embed_model=EMBED_MODEL, ingested_at=datetime.now().isoformat(timespec="minutes"))
    stats = {"pages": len(pages), "chunks": 0, "tables": 0, "ocr_pages": ocr_pages, "toc_pages": len(toc_pages)}
    heading = ""
    for pno, (page, text) in enumerate(zip(doc, pages), start=1):
        if pno in toc_pages:                              # dotted-leader pages: no facts, and find_tables sees them as tables
            continue
        for tn, (caption, md) in enumerate(tables_on_page(page), start=1):
            stats["tables"] += 1; stats["chunks"] += 1
            yield Chunk(id=f"{title}#p{pno}#t{tn}", text=f"{shown} › {caption}\n{md}", page=pno,
                        section=caption, kind="table", **base), stats
        pieces, heading = chunk_page(shown, text, s["ingest.chunk_target_chars"], s["ingest.chunk_min_chars"],
                                     s["ingest.chunk_overlap_chars"], carry_heading=heading)
        for cn, (section, stored) in enumerate(pieces, start=1):
            stats["chunks"] += 1
            yield Chunk(id=f"{title}#p{pno}#c{cn}", text=stored, page=pno, section=section, kind="text", **base), stats


def embed_with_cache(texts: list[str], reg: Registry, emb, counts: dict) -> list[list[float]]:
    """Vectors for `texts`, embedding only the ones the registry's embedding cache does not hold
    (Phase E3). A revised 400-page manual whose text changed on 3 pages re-embeds only those chunks;
    everything else is a cache hit. Updates counts["embedded"] / counts["cached"]."""
    hashes = [text_hash(t) for t in texts]
    have = reg.cached_vectors(list(dict.fromkeys(hashes)), EMBED_MODEL)
    todo = list(dict.fromkeys(h for h in hashes if h not in have))
    if todo:
        text_of = dict(zip(hashes, texts))                 # one text per distinct hash
        new = dict(zip(todo, emb.embed([text_of[h] for h in todo])))
        reg.store_vectors(list(new.items()), EMBED_MODEL)
        have.update(new)
    counts["embedded"] += len(todo)
    counts["cached"] += len(texts) - len(todo)
    return [have[h] for h in hashes]


def ingest_folder(root: Path | None = None, store=None, reg: Registry | None = None,
                  index_dir: Path | None = None) -> dict:
    """store / reg / index_dir default to the app's index; tests pass a temporary one."""
    s = settings()
    root = root or s.path("pdf_root")
    index_dir = index_dir or s.path("index_dir")
    store, reg, emb = store or get_store(), reg or Registry(), get_embedder()
    batch = s["ingest.embed_batch_size"]
    started = datetime.now()
    added = updated = removed = failed = 0
    seen: set[str] = set()
    counts = {"embedded": 0, "cached": 0}

    logging.basicConfig(filename=log_dir() / "ingest.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    for path in sorted(root.rglob("*.pdf")):
        seen.add(path.name)
        h = sha256(path)
        known = reg.known_hash(path.name)
        if known == h:
            continue
        try:
            store.delete_by_source(path.name)
            pending: list[Chunk] = []
            stats: dict = {}
            all_chunks: list[Chunk] = []
            doc_counts = {"embedded": 0, "cached": 0}
            for chunk, stats in chunks_for(path, root, h):
                pending.append(chunk); all_chunks.append(chunk)
                if len(pending) >= batch:
                    store.upsert(pending, embed_with_cache([c.text for c in pending], reg, emb, doc_counts))
                    pending = []
            if pending:
                store.upsert(pending, embed_with_cache([c.text for c in pending], reg, emb, doc_counts))
            counts["embedded"] += doc_counts["embedded"]; counts["cached"] += doc_counts["cached"]
            if not all_chunks:
                raise ValueError("zero chunks (no text layer and OCR unavailable?)")
            reg.replace_chunks(path.name, all_chunks)
            reg.upsert_document(source=path.name, title=path.stem, category=category_for(path, root), doc_hash=h,
                                pages=stats["pages"], chunks=stats["chunks"], tables_=stats["tables"],
                                ocr_pages=stats["ocr_pages"], status="ok", error=None,
                                ingested_at=datetime.now().isoformat(timespec="minutes"))
            if known is None: added += 1
            else: updated += 1
            log.info("%s: %s (%d chunks, %d tables, %d OCR pages, %d TOC pages skipped; embedded %d / cached %d)",
                     "added" if known is None else "updated", path.name, stats["chunks"], stats["tables"],
                     stats["ocr_pages"], stats.get("toc_pages", 0), doc_counts["embedded"], doc_counts["cached"])
        except Exception as e:
            failed += 1
            reg.upsert_document(source=path.name, title=path.stem, category=category_for(path, root), doc_hash=h,
                                status="failed", error=f"{e.__class__.__name__}: {e}"[:500],
                                ingested_at=datetime.now().isoformat(timespec="minutes"))
            log.error("FAILED %s: %s", path.name, e)

    for gone in reg.all_sources() - seen:
        store.delete_by_source(gone); reg.remove_document(gone); removed += 1
        log.info("removed: %s", gone)

    # Recomputed from scratch every run (not just for changed files), so a later add/removal of a
    # revision is picked up even if the older file itself did not change.
    old = superseded_sources(reg.all_titles())
    superseded_now = 0
    for source in reg.all_sources():
        want = source in old
        row = reg.db.execute("SELECT superseded FROM document WHERE source=?", (source,)).fetchone()
        if row is not None and bool(row[0]) != want:
            reg.set_superseded(source, want)
            store.set_superseded(source, want)
            superseded_now += 1
            log.info("%s: %s", "superseded" if want else "un-superseded", source)

    pruned = reg.prune_embedding_cache() if (updated or removed) else 0
    if added or updated or removed:
        rebuild_keyword_index(store, index_dir)
    reg.db.execute("INSERT INTO ingest_run(started_at,finished_at,added,updated,removed,failed) VALUES(?,?,?,?,?,?)",
                   (started.isoformat(timespec="seconds"), datetime.now().isoformat(timespec="seconds"),
                    added, updated, removed, failed))
    reg.db.commit()
    summary = {"added": added, "updated": updated, "removed": removed, "failed": failed, "superseded": len(old),
               "embedded": counts["embedded"], "cached": counts["cached"], "cache_pruned": pruned, **reg.stats()}
    state = {"started_at": started.isoformat(timespec="seconds"), "finished_at": datetime.now().isoformat(timespec="seconds"),
             "embed_model": EMBED_MODEL, "pdf_root": str(root), **summary}
    (index_dir / "ingest_state.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    return summary
