"""One writer at a time. scripts/ingest.py, ingest_worker.py and reindex.py all take data/index/ingest.lock
(created exclusively) before they touch the index, so two of them never write Chroma and the registry at
once. The holder refreshes the lock's mtime every minute while it works, so a lock is abandoned when its
process is gone (same host) or when nobody has refreshed it for `stale_s` (any host, e.g. the worker's
container); then the next writer takes it over. A long bulk ingest is never mistaken for a dead one.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from ..config import settings

HEARTBEAT_SECONDS = 60
STALE_SECONDS = 15 * 60


class IndexLocked(RuntimeError):
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE          # 64-bit: never truncate a handle to an int
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5            # access denied: it exists
        try:
            code = wintypes.DWORD()
            k32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == 259                       # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read(lock: Path) -> str:
    try:
        return lock.read_text(encoding="utf-8")
    except OSError:
        return ""


def _owner(lock: Path) -> tuple[str, int, str]:
    """(host, pid, since) written in the lock file; ('', 0, '') if unreadable."""
    try:
        host, pid, since = _read(lock).split(" ", 2)
        return host, int(pid), since.strip()
    except ValueError:
        return "", 0, ""


def _is_stale(lock: Path, stale_s: float) -> bool:
    host, pid, _ = _owner(lock)
    if host == socket.gethostname() and pid and not _pid_alive(pid):
        return True                                        # its process is gone: take over now
    try:
        return time.time() - lock.stat().st_mtime > stale_s   # its holder stopped refreshing it
    except OSError:
        return True


def _heartbeat(lock: Path, token: str, stop: threading.Event, every: float) -> None:
    while not stop.wait(every):
        if _read(lock) != token:                           # taken over (we were thought dead): leave it alone
            return
        try:
            os.utime(lock)
        except OSError:
            return


@contextmanager
def index_lock(index_dir: Optional[Path] = None, stale_s: float = STALE_SECONDS,
               heartbeat_s: float = HEARTBEAT_SECONDS) -> Iterator[None]:
    """Hold the index lock for the body; raises IndexLocked when another writer has it."""
    lock = (index_dir or settings().path("index_dir")) / "ingest.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and _is_stale(lock, stale_s):
        lock.unlink(missing_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        host, pid, since = _owner(lock)
        raise IndexLocked(f"the index is being written by process {pid} on {host or '?'} since {since or '?'}")
    token = f"{socket.gethostname()} {os.getpid()} {datetime.now().isoformat(timespec='seconds')}"
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat, args=(lock, token, stop, heartbeat_s), daemon=True)
    beat.start()
    try:
        yield
    finally:
        stop.set()
        beat.join(timeout=5)
        if _read(lock) == token:                           # never delete a lock another writer now holds
            lock.unlink(missing_ok=True)
