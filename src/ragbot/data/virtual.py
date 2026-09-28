"""Virtual views: run rag.* SQL against the real tables today, without any DDL on the server.

Each catalog view MAY carry a `definition:` — a T-SQL SELECT over the real tables that the model never
sees. At execution the guarded SQL is wrapped textually:

    WITH rag_vw_X AS (<definition>) <sql with rag.vw_X -> rag_vw_X>

sqlglot is used to VALIDATE and LOCATE only, never to regenerate — the user-visible SQL stays byte-exact
with what the model or fixed tool wrote, and no sqlglot generator drift can silently change what runs.

When the DBA later creates real rag.* views (script: scripts/gen_rag_views.py), the `definition:` field
is removed from the YAML and the same SQL runs directly.

Row scope (PRD FR-4.4). For a user limited to some factories, every view is emitted with the filter
inside the view itself — `rag_vw_X AS (SELECT * FROM (<definition>) AS v WHERE v.[Factory] IN ('TAL'))`
— so counts, GROUP BYs, joins and the guard's TOP all see only in-scope rows. (A filter around the
whole statement could not: an aggregate has no factory column left to filter on, and TOP would have run
first.) The codes are validated by auth.models.Scope. A view without a `scope_column:` is refused for
such a user, and assert_scoped() re-parses the final statement before it can run.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

import sqlglot
from sqlglot import exp

from ..auth.models import Scope
from .guard import DENY, GuardError, cte_reference_ok, statement_ctes

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


def _scope_filter(column: str, scope: Scope, dialect: str) -> str:
    codes = scope.db_factories()          # validated short codes (auth.models.Scope), safe as literals
    if not codes:
        raise GuardError("your access includes no factory's data")
    col = f"[{column}]" if dialect == "tsql" else f"`{column}`"
    return f"WHERE v.{col} IN ({', '.join(repr(c) for c in codes)})"


def _cte_body(v: "View", scope: Scope, dialect: str) -> str:
    """The view as the rewritten statement runs it: its definition (or the real rag view), filtered to
    the scope's factories unless the scope covers every factory."""
    source = v.definition.strip().rstrip(";") if v.definition else f"SELECT * FROM {v.name}"
    if scope.all_factories:
        return source
    return f"SELECT * FROM (\n{source}\n) AS v\n{_scope_filter(v.scope_column, scope, dialect)}"


def rewrite_virtual(guarded_sql: str, cat: "Catalog", *, scope: Scope) -> tuple[str, list[str]]:
    """Expand the model's `rag.vw_X` references into CTEs from the catalog's `definition:` fields, each
    limited to the scope's factories (module docstring). `scope` has no default: scheduled jobs that
    copy everything pass Scope.unrestricted() explicitly.

    Returns (sql_exec, views_used). For an unrestricted scope, views without a `definition:` are left
    qualified — real views and virtual views coexist. `views_used` uses the catalog's canonical casing
    (e.g. `rag.vw_ExportOrder`).
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
            if not scope.all_factories and not v.scope_column:
                raise GuardError(f"{v.name} is not available to users limited to some factories")
            # a real view (no definition) is wrapped too when the rows must be filtered here
            if v.definition or not scope.all_factories:
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
        ctes = ",\n".join(f"{cte_name(v.name)} AS (\n{_cte_body(v, scope, cat.dialect)}\n)" for v in virtualise)
        if re.match(r"^\s*with\b", body, re.I):
            body = re.sub(r"^\s*with\b", "", body, count=1, flags=re.I).lstrip()
            body = f"WITH {ctes},\n{body}"
        else:
            body = f"WITH {ctes}\n{body}"

    # Belt and braces: re-scan the final string. Nothing forbidden slipped in during rewriting.
    if _DENY_RE.search(body):
        m = _DENY_RE.search(body)
        raise GuardError(f"forbidden token after rewrite: {m.group(0)!r}")
    if not scope.all_factories:
        assert_scoped(body, cat, scope, [cat.view(n) for n in used])
    return body, used


def assert_scoped(sql_exec: str, cat: "Catalog", scope: Scope, views: list["View"]) -> None:
    """Second line of defence for a restricted scope: parse the statement that is about to run and
    check that every rag view it reads is one of our CTEs, filtered on the view's scope column to
    exactly the scope's factories, and that every table it reads is accounted for: a schema-qualified
    table (rag.* or a base table) only inside those CTEs, a bare name only as a CTE declared before it
    is used — anything else would be a real table read without the filter."""
    read = "tsql" if cat.dialect == "tsql" else "mysql"
    tree = sqlglot.parse_one(sql_exec, read=read)
    ctes = {c.alias_or_name.lower(): c for c in tree.find_all(exp.CTE)}
    want = set(scope.db_factories())
    for v in views:
        cte = ctes.get(cte_name(v.name).lower())
        where = cte.this.args.get("where") if cte is not None else None
        ok = False
        for cond in (where.find_all(exp.In) if where is not None else []):
            col = cond.this
            vals = {e.this for e in cond.expressions if isinstance(e, exp.Literal) and e.is_string}
            if isinstance(col, exp.Column) and col.name.lower() == v.scope_column.lower() and vals == want \
                    and len(vals) == len(cond.expressions):
                ok = True
        if not ok:
            raise GuardError(f"scope check failed: {v.name} is not limited to {sorted(want)}")
    ours = {cte_name(v.name).lower() for v in views}
    with_ctes = statement_ctes(tree)
    for t in tree.find_all(exp.Table):
        if t.db:
            holder = t.find_ancestor(exp.CTE)
            if holder is None or holder.alias_or_name.lower() not in ours:
                raise GuardError(f"scope check failed: {t.sql()} is read outside its filtered view")
        elif not cte_reference_ok(t, with_ctes):
            raise GuardError(f"scope check failed: {t.name} is not a CTE defined before it is used")
