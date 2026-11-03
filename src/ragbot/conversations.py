"""Conversations kept per user on the server (Phase U1): data/index/conversations.db.

Until v1.15.0 a conversation lived in the browser session: a reload, a sign-out or a closed tab lost it, and Recent
held at most ten. Here every answered question is written once, after it is shown, so a user finds their
conversations again on any reload and can rename or delete them. The rules:

- **Keyed by user.** Every read names the user in its SQL; another user's conversation id returns nothing. An
  administrator gets no cross-user read here (the audit panel keeps logs/chat.csv for that).
- **Rows of a restricted database are never stored.** A turn whose results come from a catalog with `restricted_to:`
  (the people databases, once armed) is stored without its rows, without row keys, and with its tables taken out of
  the text; on resume it shows the text and "Refresh data" reads them live, under the scope of the day.
- **Bounded.** Rows are capped at data.max_rows per result, a turn's results at 512 KB (else withheld as "size"), and
  `purge()` deletes conversations untouched for ui.conversations.retention_days (90) and the ones the user deleted.
- Not encrypted at rest (docs/DATA_ACCESS.md says so): the file sits with the index on the server.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Sequence

from .config import prompt, settings
from .models import QueryResult, Reference

_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS conversation (
    id TEXT PRIMARY KEY, user TEXT NOT NULL, scope_key TEXT NOT NULL, title TEXT NOT NULL,
    created TEXT NOT NULL, updated TEXT NOT NULL, deleted_at TEXT);
CREATE TABLE IF NOT EXISTS turn (
    id INTEGER PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversation(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, q TEXT, refs TEXT, results TEXT,
    meta TEXT NOT NULL, created TEXT NOT NULL, UNIQUE (conversation_id, idx));
CREATE INDEX IF NOT EXISTS conversation_user ON conversation (user, updated DESC);
"""
MAX_RESULTS_BYTES = 512 * 1024
_META_KEYS = ("follow_ups", "refine_suggestions", "request_id", "seconds", "not_found", "error_kind", "route", "kind",
              "scope", "clarify", "rewritten", "repeat_of", "prior_request_id", "page", "agent")


def db_path() -> Path:
    return settings().path("index_dir", "data/index") / "conversations.db"


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.executescript(_SCHEMA)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def restricted_databases() -> frozenset[str]:
    """The catalogs that name `restricted_to:` groups; their rows are never stored. Fails closed: if the catalogs
    cannot be read, every result is treated as restricted."""
    try:
        from .data.catalog import load_catalogs
        return frozenset(n for n, c in load_catalogs().items() if c.restricted_to)
    except Exception:
        return frozenset({"*"})


