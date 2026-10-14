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
_LIST_CUE = re.compile(r"^\s*(?:(?:please|kindly|pls|can you|could you)\s+)*(how many|how much|count|number of|total|list|"
                       r"show|which|give me|find|get|fetch|"
                       r"what (is|are|was|were) the (total|number|count|list|latest|last|current))\b", re.I)
# A pasted SQL statement is a database question whatever words it holds (P1: "ORDER BY" used to send one to the
# order agent and another database); data/tools.py runs it as written, under the guard, against the catalog that
# offers its tables.
_STATEMENT = re.compile(r"^\s*(select|with)\b", re.I)
# keywords too common in document questions to decide on their own
_GENERIC = {"file", "unit", "size", "color", "colour", "ship", "line", "lines", "plan", "card", "device", "user",
            "users", "menu", "task", "tasks"}


def _keyword_data(question: str) -> bool:
    if not _LIST_CUE.search(question):
        return False
    return catalog_vocabulary_hit(question)


def catalog_vocabulary_hit(question: str) -> bool:
    """Does the question use a word one of the routable databases' catalogs claims (`keywords:`)?"""
    try:
        from ..data.catalog import load_catalogs   # lazy: the catalogs are optional for document-only pilots
        cats = load_catalogs()
    except Exception:
        return False
    q = question.lower()
    for cat in cats.values():
        if not cat.routable:      # a prototype catalog's vocabulary must not route a question (Phase Q)
            continue
        for w in cat.keywords:
            w = w.lower()
            # 3+ letters: 'grn', 'tna', 'pcd' are specific; two-letter codes ('po', 'ot') are not
            if len(w) >= 3 and w not in _GENERIC and re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", q):
                return True
    return False


# Phase Q: a line or unit number, or "which style is running on …", is a database question whatever else the
# sentence holds — no document in data/pdfs records which style a sewing line runs today. Document nouns still
# win (they are checked first in route()). Deliberately narrow: "style" or "buyer" alone is not a cue.
_LINE_CUE = re.compile(r"\blines?\s*(?:no\.?|number|#)?\s*[-:]?\s*\d{2,4}\b|\bL-\d{2,4}\b|\b\d{3,4}\s+lines?\b"
                       r"|\bunits?\s*(?:no\.?|#)?\s*[-:]?\s*\d{1,2}\b", re.I)
_RUNNING_CUE = re.compile(r"\b(?:what|which|whose)\b.{0,40}\b(?:styles?|buyers?|orders?)\b.{0,40}\brunning\b"
                          r"|\brunning\b.{0,20}\b(?:on|in|at)\b.{0,20}\blines?\b", re.I)
# Phase R: the overtime rate is the HRMS's own slab (Hrms5Misami dbo.OTSlab), which the router prompt already
# files under data. Left to the model, "What is the overtime rate for cutting staff?" was routed documents on
# 13 Oct and data on 14 Oct from a byte-identical prompt — a coin flip between "no data" and an answer.
# "Overtime policy" and "who approves overtime" still go to documents: _DOC_HINT is checked first.
_PAY_RATE_CUE = re.compile(r"\b(?:overtime|ot|eot)\s+(?:rates?|slabs?)\b", re.I)


def _factory_with_vocabulary(question: str) -> bool:
    """A factory code plus a word one of the databases owns ("TAL DHU today"). A factory alone is not a data
    question: "What is the TAL holiday policy?" is answered from a PDF."""
    try:
        from ..data.entities import question_entities
        return bool(question_entities(question).factories) and catalog_vocabulary_hit(question)
    except Exception:
        return False


def route(question: str, user: str = "") -> str:
    """Cheap regex first for the obvious cases; small model only when the signals are mixed or absent."""
    if _WRITE.search(question):
        return "refuse"
    if _STATEMENT.search(question):
        return "data"
    d, p = bool(_DATA_HINT.search(question)), bool(_DOC_HINT.search(question))
    if d and p:
        return "both"
    # Unambiguous single-signal questions skip the LLM call (free-tier budget, ~1.3 s each).
    if _DATA_STRONG.search(question) and not p:
        return "data"
    if p and not d:
        return "documents"
    if not p and (_LINE_CUE.search(question) or _RUNNING_CUE.search(question) or _PAY_RATE_CUE.search(question)
                  or _factory_with_vocabulary(question)):
        return "data"            # Phase Q: a named line or "which style is running" needs no routing call
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
