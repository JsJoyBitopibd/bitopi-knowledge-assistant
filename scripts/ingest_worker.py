"""Watch data/pdfs and re-ingest when it changes, so new/updated/removed PDFs reach the running app
without anyone running a command or restarting it.

    python scripts/ingest_worker.py            # loop forever, rescan every ingest.watch_interval_seconds
    python scripts/ingest_worker.py --once      # one pass and exit (use from Task Scheduler / cron)
    python scripts/ingest_worker.py --interval 120

The scan is cheap: only when the set of (name, size, mtime) differs from the previous pass does it call
ingest_folder(), which is itself idempotent (SHA-256 per file). A lock file makes a manual
scripts/ingest.py and this worker mutually exclusive so they never write the index at the same time.
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


def run_once() -> bool:
    """Ingest if the folder changed. Returns True if an ingest ran."""
    root = settings().path("pdf_root")
    if not _acquire_lock():
        log.info("index locked by another process; skipping this pass")
        return False
    try:
        summary = ingest_folder(root)
        if summary["added"] or summary["updated"] or summary["removed"]:
            log.info("ingested: %s", summary)
            return True
        return False
    finally:
        _release_lock()


def main() -> None:
    logging.basicConfig(filename=log_dir() / "ingest.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # also echo to console so `--once` from a scheduler leaves a trail
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))

    once = "--once" in sys.argv
    interval = int(settings().get("ingest.watch_interval_seconds", 300))
    if "--interval" in sys.argv:
        interval = int(sys.argv[sys.argv.index("--interval") + 1])

    if once:
        ran = run_once()
        print("ingested" if ran else "no change")
        return

    root = settings().path("pdf_root")
    log.info("watching %s every %ds", root, interval)
    prev = None
    while True:
        snap = _snapshot(root)
        if snap != prev:
            run_once()
            prev = _snapshot(root)   # re-read: ingest may rename/normalise nothing, but time passed
        time.sleep(interval)


if __name__ == "__main__":
    main()
