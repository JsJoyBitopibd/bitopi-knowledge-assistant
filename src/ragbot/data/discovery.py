"""Assemble a discovered-schema document (Phase C1, docs/ROADMAP.md) from SQL Server metadata rows.

scripts/discover_schema.py runs the read-only sys.* queries and hands the rows here; this module
only reshapes them, so it is testable offline. Output: config/catalog/discovered/<db>.json
(git-ignored, like the index — regenerate it with the script), consumed from Phase C2 on.

Shape:
{"database", "conn_env", "generated_at", "tables": [
   {"name": "dbo.ExportOrder", "kind": "table"|"view", "rows": int|None, "description": str,
    "primary_key": [col, ...], "sensitive": bool,
    "columns": [{"name", "type", "nullable", "description", "sensitive", "samples"?: [...]}],
    "foreign_keys": [{"columns": [...], "ref_table": "dbo.Buyer", "ref_columns": [...]}]}]}
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any, Iterable, Optional

from .sensitive import is_sensitive

# Column types whose values may be sampled (short text codes/statuses only).
SAMPLE_TYPES = {"varchar", "nvarchar", "char", "nchar"}


def assemble(database: str, conn_env: str,
             tables: Iterable[tuple],        # (schema, name, kind 'U'|'V', rows|None)
             columns: Iterable[tuple],       # (schema, table, column, type, max_length, nullable, column_id)
             primary_keys: Iterable[tuple],  # (schema, table, column, key_ordinal)
             foreign_keys: Iterable[tuple],  # (fk_name, schema, table, column, ref_schema, ref_table, ref_column, ordinal)
             descriptions: Iterable[tuple],  # (schema, table, column|None, text)  column None = table description
             samples: Optional[dict[tuple[str, str, str], list[Any]]] = None) -> dict[str, Any]:
    samples = samples or {}
    tdesc: dict[tuple[str, str], str] = {}
    cdesc: dict[tuple[str, str, str], str] = {}
    for s, t, c, text in descriptions:
        if c is None:
            tdesc[(s, t)] = str(text)
        else:
            cdesc[(s, t, c)] = str(text)

    cols: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for row in columns:
        cols[(row[0], row[1])].append(row)
    pks: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    for s, t, c, ordinal in primary_keys:
        pks[(s, t)].append((ordinal, c))
    fks: dict[tuple[str, str, str], dict[str, Any]] = {}
    for fk, s, t, c, rs, rt, rc, ordinal in sorted(foreign_keys, key=lambda r: (r[0], r[7])):
        entry = fks.setdefault((s, t, fk), {"columns": [], "ref_table": f"{rs}.{rt}", "ref_columns": []})
        entry["columns"].append(c)
        entry["ref_columns"].append(rc)

    out_tables = []
    for s, t, kind, rows in tables:
        key = (s, t)
        col_docs = []
        for _, _, c, typ, max_len, nullable, _cid in sorted(cols.get(key, []), key=lambda r: r[6]):
            doc: dict[str, Any] = {"name": c, "type": _type(typ, max_len), "nullable": bool(nullable),
                                   "description": cdesc.get((s, t, c), ""), "sensitive": is_sensitive(c)}
            got = samples.get((s, t, c))
            if got and not doc["sensitive"]:
                doc["samples"] = [str(v) for v in got]
            col_docs.append(doc)
        out_tables.append({
            "name": f"{s}.{t}", "kind": "view" if str(kind).strip() == "V" else "table",
            "rows": int(rows) if rows is not None else None, "description": tdesc.get(key, ""),
            "primary_key": [c for _, c in sorted(pks.get(key, []))],
            "sensitive": is_sensitive(t),
            "columns": col_docs,
            "foreign_keys": [v for (fs, ft, _), v in sorted(fks.items()) if (fs, ft) == key],
        })
    return {"database": database, "conn_env": conn_env, "generated_at": datetime.now().isoformat(timespec="seconds"),
            "tables": out_tables}


def _type(typ: str, max_len: Optional[int]) -> str:
    """SQL Server type with its length: nvarchar max_length is in bytes (2 per char); -1 means MAX."""
    if typ in ("varchar", "char", "varbinary", "binary") and max_len is not None:
        return f"{typ}({'max' if max_len == -1 else max_len})"
    if typ in ("nvarchar", "nchar") and max_len is not None:
        return f"{typ}({'max' if max_len == -1 else max_len // 2})"
    return typ


def carried_samples(previous: Optional[dict[str, Any]]) -> dict[tuple[str, str, str], list[str]]:
    """The samples of an earlier discovery document, keyed like assemble()'s `samples`, so a new run keeps
    them (a run without --samples, or with --tables, used to write every other column without its values)."""
    out: dict[tuple[str, str, str], list[str]] = {}
    for t in (previous or {}).get("tables", []):
        s, _, name = t["name"].partition(".")
        for c in t.get("columns", []):
            if c.get("samples"):
                out[(s, name, c["name"])] = list(c["samples"])
    return out


def sample_candidates(tables: Iterable[tuple], columns: Iterable[tuple], max_rows: int,
                      max_len: int = 100, only: Optional[set[str]] = None) -> list[tuple[str, str, str]]:
    """Columns worth sampling for their distinct values: short text columns of base tables with at most
    `max_rows` rows, never a sensitive column or a column of a sensitive table, and — with `only` (lower-
    case schema.name) — only in those tables. Keeps the production server's cost bounded: each sample is
    one DISTINCT TOP 31 scan of a small table."""
    small = {(s, t) for s, t, kind, rows in tables
             if str(kind).strip() == "U" and rows is not None and int(rows) <= max_rows and not is_sensitive(t)
             and (only is None or f"{s}.{t}".lower() in only)}
    out = []
    for s, t, c, typ, mlen, _nullable, _cid in columns:
        if (s, t) in small and typ in SAMPLE_TYPES and mlen is not None and 0 < mlen <= max_len \
                and not is_sensitive(c):
            out.append((s, t, c))
    return out
