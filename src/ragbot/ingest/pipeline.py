"""Ingestion: data/pdfs/**/*.pdf -> chunks -> embeddings -> vector store + registry + BM25.

Idempotent: unchanged files (same SHA-256) are skipped; changed files are replaced whole;
removed files are deleted once two consecutive scans miss them, and never when the folder is missing,
empty or loses many files at once (PRD FR-2.14). Streams one document at a time so memory stays flat.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pymupdf

from ..config import log_dir, settings
from ..embed import EMBED_MODEL, get_embedder
from ..models import Chunk
from ..store import TAG_FIELDS, Registry, get_store, text_hash
from .chunker import chunk_page
from .meta import attributes_for
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


def _rel(path: Path, root: Path) -> str:
    """The file's path below pdf_root with forward slashes, as stored in document.rel_path."""
    return path.relative_to(root).as_posix()


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


def chunks_for(path: Path, root: Path, doc_hash: str,
               attrs: dict[str, str] | None = None) -> Iterator[tuple[Chunk, dict]]:
    """Yield (chunk, doc_stats) lazily for one PDF. The last yielded dict holds the totals. `attrs`: the
    file's access attributes (ingest/meta.py), computed here when not given."""
    s = settings()
    attrs = attrs if attrs is not None else attributes_for(path, root)
    # Closed explicitly: on Windows an open handle stops anyone replacing or deleting the file on the share.
    with pymupdf.open(path) as doc:
        yield from _chunks_of(doc, path, root, doc_hash, attrs, s)


def _chunks_of(doc: pymupdf.Document, path: Path, root: Path, doc_hash: str, attrs: dict[str, str],
               s) -> Iterator[tuple[Chunk, dict]]:
    if doc.is_encrypted and not doc.authenticate(""):
        raise ValueError("encrypted PDF")
    pages, ocr_pages, toc_pages = clean_pages(doc, s["ingest.ocr_min_text_chars"], s["ingest.header_footer_min_share"])
    title, source = path.stem, path.name                 # ids and Chunk.title use the file stem (REFERENCE_FORMAT)
    shown = display_title(doc, title)                     # the "<doc title> › <section>" prefix uses the real title
    base = dict(source=source, title=title, category=category_for(path, root), doc_hash=doc_hash,
                embed_model=EMBED_MODEL, ingested_at=datetime.now().isoformat(timespec="minutes"), **attrs)
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


MAX_ATTEMPTS = 3          # a file that fails this often is left alone until it changes (PRD FR-2.14)


class _LazyEmbedder:
    """Loads the embedding model only when a chunk actually needs embedding: a pass that finds nothing new
    (most worker passes) no longer loads 2 GB and contacts the model hub."""

    def __init__(self):
        self._emb = None

    def embed(self, texts):
        if self._emb is None:
            self._emb = get_embedder()
        return self._emb.embed(texts)


def _write_json(path: Path, data: dict) -> None:
    """Atomic: a reader (the app) sees the old file or the new one, never half of one."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _finish(summary: dict, reg: Registry, index_dir: Path, root: Path, started: datetime) -> dict:
    """Add the registry totals to a run's summary. ingest_state.json is rewritten only when the index
    changed or an alert started or ended: its mtime is the index version (index_version.py), and a new one
    makes the app drop its caches. last_scan.json is rewritten on every pass, for the app's status line."""
    stats = reg.stats()
    stats["failed_total"] = stats.pop("failed")       # registry-wide; summary["failed"] is this run's count
    summary = {**summary, **stats}
    now = datetime.now().isoformat(timespec="seconds")
    state_file = index_dir / "ingest_state.json"
    prev = _read_json(state_file)
    changed = any(summary.get(k) for k in ("added", "updated", "removed", "retagged", "superseded_changed"))
    if changed or prev is None or prev.get("alert", "") != summary.get("alert", ""):
        updated_at = now if changed or prev is None else prev.get("index_updated_at", prev.get("finished_at", now))
        _write_json(state_file, {"started_at": started.isoformat(timespec="seconds"), "finished_at": now,
                                 "index_updated_at": updated_at, "embed_model": EMBED_MODEL, "pdf_root": str(root),
                                 **summary})
    _write_json(index_dir / "last_scan.json", {
        "scanned_at": now, "changed": changed, "alert": summary.get("alert", ""),
        **{k: summary.get(k, 0) for k in ("documents", "failed", "failed_total", "retry", "pending_removal")},
        "duplicates": summary.get("duplicates", [])})
    return summary


