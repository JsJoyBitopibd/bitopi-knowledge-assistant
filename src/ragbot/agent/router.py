"""Classify a question: documents | data | both | chitchat | refuse."""
from __future__ import annotations

import re

from ..config import prompt, settings
from ..llm import get_small_chat

LABELS = {"documents", "data", "both", "chitchat", "refuse"}
# Only the imperative form is refused without a model call ("please update…", "can you cancel…").
# "Can you tell me the password change policy?" must NOT match.
_WRITE = re.compile(r"^\s*(please|kindly|can you|could you|would you)\s+(please\s+)?"
                    r"(update|change|set|approve|cancel|delete|remove|send|email|create|insert|modify)\b", re.I)
_DATA_HINT = re.compile(r"\b(EO\s*\d{2}-\d{4}|[A-Z]{2,4}-\d{2}-\d{2,5}(-\d+)?|FR-\d{2}-\d+|file\s*ref|lot\s*\d+|"
                        r"how many\b.{0,40}?\b(orders?|export orders?|meetings?|ppm meetings?)\b|"
                        r"which orders|status of (an? )?(eo|order|file|export order)|stock|quantity|qty|pcd for|"
                        r"ship date for)\b", re.I)
# A strong data signal is a concrete order/file identifier or a database-only noun. When one of these
# appears and no document noun does, the question is unambiguously a database query and needs no model
# call. Deliberately narrower than _DATA_HINT (which includes bare "how many").
_DATA_STRONG = re.compile(r"\b(EO\s*\d{2}-\d{4}|[A-Z]{2,4}-\d{2}-\d{2,5}(-\d+)?|FR-\d{2}-\d+|file\s*ref|lot\s*\d+|"
                          r"export orders?|pcd|ship date|ppm meeting|order status|status of (an? )?"
                          r"(eo|order|file|export order))\b", re.I)
# Policy/IT nouns that appear in count-phrased questions the PDFs answer ("how many Kaspersky licences
# does the Group have?", "how many laptops per employee?"). Without these, the router used to send such
# questions to the database (the 83%->85% correctness gap).
_DOC_HINT = re.compile(r"\b(sop|policy|procedure|who approves|rule|comment sheet|tech pack|buyer (asked|comment)|manual|"
                       r"form\s+[A-Z]{2,}-\d+|licen[cs]es?|laptops?|desktops?|mailboxes?|servers?|endpoints?|"
                       r"backups?|retention|working days|rto|rpo|recovery time|"
                       r"passwords?|privileged|it polic(?:y|ies)|it standards?)\b", re.I)


# A count/list request ("how many …", "list …", "which …") in a database's own vocabulary — the
# catalogs' hand-written `keywords:` (GRN, voucher, attendance, recipe …) — is a data question and needs
# no routing call (Phase J4: ~1.4 s saved on about half the questions). Document nouns still win.
_LIST_CUE = re.compile(r"^\s*(how many|how much|count|number of|total|list|show|which|give me|"
                       r"what (is|are|was|were) the (total|number|count|list|latest|last|current))\b", re.I)
# keywords too common in document questions to decide on their own
_GENERIC = {"file", "unit", "size", "color", "colour", "ship", "line", "lines", "plan", "card", "device", "user",
            "users", "menu", "task", "tasks"}


def _keyword_data(question: str) -> bool:
    if not _LIST_CUE.search(question):
        return False
    try:
        from ..data.catalog import load_catalogs   # lazy: the catalogs are optional for document-only pilots
        cats = load_catalogs()
    except Exception:
        return False
    q = question.lower()
    for cat in cats.values():
        for w in cat.keywords:
            w = w.lower()
            # 3+ letters: 'grn', 'tna', 'pcd' are specific; two-letter codes ('po', 'ot') are not
            if len(w) >= 3 and w not in _GENERIC and re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", q):
                return True
    return False


def route(question: str, user: str = "") -> str:
    """Cheap regex first for the obvious cases; small model only when the signals are mixed or absent."""
    if _WRITE.search(question):
        return "refuse"
    d, p = bool(_DATA_HINT.search(question)), bool(_DOC_HINT.search(question))
    if d and p:
        return "both"
    # Unambiguous single-signal questions skip the LLM call (free-tier budget, ~1.3 s each).
    if _DATA_STRONG.search(question) and not p:
        return "data"
    if p and not d:
        return "documents"
    if _keyword_data(question):
        return "data"
    max_tokens = int(settings().get("llm.max_tokens_route", 64))
    reply = get_small_chat().chat([{"role": "user", "content": question}], system=prompt("router"),
                                  max_tokens=max_tokens, temperature=0, purpose="route", user=user)
    m = re.search(r"[a-z]+", reply.text.strip().lower())
    label = m.group(0) if m else ""
    if label in LABELS:
        return label
    return "data" if d else "documents"
