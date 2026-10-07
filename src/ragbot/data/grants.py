"""Render the T-SQL a sysadmin runs to give the assistant one read-only login (`rag_reader`) on the whole
instance — and the script that takes it away again. scripts/gen_reader_grants.py writes the files; nothing
here connects to a database.

Why instance-wide read rather than "SELECT on schema rag only" (docs/DATA_ACCESS.md §1): this build's catalog
views are virtual (`definition:` SQL over base tables, data/virtual.py), and discover_schema.py,
check_catalog.py --live and refresh_aggregates.py read base tables too. Server-level CONNECT ANY DATABASE +
SELECT ALL USER SECURABLES gives read access to every current and future database in one statement; the
sensitive tables and columns found by schema discovery (data/sensitive.py) get an explicit DENY in each
discovered database, and a DENY wins over the server-level GRANT for any principal that is not sysadmin.

The password never appears in the output: it is the sqlcmd variable $(RAG_READER_PASSWORD), set with
`:setvar` in SSMS (SQLCMD mode) or substituted in memory by whoever executes the script.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .catalog import Catalog, Table

PASSWORD_VAR = "$(RAG_READER_PASSWORD)"
SERVER_PERMISSIONS = (
    ("CONNECT ANY DATABASE", "every current and future database"),
    ("SELECT ALL USER SECURABLES", "read every user table and view in them"),
    ("VIEW ANY DEFINITION", "metadata for scripts/discover_schema.py"),
)


def quote(ident: str) -> str:
    """[bracket]-quote one identifier; a ] inside it is doubled."""
    return "[" + ident.replace("]", "]]") + "]"


def qualified(name: str) -> str:
    """'dbo.ExportOrder' -> '[dbo].[ExportOrder]'; a bare name is taken to be in dbo."""
    schema, _, obj = name.rpartition(".")
    return f"{quote(schema or 'dbo')}.{quote(obj)}"


def deny_statements(table: Table, login: str) -> list[str]:
    """DENY SELECT on a sensitive table (or view) as a whole, else on its sensitive columns only."""
    if table.sensitive:
        return [f"DENY SELECT ON OBJECT::{qualified(table.name)} TO {quote(login)};"]
    cols = [c["name"] for c in table.columns if c.get("sensitive")]
    if not cols:
        return []
    return [f"DENY SELECT ON OBJECT::{qualified(table.name)} ({', '.join(quote(c) for c in cols)}) "
            f"TO {quote(login)};"]


def nstr(text: str) -> str:
    """A T-SQL N'…' literal, ' doubled."""
    return "N'" + text.replace("'", "''") + "'"


def guarded_denies(table: Table, login: str) -> list[str]:
    """deny_statements(), but each one only if its object (and column) exists on the server it runs on — a
    discovery from one server may name objects another server lacks — and one DENY per column, so a missing
    column cannot cancel the DENY of the others."""
    obj = qualified(table.name)
    if table.sensitive:
        return [f"IF OBJECT_ID({nstr(obj)}) IS NOT NULL DENY SELECT ON OBJECT::{obj} TO {quote(login)};"]
    return [f"IF COL_LENGTH({nstr(obj)}, {nstr(c['name'])}) IS NOT NULL "
            f"DENY SELECT ON OBJECT::{obj} ({quote(c['name'])}) TO {quote(login)};"
            for c in table.columns if c.get("sensitive")]


def in_database(database: str, statements: list[str]) -> list[str]:
    """Run `statements` inside `database` only if it exists on this server; otherwise say it was skipped.
    `USE [missing]` would not stop the statements after it from running in the previous database."""
    body = "\n".join(statements)
    return [
        f"IF DB_ID({nstr(database)}) IS NULL",
        f"    PRINT {nstr(database + ': not on this server, skipped')};",
        "ELSE",
        f"    EXEC {quote(database)}.[sys].[sp_executesql] {nstr(chr(10) + body + chr(10))};",
        "GO",
        "",
    ]


