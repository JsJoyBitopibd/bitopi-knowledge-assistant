"""A read-only gateway on another SQL Server (docs/DATA_ACCESS.md "Gateway on the dev server"): that server holds
the rag.* views in a database of their own and reads the live server's tables through a linked server, so the
assistant can sign in to the gateway with its read-only login while nothing is created on the live server.

Pure rendering — scripts/gen_gateway.py writes the files; nothing here connects to a database. Four scripts and two
rollbacks come out, all for a DBA to review and run:

  00 live server:    a login for the link with SELECT on exactly the tables the views read (sensitive columns denied)
  01 gateway server: the linked server, mapped for the assistant's login only
  02 gateway server: the gateway database, schema rag, the views over four-part names, SELECT for the login
  03 both:           the fixed tools' queries, timed through the gateway and directly on the live server

Table names in a view's definition are found with sqlglot and rewritten in the text (never regenerated, as in
data/virtual.py), then the result is parsed again to prove every table now goes through the link.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

import sqlglot
from sqlglot import exp

from ..auth.models import Scope
from .catalog import Catalog
from .grants import guarded_denies, in_database, nstr, quote
from .scope_sql import SESSION_KEY, session_filtered

LINK_PASSWORD_VAR = "$(RAG_LINK_PASSWORD)"
DBA_LOGIN_VAR = "$(DBA_LOGIN)"


# ── table names ────────────────────────────────────────────────────────────────────────────────────────────────

def view_tables(definition: str, dialect: str = "tsql") -> list[str]:
    """'schema.table' for every table a view definition reads, in first-seen order. A table must be named with its
    schema and without a database or server part (the gateway adds both); a column named schema.table.column is
    refused too, since it could not be rewritten safely."""
    tree = sqlglot.parse_one(definition, read=dialect)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out: list[str] = []
    for t in tree.find_all(exp.Table):
        if not t.db:
            if t.name.lower() in ctes:
                continue
            raise ValueError(f"table {t.name!r} has no schema: write it as schema.table")
        if t.catalog:
            raise ValueError(f"{t.sql(dialect=dialect)}: cross-database names are not supported in a gateway view")
        name = f"{t.db}.{t.name}"
        if name.lower() not in {n.lower() for n in out}:
            out.append(name)
    for c in tree.find_all(exp.Column):
        if c.args.get("db"):
            raise ValueError(f"{c.sql(dialect=dialect)}: write columns as alias.column, not schema.table.column")
    return out


def _name_pattern(schema: str, table: str) -> re.Pattern:
    part = lambda s: rf"(?:\[{re.escape(s)}\]|\b{re.escape(s)}\b)"      # noqa: E731 — [name] or name
    return re.compile(rf"(?<![\w\].]){part(schema)}\s*\.\s*{part(table)}(?![\w\[])(?!\s*\.)", re.IGNORECASE)


def qualify_tables(definition: str, link: str, database: str, dialect: str = "tsql") -> str:
    """The definition with every `schema.table` (bracketed or not) rewritten to `[link].[database].[schema].[table]`,
    outside string literals only; then parsed again: every table must now be a four-part name through `link`."""
    tables = view_tables(definition, dialect)
    parts = definition.split("'")                  # even pieces are outside string literals
    for name in tables:
        schema, table = name.split(".", 1)
        target = f"{quote(link)}.{quote(database)}.{quote(schema)}.{quote(table)}"
        pat = _name_pattern(schema, table)
        for i in range(0, len(parts), 2):
            parts[i] = pat.sub(target, parts[i])
    out = "'".join(parts)
    tree = sqlglot.parse_one(out, read=dialect)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for t in tree.find_all(exp.Table):
        if not t.db and t.name.lower() in ctes:
            continue
        names = [p.name for p in t.parts]
        if len(names) != 4 or names[0].lower() != link.lower():
            raise ValueError(f"not rewritten to the linked server: {t.sql(dialect=dialect)}")
    return out


def gateway_tables(catalogs: dict[str, Catalog]) -> dict[str, list[str]]:
    """{source database: tables the gateway views read there}, for the link login's grants."""
    out: dict[str, list[str]] = {}
    for cat in catalogs.values():
        for v in cat.views:
            if v.definition and cat.dialect == "tsql":
                src = cat.connection_database or cat.database
                have = out.setdefault(src, [])
                have += [t for t in view_tables(v.definition) if t.lower() not in {h.lower() for h in have}]
    return out


# ── 00: the link's login on the live server ────────────────────────────────────────────────────────────────────

