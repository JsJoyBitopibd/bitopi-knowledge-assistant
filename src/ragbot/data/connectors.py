"""Read-only connectors for SQL Server (pyodbc) and MySQL (mysql-connector). One function each:
run(sql, params) -> (columns, rows). Timeout 10 s. Every statement logged to logs/sql.csv.

Non-negotiable #7 ("nothing is written to any production database, ever"): every connection is
opened with autocommit off, and every code path — success or failure — ends in an explicit
ROLLBACK before the connection closes. `readonly=True` on the pyodbc connection is a driver hint,
not a guarantee, so the guard (src/ragbot/data/guard.py) plus this rollback discipline are the real
protection, not any single layer alone.
"""
from __future__ import annotations

import csv
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from ..config import env, log_dir, settings


def _log(engine: str, tool: str, display_sql: str, sql_exec: str, params: dict, rows: int, ms: float,
        error: str = "", user: str = "") -> None:
    f = log_dir() / "sql.csv"
    new = not f.exists()
    with open(f, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts", "user", "engine", "tool", "rows", "ms", "error", "params", "sql", "sql_exec"])
        w.writerow([datetime.now().isoformat(timespec="seconds"), user, engine, tool, rows, f"{ms:.0f}", error,
                    params, display_sql, sql_exec])


def _positional(sql: str, marker: str) -> tuple[str, list[str]]:
    """Replace `marker+name` placeholders with positional `?`/`%(name)s`-style markers, but only
    outside single-quoted string literals — an email address or free-text containing '@' or ':' in a
    row value must never be treated as a parameter. A lookbehind also excludes a marker preceded by
    itself, so T-SQL globals like @@VERSION / @@ROWCOUNT are never mistaken for a named parameter."""
    pat = re.compile(f"(?<!{re.escape(marker)})" + re.escape(marker) + r"(\w+)")
    parts = sql.split("'")  # even indices are outside quotes, odd indices are inside
    names: list[str] = []

    def repl(m: re.Match) -> str:
        names.append(m.group(1))
        return "?"

    for i in range(0, len(parts), 2):
        parts[i] = pat.sub(repl, parts[i])
    return "'".join(parts), names


def run_sqlserver(sql: str, params: dict[str, Any], *, conn_env: str = "SQLSERVER_CONN", display_sql: str = "",
                  tool: str = "generated", user: str = "") -> tuple[list[str], list[list[Any]]]:
    import pyodbc
    timeout = int(settings()["data.timeout_seconds"])
    q, names = _positional(sql, "@")
    args = [params[n] for n in names]
    t0 = time.perf_counter()
    cn = None
    try:
        cn = pyodbc.connect(env(conn_env), timeout=timeout, autocommit=False, readonly=True)
        cn.timeout = timeout
        cur = cn.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")
        cur.execute(q, args)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchall()] if cur.description else []
        _log("sqlserver", tool, display_sql or sql, sql, params, len(rows), (time.perf_counter() - t0) * 1000, user=user)
        return cols, rows
    except Exception as e:
        _log("sqlserver", tool, display_sql or sql, sql, params, 0, (time.perf_counter() - t0) * 1000,
             error=str(e)[:300], user=user)
        raise
    finally:
        if cn is not None:
            try:
                cn.rollback()  # never commit — SELECT needs nothing, and this is the write barrier of last resort
            finally:
                cn.close()


def run_mysql(sql: str, params: dict[str, Any], *, conn_env: str = "MYSQL_CONN", display_sql: str = "",
             tool: str = "generated", user: str = "") -> tuple[list[str], list[list[Any]]]:
    import mysql.connector
    timeout = int(settings()["data.timeout_seconds"])
    u = urlparse(env(conn_env))  # mysql://user:pwd@host:3306/rag
    q, names = _positional(sql, ":")
    # mysql-connector uses %s positionally when paramstyle is 'format'; build args in occurrence order
    args = [params[n] for n in names]
    q = re.sub(r"\?", "%s", q)
    t0 = time.perf_counter()
    cn = None
    try:
        cn = mysql.connector.connect(host=u.hostname, port=u.port or 3306, user=u.username, password=u.password,
                                     database=u.path.lstrip("/"), connection_timeout=timeout, autocommit=False)
        cur = cn.cursor()
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.execute(f"SET SESSION MAX_EXECUTION_TIME={timeout * 1000}")
        cur.execute(q, args)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchall()] if cur.description else []
        _log("mysql", tool, display_sql or sql, sql, params, len(rows), (time.perf_counter() - t0) * 1000, user=user)
        return cols, rows
    except Exception as e:
        _log("mysql", tool, display_sql or sql, sql, params, 0, (time.perf_counter() - t0) * 1000,
             error=str(e)[:300], user=user)
        raise
    finally:
        if cn is not None:
            try:
                cn.rollback()
            finally:
                cn.close()


def run(engine: str, sql: str, params: dict[str, Any], **kw) -> tuple[list[str], list[list[Any]]]:
    return (run_sqlserver if engine == "sqlserver" else run_mysql)(sql, params, **kw)
