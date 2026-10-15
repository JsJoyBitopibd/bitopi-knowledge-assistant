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

Sampling (`--samples`) is the one part that reads data rows, and every sampled value is pasted into the SQL
prompts that show its table and embedded in the schema index. So, besides the size and type rule of
sample_candidates(): a people database is refused outright (samples_refused), columns whose name says they
hold a person or a login are never sampled (SAMPLE_SKIP_WORDS), and the number of queries is known before a
run (candidates_from_doc, offline) and capped (budget_refused).
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Any, Iterable, Optional

from .sensitive import PEOPLE_TABLE, is_sensitive, words

if TYPE_CHECKING:                       # catalog.py does not import this module; the hint only avoids a load
    from .catalog import Catalog

# Column types whose values may be sampled (short text codes/statuses only).
SAMPLE_TYPES = {"varchar", "nvarchar", "char", "nchar"}

# Sampling only, never the guard: words of a column name whose values are a person or a login — CreatedBy,
# UserName, EmpName, LoginID. The SQL model may still read these columns; what must not happen is their values
# being pasted into every prompt that shows the table (and embedded), so they reach the provider with questions
# that never asked about a person. Also hides *Name columns of things (BuyerName, UnitName): the cost of a
# missing sample is a weaker hint, the cost of a sampled name is a person's name sent to the provider.
SAMPLE_SKIP_WORDS: tuple[str, ...] = ("user", "login", "by", "emp", "name",
                                       # review of 2026-10-15: people named in non-people databases
                                       "person", "merchandiser", "inspector", "owner")
# Matched inside a word too, for names glued without a boundary or with a digit after them (InspectorName1,
# NTDUpdatedby, EntryUserid, ApprovedBy1): words that START a person/login column, and words that END one.
_SKIP_PREFIX: tuple[str, ...] = ("user", "login", "emp")
_SKIP_SUFFIX: tuple[str, ...] = ("by", "name", "names", "user", "userid", "person", "inspector", "merchandiser")


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
    them (a run without --samples, or with --tables, used to write every other column without its values).
    A column the skip list or the sensitive rules now exclude is NOT carried, nor any column of a people table:
    neither is sampled again, so its old values would otherwise stay in the file, and in the prompts, for good."""
    out: dict[tuple[str, str, str], list[str]] = {}
    for t in (previous or {}).get("tables", []):
        s, _, name = t["name"].partition(".")
        if PEOPLE_TABLE.search(name):
            continue
        for c in t.get("columns", []):
            if c.get("samples") and not c.get("sensitive") and not is_sensitive(c["name"]) \
                    and not sample_skipped(c["name"]):
                out[(s, name, c["name"])] = list(c["samples"])
    return out


def sample_skipped(column: str, skip: tuple[str, ...] = SAMPLE_SKIP_WORDS) -> bool:
    """True when a column's name says it holds a person or a login (SAMPLE_SKIP_WORDS): one of its words is a
    skip word or its plural (CreatedBy, UserName, EmpName, Users), or — for a name written as one run with no
    word boundary to split on ('createdby', 'USERNAME', 'empname') — the name starts or ends with one."""
    ws = [re.sub(r"\d+$", "", w) for w in words(column)]          # InspectorName1 -> inspector, name
    if any(w in skip or (w.endswith("s") and w[:-1] in skip) for w in ws if w):
        return True
    if skip != SAMPLE_SKIP_WORDS:
        return len(ws) == 1 and any(ws[0].startswith(s) or ws[0].endswith(s) for s in skip)
    return any(w.startswith(_SKIP_PREFIX) or w.endswith(_SKIP_SUFFIX) for w in ws if w)


def sample_candidates(tables: Iterable[tuple], columns: Iterable[tuple], max_rows: int,
                      max_len: int = 100, only: Optional[set[str]] = None) -> list[tuple[str, str, str]]:
    """Columns worth sampling for their distinct values: short text columns (`max_len` in BYTES, as
    sys.columns reports it: nvarchar(50) is 100) of base tables with at most `max_rows` rows, never a
    sensitive column, a column of a sensitive table or a column named for a person or login
    (sample_skipped), and — with `only` (lower-case schema.name) — only in those tables. Keeps the
    production server's cost bounded: each sample is one DISTINCT TOP 31 scan of a small table."""
    small = {(s, t) for s, t, kind, rows in tables
             if str(kind).strip() == "U" and rows is not None and int(rows) <= max_rows and not is_sensitive(t)
             and not PEOPLE_TABLE.search(t)          # a people table in any database: no values sampled
             and (only is None or f"{s}.{t}".lower() in only)}
    out = []
    for s, t, c, typ, mlen, _nullable, _cid in columns:
        if (s, t) in small and typ in SAMPLE_TYPES and mlen is not None and 0 < mlen <= max_len \
                and not is_sensitive(c) and not sample_skipped(c):
            out.append((s, t, c))
    return out


