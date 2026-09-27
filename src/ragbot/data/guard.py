"""SQL guard: a generated statement runs only if it passes every check here (docs/DATA_ACCESS.md §4).
Non-negotiable #3. Tests in tests/test_guard.py must cover every deny-list entry.

Phase C4 (docs/ROADMAP.md) opens the guard, optionally, to raw discovered tables:
- `allowed_tables` — lower-case `schema.name` of every discovered table the catalog offers (the FULL
  offered set, not just what one prompt showed: security comes from this allow-list, relevance from
  the prompt). Empty (the default) keeps the pre-C4 rule: only rag.* views.
- no `SELECT *` (or `t.*`) when a raw table is referenced — columns must be named (COUNT(*) is fine).
- `big_tables` — raw tables above data.big_table_rows: a query touching one must have a WHERE.
- always: no column or table whose name is sensitive (src/ragbot/data/sensitive.py), and no
  FOR XML/JSON, @@globals or login/host functions.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from .sensitive import is_sensitive

DENY = [
    r"\binsert\b", r"\bupdate\b", r"\bdelete\b", r"\bmerge\b", r"\bdrop\b", r"\balter\b", r"\bcreate\b",
    r"\btruncate\b", r"\bexec(ute)?\b", r"\bgrant\b", r"\brevoke\b", r"\bdeny\b", r"\bbackup\b", r"\brestore\b",
    r"\bopenrowset\b", r"\bopenquery\b", r"\bopendatasource\b", r"\bbulk\b", r"\bxp_\w+", r"\bsp_\w+",
    r"\binto\b", r"\bload_file\b", r"\bload\s+data\b", r"\binformation_schema\b", r"\bsys\.",
    r"\bmysql\.", r"\bperformance_schema\b", r"\bwaitfor\b", r"\bshutdown\b", r"--", r"/\*", r";",
    # C4: data-shaping and session/identity probes
    r"\bfor\s+(xml|json)\b", r"@@\w+", r"\bsuser_\w*", r"\bsystem_user\b", r"\bhost_name\b",
    r"\boriginal_login\b",
]
_DENY_RE = re.compile("|".join(DENY), re.IGNORECASE)


class GuardError(ValueError):
    pass


def assert_read_only(sql: str, dialect: str = "tsql") -> None:
    """For curated SQL from config (config/aggregates.yaml): exactly one SELECT, no deny-list token.
    No row cap and no view allow-list — an aggregate may copy a whole view — but the same write
    checks as generated SQL, in front of the read-only, rolled-back connection."""
    s = sql.strip().rstrip(";").strip()
    m = _DENY_RE.search(s)
    if m:
        raise GuardError(f"forbidden token {m.group(0)!r}")
    if not re.match(r"^\s*(select|with)\b", s, re.IGNORECASE):
        raise GuardError("only SELECT / WITH ... SELECT is allowed")
    trees = sqlglot.parse(s, read="tsql" if dialect == "tsql" else "mysql")
    if len(trees) != 1 or not isinstance(trees[0], exp.Query):
        raise GuardError("exactly one SELECT statement is allowed")


def guard(sql: str, allowed_views: set[str], dialect: str, max_rows: int = 200, *,
          allowed_tables: frozenset[str] | set[str] = frozenset(),
          big_tables: dict[str, int] | None = None) -> str:
    """Validate and normalise. Returns SQL with a row cap applied. Raises GuardError otherwise."""
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise GuardError("empty statement")
    m = _DENY_RE.search(s)
    if m:
        raise GuardError(f"forbidden token {m.group(0)!r}")
    if not re.match(r"^\s*(select|with)\b", s, re.IGNORECASE):
        raise GuardError("only SELECT / WITH ... SELECT is allowed")

    read_dialect = "tsql" if dialect == "tsql" else "mysql"
    try:
        trees = sqlglot.parse(s, read=read_dialect)
    except Exception as e:
        raise GuardError(f"cannot parse: {e}") from e
    if len(trees) != 1 or trees[0] is None:
        raise GuardError("exactly one statement is allowed")
    tree = trees[0]
    if not isinstance(tree, (exp.Select, exp.Union)) and not (isinstance(tree, exp.Query)):
        raise GuardError("statement is not a query")

    # every table reference must be a plain identifier: either a CTE the statement itself defines,
    # or `rag.<name>` / `<name>` (case-insensitive) from the catalog's allow-list. Never a bare name
    # outside the catalog, never a three-part name, never a function call or other disguised table.
    bare = {v.split(".")[-1].lower() for v in allowed_views}
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    raw: list[str] = []                       # discovered (non-rag) tables this statement reads
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise GuardError(f"only plain view references are allowed, not {t.sql(read_dialect)!r}")
        if t.catalog:
            raise GuardError(f"three-part name not allowed: {t.sql(read_dialect)}")
        db, name = (t.db or "").lower(), t.name.lower()
        if is_sensitive(t.name):
            raise GuardError(f"restricted table: {t.name}")
        if not db:
            if name in ctes:
                continue
            raise GuardError(f"reference views as rag.<name>: {t.name}")
        if db != "rag":
            if f"{db}.{name}" in allowed_tables:
                raw.append(f"{db}.{name}")
                continue
            raise GuardError(f"schema not allowed: {db}" if not allowed_tables else f"table not in catalog: {db}.{t.name}")
        if name not in bare:
            raise GuardError(f"table/view not in catalog: rag.{t.name}")

    for c in tree.find_all(exp.Column):
        if c.name and is_sensitive(c.name):
            raise GuardError(f"restricted column: {c.name}")

    if raw:
        for star in tree.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                raise GuardError("SELECT * is not allowed on raw tables; name the columns")
        big = {n: rows for n, rows in (big_tables or {}).items() if n in raw}
        if big and tree.find(exp.Where) is None:
            n, rows = next(iter(big.items()))
            raise GuardError(f"{n} has {rows:,} rows: filter it with a WHERE clause")

    # row cap: read what sqlglot parsed (TOP maps to the `limit` arg in T-SQL), never re-derive from text
    lim = tree.args.get("limit")
    if lim is not None:
        rendered = lim.sql(read_dialect).upper()
        if "PERCENT" in rendered:
            raise GuardError("TOP ... PERCENT is not allowed; use a numeric row cap")
        n = lim.expression
        try:
            n_val = int(n.this) if n is not None else None
        except (TypeError, ValueError):
            n_val = None
        if n_val is not None and n_val > max_rows:
            raise GuardError(f"row cap {n_val} exceeds the maximum of {max_rows}")
    else:
        if dialect == "tsql":
            if tree.args.get("order") or isinstance(tree, exp.Union) or re.match(r"^\s*with\b", s, re.IGNORECASE):
                raise GuardError(f"add TOP ({max_rows}) to the final SELECT")
            s = re.sub(r"^\s*select\s+(distinct\s+)?", lambda m: f"SELECT {m.group(1) or ''}TOP ({max_rows}) ",
                       s, count=1, flags=re.IGNORECASE)
        else:
            s = f"{s} LIMIT {max_rows}"
    return s
