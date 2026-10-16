"""Stage 4 (Phase O2): copy-only drafts — a supplier escalation email, a production meeting agenda, a request for a
new delivery date, a QC investigation ticket, an LC document checklist — written from cited facts and stored,
never sent.

Signed off by the owner on 2026-10-16 ("if so, complete it", O1). Built like Stage 3: administrators only, and
nothing at all while `agents.drafts.enabled` is false (the default). The chat is unchanged — the router still
refuses "send", "approve", "update" and "delete", and a draft is asked for in its own panel, not in chat.

How a draft keeps the non-negotiables:
1. Facts first. The subject the person types goes through the normal answer path (orchestrator.answer), with the
   person's own scope, so the facts are exactly what that person may see, already cited and already verified
   against the sources ([P#] chunks, [D#] rows). No facts, no draft.
2. The model only writes. prompts/draft_system.txt + prompts/drafts/<kind>.txt, with the verified answer as
   <facts>: it may cite only that answer's markers and copy only its numbers and codes.
3. Checked after writing (verify_draft): every marker is one of the answer's, at least one is used, and every
   number and code in the draft already appears in the verified answer. One stricter retry, then no draft.
4. Stored, never sent: data/index/drafts.db, append-only. There is no mail, webhook or ticket client anywhere in
   the code (tests/test_drafts.py scans for one).
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from ..auth.models import Scope
from ..config import prompt, settings
from ..models import Answer
from .citations import _CODE, _DIGIT_RUN, _MARK, _NUM, _norm_num, normalize_markers

KINDS: dict[str, str] = {
    "supplier_escalation": "Supplier escalation email",
    "meeting_agenda": "Production meeting agenda",
    "delivery_date_request": "Request for a new delivery date",
    "qc_investigation": "QC investigation ticket",
    "lc_checklist": "LC document checklist",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS draft (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created TEXT NOT NULL,
  user TEXT NOT NULL,
  kind TEXT NOT NULL,
  subject TEXT NOT NULL,
  text TEXT NOT NULL,
  refs TEXT NOT NULL,
  request_id TEXT NOT NULL DEFAULT ''
);
CREATE TRIGGER IF NOT EXISTS draft_no_update BEFORE UPDATE ON draft
  BEGIN SELECT RAISE(ABORT, 'draft is append-only: write a new draft'); END;
CREATE TRIGGER IF NOT EXISTS draft_no_delete BEFORE DELETE ON draft
  BEGIN SELECT RAISE(ABORT, 'draft is append-only'); END;
"""


def enabled(get: Callable[[str, Any], Any], scope: Optional[Scope]) -> bool:
    """True only when `agents.drafts.enabled` is a real true AND the viewer is an administrator."""
    try:
        on = get("agents.drafts.enabled", False) is True
    except Exception:
        return False
    return bool(on and scope is not None and getattr(scope, "is_admin", False))


@dataclass
class Draft:
    kind: str
    subject: str
    text: str = ""
    ok: bool = False
    problems: list[str] = field(default_factory=list)
    references: list[Any] = field(default_factory=list)
    facts: Optional[Answer] = None
    id: Optional[int] = None


def _pool(facts: Answer) -> tuple[set[str], set[str], str]:
    """(markers, numbers, upper-case text) the draft may use: those of the verified answer and its references."""
    text = normalize_markers(facts.text or "")
    markers = {r.marker for r in facts.references} & set(_MARK.findall(text)) or {r.marker for r in facts.references}
    # what the reference cards show a reader; never the SQL (its TOP (200) would license 200)
    shown = ("quote", "row_keys", "params", "row_count", "page", "source", "title", "section", "views")
    extra = " ".join(str(getattr(r, k)) for r in facts.references for k in shown if getattr(r, k, None) is not None)
    pool = text + " " + extra
    nums = {_norm_num(x) for x in _NUM.findall(pool)} | set(_DIGIT_RUN.findall(pool))
    return markers, nums, pool.upper()


def verify_draft(text: str, facts: Answer) -> tuple[bool, list[str]]:
    """Every marker is one of the facts' markers (at least one used); every number and code in the draft already
    appears in the verified answer or its references. Placeholders in [square brackets] are not markers."""
    text = normalize_markers(text or "")
    markers, nums, up = _pool(facts)
    problems: list[str] = []
    used = set(_MARK.findall(text))
    if not used:
        problems.append("no source marker in the draft")
    bad = used - markers
    if bad:
        problems.append(f"markers not in the facts: {sorted(bad)}")
    for n in _NUM.findall(text):
        v = _norm_num(n)
        if v and v not in nums:
            problems.append(f"number not in the facts: {n}")
    for code in _CODE.findall(text):
        if code.upper() not in up:
            problems.append(f"code not in the facts: {code}")
    return not problems, problems


