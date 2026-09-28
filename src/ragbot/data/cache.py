"""Short-TTL cache of SQL results (docs/ROADMAP.md B5).

The cache sits AFTER the guard: the key is the exact statement that ran (virtual views expanded) plus
its parameters, so a cached result can only ever be one the guard already approved. A hit returns
the rows together with the time they were originally read, so the [D#] reference's "as of" stays
honest. `refresh=True` bypasses the cache (the UI's Refresh data button). Hits are logged to
logs/sql.csv with tool "cache:<tool>" and 0 ms.

Setting: data.result_cache_ttl_seconds (default 180; 0 disables). The key includes the user's scope
(PRD FR-4): today the statement already differs per scope (the factory filter is in its text), but
with the DBA's session-context views the text is the same for everyone, and a cached result must
never cross scopes.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from ..config import settings
from ..ttl_cache import TTLCache
from .connectors import _log as _log_sql
from .connectors import run

_CACHE = TTLCache(maxsize=256)


def cached_run(engine: str, sql_exec: str, params: dict[str, Any], *, conn_env: str, display_sql: str,
               tool: str = "generated", user: str = "", refresh: bool = False,
               timeout: Optional[int] = None, scope_key: str = "") -> tuple[list[str], list[list[Any]], datetime]:
    """connectors.run() behind the cache. Returns (columns, rows, as_of)."""
    ttl = int(settings().get("data.result_cache_ttl_seconds", 180))
    key = (engine, conn_env, sql_exec, json.dumps(params, default=str, sort_keys=True), scope_key)
    if ttl > 0 and not refresh:
        hit = _CACHE.get(key)
        if hit is not None:
            cols, rows, as_of = hit
            _log_sql(engine, f"cache:{tool}", display_sql, sql_exec, params, len(rows), 0, user=user, scope=scope_key)
            return list(cols), [list(r) for r in rows], as_of
    kw: dict[str, Any] = {"conn_env": conn_env, "display_sql": display_sql, "tool": tool, "user": user,
                          "scope": scope_key}
    if timeout:
        kw["timeout"] = timeout
    cols, rows = run(engine, sql_exec, params, **kw)
    as_of = datetime.now()
    _CACHE.put(key, (tuple(cols), tuple(tuple(r) for r in rows), as_of), ttl)
    return cols, rows, as_of


def clear() -> None:
    _CACHE.clear()
