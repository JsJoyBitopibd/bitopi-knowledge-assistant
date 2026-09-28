"""Watch data/pdfs and re-ingest when it changes, so new/updated/removed PDFs reach the running app
without anyone running a command or restarting it.

    python scripts/ingest_worker.py            # loop forever, rescan every ingest.watch_interval_seconds
    python scripts/ingest_worker.py --once      # one pass and exit (use from Task Scheduler / cron)
    python scripts/ingest_worker.py --interval 120

The scan is cheap: only when the set of (name, size, mtime) differs from the previous pass does it call
ingest_folder(), which is itself idempotent (SHA-256 per file). A lock file makes a manual
scripts/ingest.py and this worker mutually exclusive so they never write the index at the same time.

A file that disappears is removed only when two consecutive passes miss it, so a pass that finds a
missing file forces the next pass even if nothing else changed. A missing or empty folder, or many
files vanishing at once, removes nothing and is logged as an ALERT; `--once` then exits with code 2 so
a scheduler shows the failure.
"""
import os
import sys
import time
import logging
import _path  # noqa: F401
from datetime import datetime

from ragbot.config import log_dir, settings
from ragbot.ingest.pipeline import ingest_folder

LOCK_STALE_SECONDS = 6 * 3600

log = logging.getLogger("ingest_worker")


def _snapshot(root):
    snap = {}
    for p in root.rglob("*.pdf"):
        try:
            st = p.stat()
            snap[str(p)] = (st.st_size, st.st_mtime_ns)
        except OSError:
            pass
    return snap


def _lock_path():
    return settings().path("index_dir") / "ingest.lock"


def _acquire_lock() -> bool:
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and (time.time() - lock.stat().st_mtime) > LOCK_STALE_SECONDS:
        log.warning("removing stale lock %s", lock)
        lock.unlink(missing_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {datetime.now().isoformat()}".encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_lock() -> None:
    _lock_path().unlink(missing_ok=True)


def _changed(summary: dict) -> bool:
    return bool(summary["added"] or summary["updated"] or summary["removed"])


def run_once() -> dict | None:
    """One ingest pass. Returns its summary, or None when another process holds the index lock.
    ingest_folder() itself logs an alert (missing/empty folder, mass disappearance) as an ERROR."""
    root = settings().path("pdf_root")
    if not _acquire_lock():
        log.info("index locked by another process; skipping this pass")
        return None
    try:
        summary = ingest_folder(root)
        if _changed(summary) or summary.get("pending_removal"):
            log.info("ingested: %s", summary)
        return summary
    finally:
        _release_lock()


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
        # A missing file is removed only if the next pass misses it too, so a pending removal forces
        # that pass even when the folder looks the same.
        if snap != prev or pending:
            summary = run_once()
            if summary is not None:              # locked: keep prev, so this change is retried next time
                pending = summary.get("pending_removal", 0)
                prev = _snapshot(root)           # re-read: ingest may rename/normalise nothing, but time passed
        time.sleep(interval)


if __name__ == "__main__":
    main()
