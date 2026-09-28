"""Watch data/pdfs and re-ingest when it changes, so new/updated/removed PDFs reach the running app
without anyone running a command or restarting it.

    python scripts/ingest_worker.py            # loop forever, rescan every ingest.watch_interval_seconds
    python scripts/ingest_worker.py --once      # one pass and exit (use from Task Scheduler / cron)
    python scripts/ingest_worker.py --interval 120

The scan is cheap: only when the set of (name, size, mtime) differs from the previous pass does it call
ingest_folder(), which is itself idempotent (a file with the path, size and mtime it was indexed with is
not read; any other is compared by SHA-256). The index lock (ragbot/ingest/lock.py)
makes this worker, scripts/ingest.py and scripts/reindex.py mutually exclusive, so they never write the
index at the same time; a lock left by a crashed run is taken over as soon as its process is gone.

A file that disappears is removed only when two consecutive passes miss it, so a pass that finds a
missing file forces the next pass even if nothing else changed; so does a file that failed and will be
retried. A missing or empty folder, or many files vanishing at once, removes nothing and is logged as
an ALERT; `--once` then exits with code 2 so a scheduler shows the failure. A pass killed half-way is
safe to restart: a document's hash is recorded only after all its chunks are written, so the next pass
redoes the one that was being written (scripts/inspect.py --check shows any store left behind).
"""
import sys
import time
import logging
import _path  # noqa: F401

from ragbot.config import log_dir, settings
from ragbot.ingest.lock import IndexLocked, index_lock
from ragbot.ingest.pipeline import ingest_folder

log = logging.getLogger("ingest_worker")


def _snapshot(root):
    """(size, mtime) of every PDF and every meta.yaml: an edited meta.yaml changes who may see the files
    below it, so it must trigger a pass too (they are retagged without re-embedding)."""
    snap = {}
    for p in (*root.rglob("*.pdf"), *root.rglob("meta.yaml")):
        try:
            st = p.stat()
            snap[str(p)] = (st.st_size, st.st_mtime_ns)
        except OSError:
            pass
    return snap


def _changed(summary: dict) -> bool:
    return any(summary.get(k) for k in ("added", "updated", "removed", "retagged", "superseded_changed"))


def run_once() -> dict | None:
    """One ingest pass. Returns its summary, or None when another process holds the index lock.
    ingest_folder() itself logs an alert (missing/empty folder, mass disappearance) as an ERROR."""
    root = settings().path("pdf_root")
    try:
        with index_lock():
            summary = ingest_folder(root)
    except IndexLocked as e:
        log.info("%s; skipping this pass", e)
        return None
    if _changed(summary) or summary.get("pending_removal") or summary.get("failed"):
        log.info("ingested: %s", summary)
    return summary


def main() -> None:
    logging.basicConfig(filename=log_dir() / "ingest.log", level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # also echo to console so `--once` from a scheduler leaves a trail
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

    once = "--once" in sys.argv
    interval = int(settings().get("ingest.watch_interval_seconds", 300))
    if "--interval" in sys.argv:
        interval = int(sys.argv[sys.argv.index("--interval") + 1])

    if once:
        summary = run_once()
        if summary is None:
            print("index locked by another process")
        elif summary.get("alert"):
            print("ALERT:", summary["alert"])
            sys.exit(2)
        else:
            print("ingested" if _changed(summary) else "no change")
        return

    root = settings().path("pdf_root")
    log.info("watching %s every %ds", root, interval)
    prev, pending = None, 0
    while True:
        snap = _snapshot(root)
        # A missing file is removed only if the next pass misses it too, and a file that failed is retried
        # (up to pipeline.MAX_ATTEMPTS times), so either forces the next pass even when the folder looks
        # the same.
        if snap != prev or pending:
            summary = run_once()
            if summary is not None:              # locked: keep prev, so this change is retried next time
                pending = summary.get("pending_removal", 0) + summary.get("retry", 0)
                prev = _snapshot(root)           # re-read: ingest may rename/normalise nothing, but time passed
        time.sleep(interval)


if __name__ == "__main__":
    main()