def sensitive_counts(cat: Catalog) -> tuple[int, int]:
    """(tables denied whole, sensitive columns denied inside other objects)."""
    whole = sum(1 for t in cat.tables if t.sensitive)
    cols = sum(len([c for c in t.columns if c.get("sensitive")]) for t in cat.tables if not t.sensitive)
    return whole, cols


def render_grants(login: str, catalogs: dict[str, Catalog], *, now: Optional[datetime] = None) -> str:
    ts = (now or datetime.now()).isoformat(timespec="seconds")
    L = quote(login)
    lines = [
        f"-- Generated {ts} by scripts/gen_reader_grants.py — review, then run as a sysadmin in SQLCMD mode.",
        "-- One read-only login for the assistant on the whole instance. Idempotent: safe to re-run after a",
        "-- new discovery (new DENYs are added; existing ones are re-issued).",
        f'-- Password: :setvar RAG_READER_PASSWORD "..."   (never stored in this file, never in git)',
        f"-- Rollback: {login}_rollback.sql next to this file.",
        "",
        "USE [master];",
        f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'{login}')",
        f"    CREATE LOGIN {L} WITH PASSWORD = N'{PASSWORD_VAR}', CHECK_POLICY = ON, CHECK_EXPIRATION = OFF,",
        "        DEFAULT_DATABASE = [master];",
    ]
    width = max(len(p) for p, _ in SERVER_PERMISSIONS)
    lines += [f"GRANT {p:<{width}} TO {L};   -- {why}" for p, why in SERVER_PERMISSIONS]
    lines += [
        "-- Deliberately not granted: CONTROL SERVER, ALTER ANY *, IMPERSONATE, VIEW SERVER STATE, any write.",
        "GO",
        "",
    ]
    lines += [
        "-- Each database below is changed only if it exists on this server, and each DENY only if its object",
        "-- (and column) exists there: safe on any server that holds some of these databases.",
        "",
    ]
    for name, cat in catalogs.items():
        if cat.dialect != "tsql":
            continue
        stmts = [f"IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = {nstr(login)})",
                 f"    CREATE USER {L} FOR LOGIN {L};"]
        if not cat.tables:
            lines += [
                f"-- {name}: no discovery (config/catalog/discovered/{name}.json missing), so no DENYs yet.",
                f"-- Run `python scripts/discover_schema.py --json {name}` and regenerate before the first use.",
            ]
        else:
            whole, cols = sensitive_counts(cat)
            lines += [
                f"-- {name}: {whole} sensitive objects denied whole; {cols} sensitive columns denied inside other",
                f"-- objects (data/sensitive.py patterns, from config/catalog/discovered/{name}.json).",
            ]
            for t in sorted(cat.tables, key=lambda t: t.name.lower()):
                stmts += guarded_denies(t, login)
        lines += in_database(name, stmts)
    return "\n".join(lines) + "\n"


def render_rollback(login: str, catalogs: dict[str, Catalog]) -> str:
    L = quote(login)
    lines = [
        f"-- Removes the assistant's read-only login {login} and its database users. Run as a sysadmin.",
        "-- Stop the containers first (docker compose stop): DROP LOGIN fails while the login is connected.",
        "-- Order matters: database users first (their DENYs go with them), then the server permissions and the login.",
        "",
    ]
    for name, cat in catalogs.items():
        if cat.dialect != "tsql":
            continue
        lines += in_database(name, [f"IF EXISTS (SELECT 1 FROM sys.database_principals WHERE name = {nstr(login)})",
                                    f"    DROP USER {L};"])
    lines += ["USE [master];"]
    lines += [f"REVOKE {p} FROM {L};" for p, _ in SERVER_PERMISSIONS]
    lines += [
        f"IF EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'{login}')",
        f"    DROP LOGIN {L};",
        "GO",
    ]
    return "\n".join(lines) + "\n"
