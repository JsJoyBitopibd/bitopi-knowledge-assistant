"""Phase O2: copy-only drafts (agent/drafts.py) — off by default and for non-administrators, written only from the
verified answer's facts, checked after writing, stored append-only, never sent; and no mail, webhook or ticket
client anywhere in the code."""
import re
import sqlite3

import pytest
import yaml

from ragbot.agent import drafts
from ragbot.auth.models import Scope
from ragbot.config import ROOT
from ragbot.llm.base import ChatReply
from ragbot.models import Answer, Reference

ADMIN = Scope(factories=frozenset(["*"]), is_admin=True)
STAFF = Scope(factories=frozenset(["*"]))


def _get(value):
    return lambda key, default=None: value if key == "agents.drafts.enabled" else default


def _facts(**kw):
    ref = Reference(marker="D1", kind="data", database="Inventory", views=["rag.vw_StockBalance"],
                    row_keys=["PO=4500123"], row_count=1)
    base = dict(text="PO 4500123 for 500 kg of thread was due on 2026-10-01 and 120 kg arrived [D1].",
                references=[ref], route="data", request_id="abc123")
    base.update(kw)
    return Answer(**base)


class FakeModel:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, system="", **kw):
        self.calls.append((messages, system, kw))
        return ChatReply(text=self.replies.pop(0))


GOOD = ("Subject: Late delivery of PO 4500123\n\nPO 4500123 for 500 kg of thread was due on 2026-10-01; only 120 kg "
        "has arrived [D1]. Please confirm the remaining quantity by [deadline].\n\n[your name], [your title]")


# ---------------------------------------------------------------- the gate
def test_off_by_default_and_for_everyone_but_administrators():
    d = yaml.safe_load((ROOT / "config" / "settings.example.yaml").read_text(encoding="utf-8"))
    assert d["agents"]["drafts"]["enabled"] is False
    assert not drafts.enabled(_get(False), ADMIN)
    assert not drafts.enabled(_get("true"), ADMIN)
    assert not drafts.enabled(_get(True), STAFF)
    assert not drafts.enabled(_get(True), None)
    assert drafts.enabled(_get(True), ADMIN)


# ---------------------------------------------------------------- checking a written draft
def test_a_draft_that_copies_the_facts_passes():
    assert drafts.verify_draft(GOOD, _facts()) == (True, [])


@pytest.mark.parametrize("text, problem", [
    ("PO 4500123 was due on 2026-10-01 [D2].", "markers not in the facts"),
    ("PO 4500123 was due on 2026-10-01.", "no source marker"),
    ("PO 4500123: 380 kg are still missing [D1].", "number not in the facts: 380"),     # the model computed
    ("PO 4500999 was due on 2026-10-01 [D1].", "number not in the facts: 4500999"),
])
def test_a_draft_with_anything_not_in_the_facts_is_refused(text, problem):
    ok, problems = drafts.verify_draft(text, _facts())
    assert not ok and any(problem in p for p in problems), problems


def test_placeholders_are_listed_and_markers_are_not():
    assert drafts.placeholders(GOOD) == ["[deadline]", "[your name]", "[your title]"]


# ---------------------------------------------------------------- making one
def test_make_writes_from_the_facts_and_stores_the_draft(tmp_path):
    model = FakeModel(GOOD)
    seen = {}

    def answer_fn(q, user, scope):
        seen.update(q=q, user=user, scope=scope)
        return _facts()
    d = drafts.make("supplier_escalation", "PO 4500123", user="j.hossain", scope=ADMIN, answer_fn=answer_fn,
                    model=model, path=tmp_path / "drafts.db")
    assert d.ok and d.id == 1 and seen == {"q": "PO 4500123", "user": "j.hossain", "scope": ADMIN}
    messages, system, kw = model.calls[0]
    assert kw["purpose"] == "draft" and kw["temperature"] == 0.0
    assert "never send" in system and "<facts>" in messages[0]["content"]
    [row] = drafts.stored("j.hossain", path=tmp_path / "drafts.db")
    assert row["kind"] == "supplier_escalation" and row["text"] == GOOD and row["refs"][0]["marker"] == "D1"
    assert drafts.stored("someone.else", path=tmp_path / "drafts.db") == []


def test_one_retry_then_no_draft(tmp_path):
    model = FakeModel("380 kg are missing [D1].", "Still 380 kg [D1].")
    d = drafts.make("supplier_escalation", "PO 4500123", user="u", scope=ADMIN, answer_fn=lambda q, **k: _facts(),
                    model=model, path=tmp_path / "drafts.db")
    assert not d.ok and len(model.calls) == 2 and "could not be verified" in d.problems[0]
    assert "previous draft was refused" in model.calls[1][0][0]["content"]
    assert drafts.stored("u", path=tmp_path / "drafts.db") == []


def test_no_facts_no_draft_and_no_model_call(tmp_path):
    model = FakeModel()
    for facts in (_facts(not_found=True, text="System doesn't have the data.", references=[]),
                  _facts(error_kind="quota")):
        d = drafts.make("meeting_agenda", "line 2101", user="u", scope=ADMIN, answer_fn=lambda q, f=facts, **k: f,
                        model=model, path=tmp_path / "drafts.db")
        assert not d.ok and d.problems
    assert model.calls == []
    assert not drafts.make("nonsense", "x", user="u", scope=ADMIN, model=model).ok
    assert not drafts.make("lc_checklist", "  ", user="u", scope=ADMIN, model=model).ok


def test_the_draft_store_is_append_only(tmp_path):
    db = tmp_path / "drafts.db"
    d = drafts.make("supplier_escalation", "PO 4500123", user="u", scope=ADMIN, answer_fn=lambda q, **k: _facts(),
                    model=FakeModel(GOOD), path=db)
    con = sqlite3.connect(db)
    for sql in ("UPDATE draft SET text = 'x' WHERE id = ?", "DELETE FROM draft WHERE id = ?"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute(sql, (d.id,))
    con.close()


# ---------------------------------------------------------------- never sent
def test_no_mail_webhook_or_ticket_client_in_the_code():
    """Stage 4 drafts are copied by a person, never sent by the assistant (ROADMAP Phase O gate)."""
    banned = re.compile(r"^\s*(import|from)\s+(smtplib|email\.mime|slack_sdk|jira|exchangelib|win32com|"
                        r"sendgrid|twilio|msal|O365)\b", re.M)
    post = re.compile(r"requests\.(post|put|patch|delete)\(|httpx\.(post|put|patch|delete)\(|\.sendmail\(")
    offenders = []
    for f in sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "scripts").glob("*.py")):
        text = f.read_text(encoding="utf-8")
        if banned.search(text) or post.search(text):
            offenders.append(f.relative_to(ROOT).as_posix())
    assert offenders == []


def test_the_app_reaches_drafts_only_behind_the_admin_switch():
    text = (ROOT / "src" / "ragbot" / "app.py").read_text(encoding="utf-8")
    head, _, body = text.partition("if _drafts.enabled(s.get, scope):")
    assert body and "_drafts.make(" not in head
