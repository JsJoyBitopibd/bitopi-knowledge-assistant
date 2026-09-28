"""Which raw tables the SQL model actually needs (G2): the tables schema selection showed it, logged per
question to logs/schema_select.csv, plus the raw tables its generated SQL read (logs/sql.csv). That
short list is what `discover_schema.py --samples --tables-from-logs` samples, instead of the ~6,700
columns of a full pass over the production server."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import log_dir
from ..logs import append_row

SELECT_HEADER = ["ts", "user", "database", "tables", "question"]


def log_selection(database: str, tables: list[str], question: str, user: str = "") -> None:
    if tables:
        append_row("schema_select.csv", SELECT_HEADER,
                   [datetime.now().isoformat(timespec="seconds"), user, database, ";".join(tables), question])


def _rows(folder: Path, stem: str):
    for f in sorted(folder.glob(f"{stem}*.csv")):
        if f.name != f"{stem}.csv" and not f.name.startswith(f"{stem}."):
            continue
        with open(f, newline="", encoding="utf-8") as fh:
            yield from csv.DictReader(fh)


def used_tables(cat, folder: Optional[Path] = None) -> list[str]:
    """Raw tables of `cat` (catalog.Catalog) that schema selection showed or generated SQL read, most
    used first. Curated rag.* views are not raw tables and are left out."""
    from .tools import _views_in
    folder = folder or log_dir()
    counts: dict[str, int] = {}
    for r in _rows(folder, "schema_select"):
        if r.get("database") == cat.database:
            for n in (r.get("tables") or "").split(";"):
                if n and cat.table(n):
                    counts[cat.table(n).name] = counts.get(cat.table(n).name, 0) + 1
    for r in _rows(folder, "sql"):
        if r.get("tool") in ("generated", "cache:generated") and r.get("sql"):
            for n in _views_in(r["sql"], cat):
                if not cat.view(n) and cat.table(n):
                    counts[cat.table(n).name] = counts.get(cat.table(n).name, 0) + 1
    return sorted(counts, key=lambda n: (-counts[n], n))
