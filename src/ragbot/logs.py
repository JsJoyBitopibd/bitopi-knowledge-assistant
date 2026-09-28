"""CSV logs in logs/ (git-ignored): one writer that keeps every file's rows matching its header, and the
audit read-back for administrators (PRD FR-4.8, FR-6.2).

A file whose header differs from the one being written (an older version wrote it) is renamed to
`<name>.<timestamp>.csv` first, never rewritten in place, so old rows keep the columns they were
written with.
"""
from __future__ import annotations

import csv
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import log_dir

_LOCK = threading.Lock()          # Streamlit serves every browser session from threads of one process


def append_row(name: str, header: list[str], row: Iterable[Any], folder: Optional[Path] = None) -> None:
    f = (folder or log_dir()) / name
    with _LOCK:
        if f.exists():
            with open(f, newline="", encoding="utf-8") as fh:
                first = next(csv.reader(fh), None)
            if first != header:
                stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
                target = f.with_name(f"{f.stem}.{stamp}{f.suffix}")
                n = 1
                while target.exists():
                    target = f.with_name(f"{f.stem}.{stamp}-{n}{f.suffix}"); n += 1
                f.rename(target)
        new = not f.exists()
        with open(f, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(header)
            w.writerow(list(row))


def rows_for_user(name: str, user: str, folder: Optional[Path] = None) -> list[dict[str, str]]:
    """Every row of logs/<name> and its rotated copies whose `user` column is `user`, oldest first.
    Rows from before a column existed have it empty."""
    folder = folder or log_dir()
    stem, suffix = Path(name).stem, Path(name).suffix
    out: list[dict[str, str]] = []
    for f in sorted(folder.glob(f"{stem}*{suffix}")):
        if f.name != name and not f.name.startswith(f"{stem}."):
            continue                                      # e.g. chat_other.csv is not a rotated chat.csv
        with open(f, newline="", encoding="utf-8") as fh:
            out += [r for r in csv.DictReader(fh) if (r.get("user") or "").lower() == user.lower()]
    return sorted(out, key=lambda r: r.get("ts", ""))