def render_link_login(login: str, catalogs: dict[str, Catalog], live_label: str) -> str:
    L = quote(login)
    lines = [
        f"-- 00 — run on the LIVE server ({live_label}) as a sysadmin, in SQLCMD mode. Review first.",
        f'-- Password: :setvar RAG_LINK_PASSWORD "..."   (never stored in this file). Rollback: 99_rollback_live.sql.',
        f"-- Creates {login}, the login the gateway's linked server signs in with: SELECT on exactly the tables the",
        "-- rag views read, their sensitive columns denied, nothing else (no server-wide read, no write).",
        "",
        "USE [master];",
        f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = {nstr(login)})",
        f"    CREATE LOGIN {L} WITH PASSWORD = N'{LINK_PASSWORD_VAR}', CHECK_POLICY = ON, CHECK_EXPIRATION = OFF,",
        "        DEFAULT_DATABASE = [master];",
        "GO",
        "",
    ]
    by_db = gateway_tables(catalogs)
    cats_by_db = {(c.connection_database or c.database): c for c in catalogs.values()}
    for db, tables in by_db.items():
        cat = cats_by_db.get(db)
        stmts = [f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = {nstr(login)})",
                 f"    CREATE USER {L} FOR LOGIN {L};"]
        for name in tables:
            obj = ".".join(quote(p) for p in name.split(".", 1))
            stmts.append(f"IF OBJECT_ID({nstr(obj)}) IS NOT NULL GRANT SELECT ON OBJECT::{obj} TO {L};")
            t = cat.table(name) if cat else None
            if t is not None:
                stmts += guarded_denies(t, login)
        lines += [f"-- {db}: {len(tables)} tables read by the rag views."]
        lines += in_database(db, stmts)
    return "\n".join(lines) + "\n"


# ── 01: the linked server on the gateway ───────────────────────────────────────────────────────────────────────

def render_linked_server(link: str, live_host: str, link_login: str, reader: str) -> str:
    S = nstr(link)
    return "\n".join([
        f"-- 01 — run on the GATEWAY server as a sysadmin, in SQLCMD mode, after 00 ran on the live server. Review first.",
        f'-- :setvar RAG_LINK_PASSWORD "..."   (the password 00 used)',
        f'-- :setvar DBA_LOGIN "DOMAIN\\you"    (your own login, mapped too so you can run 03; remove it afterwards)',
        f'-- :setvar LINK_PROVIDER "MSOLEDBSQL" (the Microsoft OLE DB Driver for SQL Server must be installed here)',
        "-- Rollback: 99_rollback_gateway.sql.",
        "",
        "USE [master];",
        f"IF NOT EXISTS (SELECT 1 FROM sys.servers WHERE name = {S})",
        f"    EXEC sp_addlinkedserver @server = {S}, @srvproduct = N'', @provider = N'$(LINK_PROVIDER)',",
        f"         @datasrc = {nstr(live_host)};",
        "-- Read only: data access on; no remote procedure calls in either direction.",
        f"EXEC sp_serveroption {S}, 'data access', 'true';",
        f"EXEC sp_serveroption {S}, 'rpc', 'false';",
        f"EXEC sp_serveroption {S}, 'rpc out', 'false';",
        f"EXEC sp_serveroption {S}, 'remote proc transaction promotion', 'false';",
        f"EXEC sp_serveroption {S}, 'lazy schema validation', 'false';",
        "-- 'collation compatible' lets filters on text columns run on the live server (faster). Turn it on only if",
        "-- the two collations printed at the end of this script are the same:",
        f"--   EXEC sp_serveroption {S}, 'collation compatible', 'true';",
        "",
        "-- Logins: no default mapping (other logins on this server cannot reach the live server through the link);",
        f"-- only {reader} (the assistant) and the DBA running the speed check are mapped, to {link_login}.",
        f"IF EXISTS (SELECT 1 FROM sys.linked_logins ll JOIN sys.servers s ON s.server_id = ll.server_id",
        f"           WHERE s.name = {S} AND ll.local_principal_id = 0)",
        f"    EXEC sp_droplinkedsrvlogin @rmtsrvname = {S}, @locallogin = NULL;",
        f"EXEC sp_addlinkedsrvlogin @rmtsrvname = {S}, @useself = 'FALSE', @locallogin = {nstr(reader)},",
        f"     @rmtuser = {nstr(link_login)}, @rmtpassword = N'{LINK_PASSWORD_VAR}';",
        f"EXEC sp_addlinkedsrvlogin @rmtsrvname = {S}, @useself = 'FALSE', @locallogin = N'{DBA_LOGIN_VAR}',",
        f"     @rmtuser = {nstr(link_login)}, @rmtpassword = N'{LINK_PASSWORD_VAR}';",
        "GO",
        "",
        "-- Smoke test (as the DBA): the link answers, and the two collations to compare.",
        f"SELECT TOP (1) name FROM {quote(link)}.[master].[sys].[databases] ORDER BY name;",
        "SELECT CONVERT(nvarchar(128), SERVERPROPERTY('Collation')) AS gateway_collation,",
        f"       (SELECT c FROM OPENQUERY({quote(link)}, 'SELECT CONVERT(nvarchar(128), SERVERPROPERTY(''Collation'')) AS c'))"
        " AS live_collation;",
        "GO",
    ]) + "\n"