def _type_bytes(sql_type: str) -> tuple[str, Optional[int]]:
    """A discovery document's column type back into sys.columns terms: ('nvarchar', 100) for 'nvarchar(50)'
    (assemble() halved nvarchar/nchar lengths to characters; this doubles them back), -1 for '(max)', None
    for a type written without a length ('int', 'decimal')."""
    m = re.fullmatch(r"\s*([A-Za-z0-9_ ]+?)\s*(?:\(\s*(max|\d+)\s*\))?\s*", sql_type or "", re.IGNORECASE)
    if not m:
        return (sql_type or "").strip().lower(), None
    typ, n = m.group(1).lower(), m.group(2)
    if n is None:
        return typ, None
    if n.lower() == "max":
        return typ, -1
    return typ, int(n) * (2 if typ in ("nvarchar", "nchar") else 1)


def doc_rows(doc: dict[str, Any]) -> tuple[list[tuple], list[tuple]]:
    """A discovery document back into the (tables, columns) rows assemble() was built from, shaped as the
    sys.* queries return them, so sample_candidates() — the live rule, unchanged — can run over it."""
    tables: list[tuple] = []
    columns: list[tuple] = []
    for t in (doc or {}).get("tables", []):
        s, _, name = str(t["name"]).partition(".")
        tables.append((s, name, "V" if t.get("kind") == "view" else "U", t.get("rows")))
        for i, c in enumerate(t.get("columns", []), 1):
            typ, mlen = _type_bytes(str(c.get("type", "")))
            columns.append((s, name, c["name"], typ, mlen, c.get("nullable", True), i))
    return tables, columns


def candidates_from_doc(doc: dict[str, Any], tables: Optional[set[str]] = None, max_rows: int = 200_000,
                        max_len: int = 100) -> list[tuple[str, str, str]]:
    """The columns a --samples run would read, worked out OFFLINE from a discovery document already on disk
    (scripts/discover_schema.py --count-only): one sampling query each. `tables` is the same lower-case
    schema.name filter as sample_candidates' `only`. Row counts are those of the document's own run."""
    t_rows, c_rows = doc_rows(doc)
    return sample_candidates(t_rows, c_rows, max_rows, max_len=max_len, only=tables)


def queries_by_table(cands: Iterable[tuple[str, str, str]]) -> list[tuple[str, int]]:
    """[(schema.table, sampling queries)], most first: what --count-only prints."""
    counts = Counter(f"{s}.{t}" for s, t, _c in cands)
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def budget_refused(n_queries: int, max_queries: int) -> Optional[str]:
    """Why a sampling run of `n_queries` must not start under the `max_queries` cap, or None when it may."""
    if n_queries > max_queries:
        return (f"{n_queries:,} sampling queries exceed --max-queries {max_queries:,}; narrow the run "
                "(--tables, --tables-from-logs) or raise the cap once the count has been approved")
    return None


def samples_refused(cat: Optional["Catalog"]) -> Optional[str]:
    """Why a --samples run must not read this database's values, or None when it may.

    A people database — its catalog declares its own `sensitive_patterns:` (Phase R opened names, gender,
    designation, salary...) or limits who may read it with `restricted_to:` (Phase S) — would have those
    values pasted into the SQL prompt and embedded in the schema index. A database no catalog describes has
    no rule to be judged by. scripts/discover_schema.py refuses all three unless --allow-people-samples."""
    if cat is None:
        return "no catalog in config/catalog/ describes this database, so nothing says which columns are personal"
    if cat.sensitive_patterns is not None:
        return (f"{cat.database} declares its own sensitive_patterns ({', '.join(cat.sensitive_patterns)}): "
                "a people database, whose names, gender and designation would be sampled")
    if cat.restricted_to:
        return (f"{cat.database} is restricted_to {', '.join(cat.restricted_to)}: its values would reach the "
                "prompts and the schema index")
    return None