def ingest_folder(root: Path | None = None, store=None, reg: Registry | None = None,
                  index_dir: Path | None = None, confirm_removals: bool = False,
                  redo: set[str] | frozenset[str] = frozenset()) -> dict:
    """store / reg / index_dir default to the app's index; tests pass a temporary one.

    Removals are cautious (PRD FR-2.14): a known file leaves the index only when two consecutive scans
    miss it. If the folder is missing or holds no PDFs, or more files vanish in one scan than
    `ingest.removal_alert_fraction` of the known ones (and at least `ingest.removal_alert_min_files`),
    nothing is deleted and the summary carries an "alert" — an offline share or a wrong mount must not
    empty the index. confirm_removals=True (scripts/ingest.py --confirm-removals) deletes every file
    missing now, for a deliberate bulk removal; a missing or empty folder is refused even then.
    `redo`: file names to index again although they did not change (the repair for inspect.py --check)."""
    s = settings()
    root = root or s.path("pdf_root")
    index_dir = index_dir or s.path("index_dir")
    store, reg = store or get_store(), reg or Registry()
    started = datetime.now()
    added = updated = removed = failed = pending_removal = retagged = retry = 0
    known_before = reg.all_sources()
    meta_cache: dict[Path, dict] = {}                          # meta.yaml files read once per run
    seen: set[str] = set()
    counts = {"embedded": 0, "cached": 0}

    logging.basicConfig(filename=log_dir() / "ingest.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    if not root.is_dir():
        alert = f"PDF folder {root} is missing or not a folder: nothing changed (is the share mounted?)"
        log.error(alert)
        return _finish({"added": 0, "updated": 0, "removed": 0, "pending_removal": 0, "retagged": 0, "failed": 0,
                        "alert": alert}, reg, index_dir, root, started)
    emb = _LazyEmbedder()
    batch = s["ingest.embed_batch_size"]
    run_id, interrupted = reg.start_run(started.isoformat(timespec="seconds"))
    for when in interrupted:
        log.warning("the ingest run started %s did not finish (crash or kill); resuming", when)
    # A document is embedded in full before its old chunks are touched, and marked dirty while the stores
    # are rewritten; a document left dirty (the process died mid-write, or a store failed) is written again.
    dirty = reg.dirty_sources()
    for source in sorted(dirty):
        log.warning("%s: its last write to the index did not finish; writing it again", source)
    redo = set(redo) | dirty
    seeded = reg.seed_embedding_cache(store, EMBED_MODEL)   # index built before E3: reuse its vectors
    if seeded:
        log.info("embedding cache seeded from %d existing vectors (no re-embedding)", seeded)
    fts_seeded = reg.seed_fts(store)                           # index built before D1: fill chunk_fts once
    if fts_seeded:
        log.info("keyword index (chunk_fts) seeded with %d chunks", fts_seeded)
    touched: set[str] = set()                                  # sources whose chunks were rewritten this run

    # H1: documents are keyed by file name (chunk ids too), so two PDFs with the same name in different
    # folders would overwrite each other. The copy already indexed wins, else the first by path; the others
    # are skipped and reported (inspect.py --failed) until one of them is renamed.
    by_name: dict[str, list[Path]] = {}
    for p in sorted(root.rglob("*.pdf")):
        by_name.setdefault(p.name, []).append(p)
    duplicates: list[str] = []
    chosen: list[Path] = []
    for name, paths in by_name.items():
        if len(paths) > 1:
            where = reg.indexed_path(name)
            row = reg.db.execute("SELECT category FROM document WHERE source=?", (name,)).fetchone()
            keep = next((p for p in paths if where and _rel(p, root) == where), None) or \
                next((p for p in paths if row and category_for(p, root) == row[0]), paths[0])
            for p in paths:
                if p is not keep:
                    duplicates.append(_rel(p, root))
                    log.error("SKIPPED %s: a file with the same name is indexed from %s; rename one of them",
                              _rel(p, root), _rel(keep, root))
        else:
            keep = paths[0]
        chosen.append(keep)

    for path in sorted(chosen):
        seen.add(path.name)
        row = reg.document_row(path.name) or {}               # one read per file; writes only on a change
        known = row.get("doc_hash")
        was = (row.get("rel_path"), row.get("file_size"), row.get("file_mtime"))
        try:
            st = path.stat()
            stamp = (_rel(path, root), st.st_size, st.st_mtime_ns)
            # same place, size and mtime as when the indexed version was hashed: unchanged, not read again
            h = known if known and was == stamp and path.name not in redo else sha256(path)
        except OSError as e:                                   # locked, no permission, share hiccup: next pass
            failed += 1
            reg.record_failure(path.name, path.stem, category_for(path, root),
                               f"cannot read the file: {e.__class__.__name__}: {e}")
            log.error("FAILED %s: cannot read the file (%s)%s", path.name, str(e),
                      "; the indexed version is kept" if known else "")
            continue
        try:
            attrs = attributes_for(path, root, meta_cache)
        except ValueError as e:                                # invalid meta.yaml: never index with a guess
            failed += 1
            reg.record_failure(path.name, path.stem, category_for(path, root), str(e))   # retried until fixed
            log.error("FAILED %s: %s%s", path.name, str(e), "; the indexed version is kept" if known else "")
            continue
        if known == h and path.name not in redo:
            if row.get("status") == "failed" and reg.clear_failure(path.name):
                log.info("recovered: %s (the indexed version is current again)", path.name)
            if was != stamp:                                     # moved, touched, or indexed before H3
                reg.set_stamp(path.name, stamp)
            # Same content; its category and access attributes may still have changed (a moved file, an
            # edited meta.yaml, or a document indexed before F1): retag it in place, nothing is re-embedded.
            tags = {"category": category_for(path, root), **attrs}
            if {k: row.get(k) for k in TAG_FIELDS} != tags:
                try:
                    # The vector store first: if it fails, the registry still holds the old tags, so the
                    # next pass sees the difference and retries. (Registry first left Chroma with the old,
                    # broader tags for good — the dense search filters on those.)
                    store.set_attributes(path.name, tags)
                    reg.retag(path.name, tags)
                except Exception as e:
                    failed += 1; retry += 1
                    log.error("FAILED to retag %s: %s (retried on the next pass)", path.name, str(e))
                    continue
                retagged += 1
                log.info("retagged: %s (%s)", path.name, ", ".join(f"{k}={v}" for k, v in tags.items()))
            continue
        if reg.failed_attempts(path.name, h) >= MAX_ATTEMPTS and path.name not in redo:
            continue                      # failed MAX_ATTEMPTS times: listed as failed until the file changes
        try:
            # Parse and embed everything first; only then replace the document in the stores, so a failure
            # (corrupt page, OCR error, embedding error) leaves the indexed version whole (PRD FR-2.14).
            stats: dict = {}
            all_chunks: list[Chunk] = []
            vectors: list[list[float]] = []
            doc_counts = {"embedded": 0, "cached": 0}
            pending: list[Chunk] = []
            for chunk, stats in chunks_for(path, root, h, attrs):
                pending.append(chunk); all_chunks.append(chunk)
                if len(pending) >= batch:
                    vectors += embed_with_cache([c.text for c in pending], reg, emb, doc_counts)
                    pending = []
            if pending:
                vectors += embed_with_cache([c.text for c in pending], reg, emb, doc_counts)
            if not all_chunks:
                raise ValueError("zero chunks (no text layer and OCR unavailable?)")
            reg.mark_dirty(path.name)
            store.delete_by_source(path.name)
            for i in range(0, len(all_chunks), batch):
                store.upsert(all_chunks[i:i + batch], vectors[i:i + batch])
            reg.replace_chunks(path.name, all_chunks)
            touched.add(path.name)
            counts["embedded"] += doc_counts["embedded"]; counts["cached"] += doc_counts["cached"]
            # the hash last: a crash before this line makes the next pass redo the document
            reg.upsert_document(source=path.name, title=path.stem, category=category_for(path, root), doc_hash=h,
                                pages=stats["pages"], chunks=stats["chunks"], tables_=stats["tables"],
                                ocr_pages=stats["ocr_pages"], status="ok", error=None, failed_hash=None, attempts=0,
                                rel_path=stamp[0], file_size=stamp[1], file_mtime=stamp[2], dirty=0,
                                ingested_at=datetime.now().isoformat(timespec="minutes"), **attrs)
            if known is None: added += 1
            else: updated += 1
            log.info("%s: %s (%d chunks, %d tables, %d OCR pages, %d TOC pages skipped; embedded %d / cached %d)",
                     "added" if known is None else "updated", path.name, stats["chunks"], stats["tables"],
                     stats["ocr_pages"], stats.get("toc_pages", 0), doc_counts["embedded"], doc_counts["cached"])
        except Exception as e:
            failed += 1
            n = reg.record_failure(path.name, path.stem, category_for(path, root), f"{e.__class__.__name__}: {e}", h)
            retry += n < MAX_ATTEMPTS
            log.error("FAILED %s: %s (attempt %d of %d%s%s)", path.name, str(e), n, MAX_ATTEMPTS,
                      "; the indexed version is kept" if known else "",
                      "; not retried until the file changes" if n >= MAX_ATTEMPTS else "")

    missing = reg.all_sources() - seen
    limit = max(int(s.get("ingest.removal_alert_min_files", 3)),
                float(s.get("ingest.removal_alert_fraction", 0.05)) * len(known_before))
    alert = ""
    if missing and not seen:
        alert = (f"no PDFs found under {root}, but the index holds {len(missing)} documents: nothing removed "
                 "(is the share mounted?)")
    elif not confirm_removals and len(missing) > limit:
        alert = (f"{len(missing)} of {len(known_before)} indexed files are missing from {root} in this scan: nothing "
                 "removed. Check the share or mount; if the removal is deliberate, run "
                 "`python scripts/ingest.py --confirm-removals`")
    if alert:
        log.error(alert)
        reg.record_scan(seen, count_missing=False)             # an outage must not count toward the two-scan rule
    else:
        missed = reg.record_scan(seen)
        for gone in sorted(missed):
            if missed[gone] >= 2 or confirm_removals:
                store.delete_by_source(gone); reg.remove_document(gone); removed += 1
                log.info("removed: %s (%s)", gone,
                         "removal confirmed" if missed[gone] < 2 else f"missing in {missed[gone]} consecutive scans")
            else:
                pending_removal += 1
                log.warning("missing: %s (removed if the next scan misses it too)", gone)

    # Recomputed from scratch every run (not just for changed files), so a later add/removal of a
    # revision is picked up even if the older file itself did not change.
    old = superseded_sources(reg.all_titles())
    superseded_now = 0
    for source in reg.all_sources():
        want = source in old
        row = reg.db.execute("SELECT superseded FROM document WHERE source=?", (source,)).fetchone()
        # A re-ingested file's fresh chunks are written with superseded=False; if its document row
        # already says superseded, re-apply it — otherwise an old revision touched on disk silently
        # reappeared in answers (found 2026-09-27).
        if row is not None and (bool(row[0]) != want or (want and source in touched)):
            store.set_superseded(source, want)       # store first, as for retagging: a failure is retried
            reg.set_superseded(source, want)
            superseded_now += 1
            log.info("%s: %s", "superseded" if want else "un-superseded", source)

    pruned = reg.prune_embedding_cache() if (updated or removed) else 0
    # No keyword-index rebuild: chunk_fts is maintained per document by the registry (Phase D1).
    reg.finish_run(run_id, datetime.now().isoformat(timespec="seconds"), added, updated, removed, failed)
    summary = {"added": added, "updated": updated, "removed": removed, "pending_removal": pending_removal,
               "retagged": retagged, "failed": failed + len(duplicates), "retry": retry, "superseded": len(old),
               "superseded_changed": superseded_now, "embedded": counts["embedded"], "cached": counts["cached"],
               "cache_pruned": pruned}
    if duplicates:
        summary["duplicates"] = duplicates
    if alert:
        summary["alert"] = alert
    return _finish(summary, reg, index_dir, root, started)
