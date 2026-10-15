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


WITHHELD = "(withheld: restricted database)"
_WITHHOLD = ("question", "rewritten", "refs", "warnings", "agent", "databases_considered")


def withhold_restricted(rows: list[dict[str, str]], denied: Iterable[str],
                        denied_agents: Iterable[str] = ()) -> list[dict[str, str]]:
    """chat.csv rows as an administrator may see them (Phase S4). Being an admin is not a master key (S1), so a
    row about a database the viewing admin may not read keeps when it was, who asked, the scope, the route,
    whether it was not-found and the timings — and the question, the answer's sources, the warnings, the agent
    and the databases are withheld. The audit still shows that the person asked something.

    A row is about such a database when its sources or considered databases name it, when a warning reports an
    error from it ("data Hrms5Misami: timeout" — a failed fixed tool cites nothing), when it was answered by an
    agent that speaks only for denied databases (`denied_agents`), or when it shares a request id with such a
    row — which is what catches the thumbs-up/down row, whose warning carries the first 200 characters of the
    answer."""
    low = {d.lower() for d in denied}
    agents = {a.lower() for a in denied_agents}
    if not low and not agents:
        return rows

    def about_denied(r: dict[str, str]) -> bool:
        named = {x.split(":", 1)[-1].strip().lower() for x in (r.get("refs") or "").split(";") if x.strip()}
        named |= {x.strip().lower() for x in (r.get("databases_considered") or "").split(";") if x.strip()}
        warnings = (r.get("warnings") or "").lower()
        return bool(named & low) or any(f"data {d}:" in warnings for d in low) \
            or (r.get("agent") or "").strip().lower() in agents

    hit = {i for i, r in enumerate(rows) if about_denied(r)}
    ids = {rows[i].get("request_id") for i in hit} - {"", None}
    return [{**r, **{k: WITHHELD for k in _WITHHOLD if k in r}}
            if i in hit or (r.get("request_id") or None) in ids else r for i, r in enumerate(rows)]