def _cell(v: Any) -> Any:
    """A cell as JSON that reads back to the same text (agent/templated.fmt): exact decimals become numbers,
    dates their ISO text, a time stamp 'YYYY-MM-DD HH:MM'."""
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral() else float(v)
    if isinstance(v, datetime):
        from .agent.templated import fmt
        return fmt(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return None
    return v


def _result_json(r: QueryResult, max_rows: int) -> dict[str, Any]:
    d = r.model_dump(mode="json", exclude={"lookups", "sql_executed", "rows"})
    d["rows"] = [[_cell(v) for v in row] for row in r.rows[:max_rows]]
    return d


def _without_tables(text: str) -> str:
    from .ui.view import split_md_tables
    parts = [p for kind, p in split_md_tables(text or "") if kind != "table" and str(p).strip()]
    return "\n\n".join(str(p).strip() for p in parts)


def _serialize(t: dict[str, Any], restricted: frozenset[str], max_rows: int) -> tuple:
    """(role, text, q, refs, results, meta) for one turn dict of app.py."""
    meta = {k: t[k] for k in _META_KEYS if k in t}
    results: list[QueryResult] = [r for r in (t.get("results") or []) if isinstance(r, QueryResult)]
    refs: list[Any] = list(t.get("refs") or [])
    text = str(t.get("text") or "")
    withheld = ""
    if results and ("*" in restricted or any(r.database in restricted for r in results)):
        withheld = "restricted"
    blob = None
    if results and not withheld:
        blob = json.dumps([_result_json(r, max_rows) for r in results], ensure_ascii=False, default=str)
        if len(blob.encode("utf-8")) > MAX_RESULTS_BYTES:
            withheld, blob = "size", None
    if withheld:
        meta["results_withheld"] = withheld
        if withheld == "restricted":
            text = _without_tables(text) + "\n\n" + prompt("stored_table_withheld").strip()
            refs = [r.model_copy(update={"row_keys": None}) if isinstance(r, Reference) else r for r in refs]
    ref_blob = json.dumps([r.model_dump(mode="json") if isinstance(r, Reference) else r for r in refs],
                          ensure_ascii=False, default=str) if refs else None
    return str(t.get("role") or ""), text, t.get("q"), ref_blob, blob, json.dumps(meta, ensure_ascii=False, default=str)


def save(cid: str, user: str, scope_key: str, title: str, turns: Sequence[dict[str, Any]], *, from_idx: int = 0,
         path: Optional[Path] = None) -> None:
    """Write turns from `from_idx` on (one call per answered question) and touch the conversation. The title is
    set when the conversation is first stored; after that only rename() changes it."""
    name = (user or "").strip()
    if not cid or not name:
        raise ValueError("a conversation is stored under its id and the user it belongs to")
    restricted = restricted_databases()
    max_rows = int(settings().get("data.max_rows", 200))
    con = _connect(path)
    try:
        with con:
            row = con.execute("SELECT user FROM conversation WHERE id = ?", (cid,)).fetchone()
            if row is not None and row[0] != name:
                raise PermissionError("that conversation belongs to someone else")
            now = _now()
            con.execute("INSERT INTO conversation (id, user, scope_key, title, created, updated) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET updated = excluded.updated, "
                        "scope_key = excluded.scope_key",
                        (cid, name, scope_key, (title or "Conversation")[:200], now, now))
            for i, t in enumerate(turns):
                if i < from_idx:
                    continue
                role, text, q, refs, results, meta = _serialize(t, restricted, max_rows)
                con.execute("INSERT INTO turn (conversation_id, idx, role, text, q, refs, results, meta, created) "
                            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(conversation_id, idx) DO UPDATE SET "
                            "role = excluded.role, text = excluded.text, q = excluded.q, refs = excluded.refs, "
                            "results = excluded.results, meta = excluded.meta",
                            (cid, i, role, text, q, refs, results, meta, now))
    finally:
        con.close()


def _read(path: Optional[Path]) -> Optional[sqlite3.Connection]:
    p = path or db_path()
    if not p.exists():
        return None
    return _connect(p)


def recent(user: str, limit: int = 20, path: Optional[Path] = None) -> list[dict[str, str]]:
    con = _read(path)
    if con is None:
        return []
    try:
        rows = con.execute("SELECT id, title, updated FROM conversation WHERE user = ? AND deleted_at IS NULL "
                           "ORDER BY updated DESC, rowid DESC LIMIT ?", ((user or "").strip(), int(limit))).fetchall()
        return [{"id": r[0], "title": r[1], "updated": r[2]} for r in rows]
    finally:
        con.close()


def load(cid: str, user: str, path: Optional[Path] = None) -> Optional[list[dict[str, Any]]]:
    """The turns of one of `user`'s conversations as app.py keeps them, or None (not theirs, deleted, missing)."""
    con = _read(path)
    if con is None:
        return None
    try:
        owner = con.execute("SELECT 1 FROM conversation WHERE id = ? AND user = ? AND deleted_at IS NULL",
                            (cid, (user or "").strip())).fetchone()
        if owner is None:
            return None
        out = []
        for role, text, q, refs, results, meta in con.execute(
                "SELECT role, text, q, refs, results, meta FROM turn WHERE conversation_id = ? ORDER BY idx", (cid,)):
            t: dict[str, Any] = {"role": role, "text": text}
            if role == "assistant":
                t.update(json.loads(meta or "{}"))
                t["q"] = q
                t["refs"] = [Reference.model_validate(r) for r in json.loads(refs)] if refs else []
                t["results"] = [QueryResult.model_validate(r) for r in json.loads(results)] if results else []
            out.append(t)
        return out
    finally:
        con.close()


def rename(cid: str, user: str, title: str, path: Optional[Path] = None) -> bool:
    con = _connect(path)
    try:
        with con:
            cur = con.execute("UPDATE conversation SET title = ? WHERE id = ? AND user = ? AND deleted_at IS NULL",
                              ((title or "").strip()[:200] or "Conversation", cid, (user or "").strip()))
        return cur.rowcount > 0
    finally:
        con.close()


def delete(cid: str, user: str, path: Optional[Path] = None) -> bool:
    """Gone from the user's list at once; the rows are removed by the next purge."""
    con = _connect(path)
    try:
        with con:
            cur = con.execute("UPDATE conversation SET deleted_at = ? WHERE id = ? AND user = ? AND deleted_at IS NULL",
                              (_now(), cid, (user or "").strip()))
        return cur.rowcount > 0
    finally:
        con.close()


def recent_questions(user: str, limit: int = 10, path: Optional[Path] = None) -> list[str]:
    """The user's own recent questions that got an answer, newest first, each once (case aside) — for the home
    page's suggestions (U3)."""
    con = _read(path)
    if con is None:
        return []
    try:
        rows = con.execute(
            "SELECT u.text FROM turn u JOIN turn a ON a.conversation_id = u.conversation_id AND a.idx = u.idx + 1 "
            "JOIN conversation c ON c.id = u.conversation_id "
            "WHERE c.user = ? AND c.deleted_at IS NULL AND u.role = 'user' AND a.role = 'assistant' "
            "AND json_extract(a.meta, '$.not_found') IS NOT 1 AND IFNULL(json_extract(a.meta, '$.kind'), '') "
            "NOT IN ('meta', 'social', 'clarify') ORDER BY u.created DESC, u.idx DESC LIMIT 200",
            ((user or "").strip(),)).fetchall()
    finally:
        con.close()
    seen, out = set(), []
    for (text,) in rows:
        key = " ".join(str(text).split()).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(" ".join(str(text).split()))
        if len(out) >= limit:
            break
    return out


def purge(retention_days: int, now: Optional[datetime] = None, path: Optional[Path] = None) -> int:
    """Delete conversations untouched for `retention_days` and those the user deleted; returns how many."""
    p = path or db_path()
    if not p.exists():
        return 0
    cutoff = ((now or datetime.now()) - timedelta(days=max(0, int(retention_days)))).isoformat(timespec="seconds")
    con = _connect(p)
    try:
        with con:
            cur = con.execute("DELETE FROM conversation WHERE deleted_at IS NOT NULL OR updated < ?", (cutoff,))
        return cur.rowcount
    finally:
        con.close()
