"""Virtual views: run rag.* SQL against the real tables today, without any DDL on the server.

Each catalog view MAY carry a `definition:` — a T-SQL SELECT over the real tables that the model never
sees. At execution the guarded SQL is wrapped textually:

    WITH rag_vw_X AS (<definition>) <sql with rag.vw_X -> rag_vw_X>

sqlglot is used to VALIDATE and LOCATE only, never to regenerate — the user-visible SQL stays byte-exact
with what the model or fixed tool wrote, and no sqlglot generator drift can silently change what runs.

When the DBA later creates real rag.* views (script: scripts/gen_rag_views.py), the `definition:` field
is removed from the YAML and the same SQL runs directly.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from .guard import DENY, GuardError

if TYPE_CHECKING:
    from .catalog import Catalog, View


_DENY_RE = re.compile("|".join(DENY), re.IGNORECASE)
# rag.<name>, [rag].[<name>], RAG.<name> — case-insensitive, brackets optional
_QUAL = re.compile(r"\[?\brag\b\]?\s*\.\s*\[?(\w+)\]?", re.IGNORECASE)


def cte_name(view_name: str) -> str:
    """Alias used in the wrapping CTE: `rag.vw_ExportOrder` -> `rag_vw_ExportOrder`."""
    return "rag_" + view_name.split(".")[-1]


def validate_definition(sql: str, dialect: str) -> None:
    """Reject definitions that would defeat the guard or fail T-SQL rules for a CTE body."""
    body = sql.strip().rstrip(";").strip()
    if not body:
        raise GuardError("empty definition")
    if _DENY_RE.search(body):
        m = _DENY_RE.search(body)
        raise GuardError(f"forbidden token in definition: {m.group(0)!r}")
    if re.search(r"[@?]", body):
        raise GuardError("definitions must not contain parameters (@name or ?)")
    if re.match(r"^\s*with\b", body, re.I):
        raise GuardError("definitions cannot start with WITH (T-SQL forbids nested CTEs); use a derived table")
    read = "tsql" if dialect == "tsql" else "mysql"
    try:
        trees = sqlglot.parse(body, read=read)
    except Exception as e:
        raise GuardError(f"cannot parse definition: {e}") from e
    if len(trees) != 1 or trees[0] is None:
        raise GuardError("definition must be exactly one SELECT")
    tree = trees[0]
    if not isinstance(tree, (exp.Select, exp.Union)):
        raise GuardError("definition must be a SELECT / UNION")
    if tree.args.get("order") and not tree.args.get("limit"):
        raise GuardError("ORDER BY without TOP/LIMIT is invalid inside a CTE — use ROW_NUMBER in a derived table")
    if tree.args.get("limit"):
        raise GuardError("definitions must not truncate rows (no TOP/LIMIT); use ROW_NUMBER() for 'latest per key'")
    for t in tree.find_all(exp.Table):
        db = (t.db or "").lower()
        if db == "rag":
            raise GuardError(f"definition of a rag.* view must not reference rag.* itself: {t.sql()}")
        # sqlglot strips the leading '#' into Identifier(temporary=True) rather than keeping it in .name
        if t.name.startswith("#") or getattr(t.this, "args", {}).get("temporary"):
            raise GuardError(f"temp tables not allowed in a definition: {t.name}")


def rewrite_virtual(guarded_sql: str, cat: "Catalog") -> tuple[str, list[str]]:
    """Expand the model's `rag.vw_X` references into CTEs from the catalog's `definition:` fields.

    Returns (sql_exec, views_used). Views without a `definition:` are left qualified — real views and
    virtual views coexist. `views_used` uses the catalog's canonical casing (e.g. `rag.vw_ExportOrder`).
    """
    read = "tsql" if cat.dialect == "tsql" else "mysql"
    tree = sqlglot.parse(guarded_sql, read=read)[0]
    model_ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}

    used: list[str] = []           # canonical names in first-seen order
    virtualise: list["View"] = []
    for t in tree.find_all(exp.Table):
        if (t.db or "").lower() != "rag":
            continue
        v = cat.view(f"rag.{t.name}")
        if v is None:
            raise GuardError(f"view not in catalog: rag.{t.name}")
        if v.name not in used:
            used.append(v.name)
            if v.definition:
                virtualise.append(v)

    # A stray `rag.` inside a string literal would slip through the textual rename. Fail loudly.
    for lit in tree.find_all(exp.Literal):
        if isinstance(lit.this, str) and re.search(r"\brag\.\w", lit.this, re.I):
            raise GuardError("literal contains 'rag.'; refuse to rewrite (ambiguous)")

    for v in virtualise:
        if cte_name(v.name).lower() in model_ctes:
            raise GuardError(f"CTE name {cte_name(v.name)!r} is reserved for virtual views")

    body = guarded_sql
    if virtualise:
        rename = {v.name.split(".")[-1].lower(): cte_name(v.name) for v in virtualise}

        def _sub(m: re.Match) -> str:
            return rename.get(m.group(1).lower(), m.group(0))

        body = _QUAL.sub(_sub, body)
        ctes = ",\n".join(f"{cte_name(v.name)} AS (\n{v.definition.strip().rstrip(';')}\n)" for v in virtualise)
        if re.match(r"^\s*with\b", body, re.I):
            body = re.sub(r"^\s*with\b", "", body, count=1, flags=re.I).lstrip()
            body = f"WITH {ctes},\n{body}"
        else:
            body = f"WITH {ctes}\n{body}"

    # Belt and braces: re-scan the final string. Nothing forbidden slipped in during rewriting.
    if _DENY_RE.search(body):
        m = _DENY_RE.search(body)
        raise GuardError(f"forbidden token after rewrite: {m.group(0)!r}")
    return body, used