def _facts_block(facts: Answer) -> str:
    refs = []
    for r in facts.references:
        if r.kind == "pdf":
            refs.append(f"[{r.marker}] {r.source}, page {r.page}: {r.quote or ''}")
        else:
            refs.append(f"[{r.marker}] {r.database}: {', '.join(r.views or [])}")
    return f"<facts>\n{facts.text}\n\nSources:\n" + "\n".join(refs) + "\n</facts>"


def make(kind: str, subject: str, *, user: str, scope: Scope,
         answer_fn: Optional[Callable[..., Answer]] = None, model: Any = None,
         path: Optional[Path] = None, store: bool = True) -> Draft:
    """Draft `kind` about `subject` for `user` within `scope`. Returns a Draft with ok False and the reasons when
    there are no facts or the written draft cannot be verified; stores it when ok."""
    if kind not in KINDS:
        return Draft(kind, subject, problems=[f"unknown draft kind {kind!r}; one of {', '.join(KINDS)}"])
    subject = (subject or "").strip()
    if not subject:
        return Draft(kind, subject, problems=["say what the draft is about (an order, PO, file, line or LC)"])
    if answer_fn is None:
        from .orchestrator import answer as answer_fn
    facts = answer_fn(subject, user=user, scope=scope)
    if facts.error_kind:
        return Draft(kind, subject, facts=facts, problems=[f"the facts could not be read ({facts.error_kind})"])
    if facts.not_found or not facts.references:
        return Draft(kind, subject, facts=facts,
                     problems=["no cited facts for this subject, so nothing to draft from: " + (facts.text or "")])
    if model is None:
        from ..llm import get_model
        model = get_model("draft")
    system = prompt("draft_system")
    ask = (f"{_facts_block(facts)}\n\nWrite: {KINDS[kind]}.\n{prompt('drafts/' + kind)}\n"
           f"It is about: {subject}")
    problems: list[str] = []
    text = ""
    for attempt in (1, 2):
        content = ask if attempt == 1 else (ask + "\n\nYour previous draft was refused: " + "; ".join(problems)
                                           + ". Use only markers and numbers that appear in <facts>; put anything "
                                             "else in [square-bracket placeholders].")
        reply = model.chat([{"role": "user", "content": content}], system=system, max_tokens=900, temperature=0.0,
                           purpose="draft", sent_rows=sum(len(r.rows) for r in facts.results), user=user)
        text = (reply.text or "").strip()
        ok, problems = verify_draft(text, facts)
        if ok:
            d = Draft(kind, subject, text=normalize_markers(text), ok=True, references=list(facts.references),
                      facts=facts)
            if store:
                d.id = write_draft(d, user, request_id=facts.request_id, path=path)
            return d
    return Draft(kind, subject, problems=["the draft could not be verified against its facts: " + "; ".join(problems)],
                 references=list(facts.references), facts=facts)


# ---------------------------------------------------------------- storage (append-only)
def db_path() -> Path:
    return settings().path("index_dir") / "drafts.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    return con


def write_draft(d: Draft, user: str, *, request_id: str = "", path: Optional[Path] = None) -> int:
    name = (user or "").strip()
    if not name:
        raise ValueError("a draft is stored under the user who asked for it")
    con = _connect(path or db_path())
    try:
        with con:
            refs = json.dumps([r.model_dump() if hasattr(r, "model_dump") else r for r in d.references],
                              ensure_ascii=False, default=str)
            cur = con.execute("INSERT INTO draft (created, user, kind, subject, text, refs, request_id) "
                              "VALUES (?,?,?,?,?,?,?)", (datetime.now().isoformat(timespec="seconds"), name, d.kind,
                                                         d.subject, d.text, refs, request_id or ""))
            return int(cur.lastrowid)
    finally:
        con.close()


def stored(user: str, *, limit: int = 20, path: Optional[Path] = None) -> list[dict[str, Any]]:
    """The user's own drafts, newest first."""
    p = path or db_path()
    if not p.exists():
        return []
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT id, created, kind, subject, text, refs FROM draft WHERE user = ? "
                           "ORDER BY id DESC LIMIT ?", (user, limit)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [dict(id=r[0], created=r[1], kind=r[2], subject=r[3], text=r[4], refs=json.loads(r[5])) for r in rows]


_PLACEHOLDER = re.compile(r"\[(?![PD]\d+\])[^\[\]]+\]")


def placeholders(text: str) -> list[str]:
    """The [square-bracket] blanks a person still has to fill in, in order, each once."""
    return list(dict.fromkeys(_PLACEHOLDER.findall(text or "")))