# ── 02: the gateway database and its views ─────────────────────────────────────────────────────────────────────

def render_gateway_views(link: str, gateway_db: str, catalogs: dict[str, Catalog], reader: str) -> str:
    names: dict[str, str] = {}
    lines = [
        f"-- 02 — run on the GATEWAY server as a sysadmin, after 01. Review first. Rollback: 99_rollback_gateway.sql.",
        f"-- A database of its own ({gateway_db}), so the gateway's live rows never mix with this server's own data.",
        "-- Row scope as in the app (data/scope_sql.py): a view with a factory column returns the factories named in",
        f"-- the session context key '{SESSION_KEY}' (comma list, or * for all), and nothing without one.",
        "",
        "USE [master];",
        f"IF DB_ID({nstr(gateway_db)}) IS NULL CREATE DATABASE {quote(gateway_db)};",
        "GO",
        f"USE {quote(gateway_db)};",
        "GO",
        "IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = N'rag') EXEC(N'CREATE SCHEMA rag AUTHORIZATION dbo');",
        "GO",
        "",
    ]
    for cat in catalogs.values():
        if cat.dialect != "tsql":
            continue
        src = cat.connection_database or cat.database
        for v in cat.views:
            if not v.definition:
                continue
            bare = v.name.split(".")[-1]
            if bare.lower() in names:
                raise ValueError(f"two views named rag.{bare} ({names[bare.lower()]} and {cat.database}): "
                                 "the gateway holds them in one schema")
            names[bare.lower()] = cat.database
            body = qualify_tables(v.definition.strip().rstrip(";"), link, src)
            if v.scope_column:
                body = session_filtered(body, v.scope_column)
            lines += [f"-- rag.{bare} — from {src}" + (f": {' '.join(v.description.split())}" if v.description else ""),
                      f"CREATE OR ALTER VIEW rag.{quote(bare)} AS", body, "GO", ""]
    R = quote(reader)
    lines += [
        f"-- The assistant's login reads the rag schema here, nothing else in this database.",
        f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = {nstr(reader)})",
        f"    CREATE USER {R} FOR LOGIN {R};",
        f"GRANT SELECT ON SCHEMA::[rag] TO {R};",
        "GO",
    ]
    return "\n".join(lines) + "\n"


# ── 03: speed check ────────────────────────────────────────────────────────────────────────────────────────────

def _sql_type(v: Any) -> str:
    if isinstance(v, bool):
        return "bit"
    if isinstance(v, datetime):
        return "datetime2"
    if isinstance(v, date):
        return "date"
    if isinstance(v, int):
        return "int"
    if isinstance(v, (float, Decimal)):
        return "decimal(38, 10)"
    return "nvarchar(4000)"


def _sql_literal(v: Any) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, datetime):
        return f"'{v.isoformat(sep=' ', timespec='seconds')}'"
    if isinstance(v, date):
        return f"'{v.isoformat()}'"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    return nstr(str(v))


def timed_exec(label: str, sql: str, params: dict[str, Any]) -> list[str]:
    """One batch: run `sql` the way the app does (sp_executesql with typed parameters, so the plan is the one the
    app gets) and print how long it took."""
    from .connectors import _positional
    _, used = _positional(sql, "@")
    names = list(dict.fromkeys(used))
    missing = [n for n in names if n not in params]
    if missing:
        raise ValueError(f"{label}: no value for {', '.join('@' + n for n in missing)}")
    call = f"EXEC sp_executesql {nstr(sql)}"
    if names:
        call += ",\n     " + nstr(", ".join(f"@{n} {_sql_type(params[n])}" for n in names))
        call += ",\n     " + ", ".join(f"@{n} = {_sql_literal(params[n])}" for n in names)
    return [
        f"PRINT {nstr('== ' + label)};",
        "DECLARE @t0 datetime2(3) = SYSDATETIME();",
        call + ";",
        f"PRINT {nstr(label + ': ')} + CONVERT(nvarchar(20), DATEDIFF(millisecond, @t0, SYSDATETIME())) + N' ms';",
        "GO",
        "",
    ]


def _raw_tables(sql: str, dialect: str) -> list[str]:
    """Tables a query reads that are not rag views (nor its own CTEs)."""
    tree = sqlglot.parse_one(sql, read="tsql" if dialect == "tsql" else "mysql")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return sorted({t.sql() for t in tree.find_all(exp.Table)
                   if (t.db or "").lower() != "rag" and t.name.lower() not in ctes})


def render_speed_check(cases: Iterable[tuple[dict[str, Any], dict[str, Any]]], catalogs: dict[str, Catalog], *,
                       via_gateway: bool, gateway_db: str, server_label: str, limits: tuple[int, int]) -> str:
    """The fixed tools' queries with their example parameters. via_gateway: against the gateway's real rag views,
    session context '*'. Otherwise: exactly what the app runs on the live server today (virtual views expanded)."""
    from .virtual import rewrite_virtual
    normal, heavy = limits
    lines = [
        f"-- 03 — run on the {server_label} as the DBA. Only SELECTs. In SSMS: Query > Query Options > Results >",
        "-- 'Discard results after execution', then copy the Messages tab: one 'name: N ms' line per query.",
        f"-- Pass: each query through the gateway takes at most 2x its direct time, and at most {normal} s",
        f"-- ({heavy} s for the ones marked heavy): the app gives up after that.",
        "",
    ]
    if via_gateway:
        lines += [f"USE {quote(gateway_db)};",
                  f"EXEC sp_set_session_context @key = {nstr(SESSION_KEY)}, @value = N'*';",
                  "GO", ""]
    current = None
    for tool, params in cases:
        cat = catalogs[tool["database"]]
        sql = tool["sql"].strip()
        raw = _raw_tables(sql, cat.dialect)
        if via_gateway and raw:
            lines += [f"-- skipped {tool['name']}: reads {', '.join(raw)} directly, not through a rag view", ""]
            continue
        if not via_gateway:
            sql, _ = rewrite_virtual(sql, cat, scope=Scope.unrestricted())
            db = cat.connection_database or cat.database
            if db != current:
                lines += [f"USE {quote(db)};", "GO", ""]
                current = db
        tags = [t for t in ("heavy" if tool.get("heavy") else "",
                            "the app answers it from its local copy" if tool.get("aggregate") else "") if t]
        label = tool["name"] + (f" ({'; '.join(tags)})" if tags else "")
        lines += timed_exec(label, sql, params)
    return "\n".join(lines) + "\n"


# ── rollbacks ──────────────────────────────────────────────────────────────────────────────────────────────────

def render_rollback_live(login: str, catalogs: dict[str, Catalog]) -> str:
    L = quote(login)
    lines = [f"-- Undo 00 on the LIVE server: drop the link login's users, then the login. Run as a sysadmin.", ""]
    for db in gateway_tables(catalogs):
        lines += in_database(db, [f"IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = {nstr(login)})",
                                  f"    DROP USER {L};"])
    lines += ["USE [master];",
              f"IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = {nstr(login)})",
              f"    DROP LOGIN {L};",
              "GO"]
    return "\n".join(lines) + "\n"


def render_rollback_gateway(link: str, gateway_db: str) -> str:
    return "\n".join([
        "-- Undo 02 and 01 on the GATEWAY server: drop the gateway database (its views go with it), then the linked",
        "-- server and its login mappings. Run as a sysadmin. Stop the assistant first: open connections are cut.",
        "",
        "USE [master];",
        f"IF DB_ID({nstr(gateway_db)}) IS NOT NULL",
        "BEGIN",
        f"    ALTER DATABASE {quote(gateway_db)} SET SINGLE_USER WITH ROLLBACK IMMEDIATE;",
        f"    DROP DATABASE {quote(gateway_db)};",
        "END",
        f"IF EXISTS (SELECT 1 FROM sys.servers WHERE name = {nstr(link)})",
        f"    EXEC sp_dropserver @server = {nstr(link)}, @droplogins = 'droplogins';",
        "GO",
    ]) + "\n"
