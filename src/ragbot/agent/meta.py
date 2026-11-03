"""Messages about the conversation itself, read before anything is searched (Phase T7).

Real messages in logs/chat.csv that the assistant handled badly:
- "You showed me the BGL, not TAL" was classed as chitchat and got the fixed capability paragraph;
- "you haven't replied?" was rewritten into a data query;
- "which database access do you have?" and "hi" got the same fixed paragraph as everything social.

`classify` recognises these with patterns only, before any rewrite, so a three-word complaint is never turned into a
query. What each kind gets: a correction re-asks the previous question with the record the user meant; "no reply" /
"say that again" shows the previous answer again; a capability question is answered from the configuration
(agent/capabilities.py); a greeting gets a short reply from the small model, checked by `social_ok`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .state import ConversationState


@dataclass
class MetaTurn:
    kind: str                 # greeting | capability | wrong_entity | no_reply | repeat
    asked: str = ""           # wrong_entity: the record the user meant
    shown: str = ""           # wrong_entity: the record the previous answer covered
    topic: str = ""           # capability: what about ("inventory", "" = everything)


_GREETING = re.compile(r"^\s*(?:hi+|hello|hey|hiya|salam|assalamu?\s*[ao]laikum|good\s+(?:morning|afternoon|evening|day)|"
                       r"thanks?(?:\s+a\s+lot)?(?:\s+you)?|thank\s+you(?:\s+so\s+much)?|ok(?:ay)?(?:,?\s+thanks?(?:\s+you)?)?|"
                       r"great|nice|cool|who\s+are\s+you|what\s+are\s+you|what\s+can\s+you\s+do|how\s+are\s+you|help)"
                       r"(?:\s+(?:there|bot|assistant))?[\s!.?]*$", re.I)
_NO_REPLY = re.compile(r"\b(?:you\s+)?(?:haven'?t|have\s+not|didn'?t|did\s+not|did\s+n't)\s+(?:yet\s+)?"
                       r"(?:replied|reply|answered|answer|respond(?:ed)?)\b|\bno\s+(?:answer|reply|response)\b|"
                       r"^\s*\?+\s*$|\bstill\s+waiting\b|\bare\s+you\s+there\b", re.I)
_REPEAT = re.compile(r"^\s*(?:(?:can\s+you\s+|please\s+)?(?:show|say|repeat|give)\s+(?:me\s+)?(?:it|that|the\s+(?:last|"
                     r"previous)\s+(?:answer|reply|one))\s+again|again)\s*[?.!]*\s*$", re.I)
_WRONG = re.compile(r"\byou\s+(?:showed|show|gave|give|sent|answered|returned|are\s+showing)\b(?:\s+me)?\s+(?:the\s+)?"
                    r"(?P<shown>[\w-]+)?.*?\bnot\s+(?:the\s+)?(?P<asked>[\w-]+)"
                    r"|\bi\s+(?:asked|meant|wanted|said)\s+(?:for\s+|about\s+)?(?:the\s+)?(?P<asked2>[\w-]+)"
                    r"(?:,?\s+not\s+(?:the\s+)?(?P<shown2>[\w-]+))?", re.I)
_CAPABILITY = re.compile(
    r"\b(?:what|which)\s+(?:kind\s+of\s+|type\s+of\s+|sort\s+of\s+)?(?:information|info|datas?|databases?|access|"
    # Phase V: "Which modules and datas you have access?" (asked live on 3 Nov 2026) ran SQL on the PPC menu table
    r"questions?|reports?|things|modules?|areas?|systems?|sources?|subjects?|topics?)"
    r"(?:\s+(?:and|or|&)\s+(?:datas?|information|info|access|databases?|reports?))?"
    r"\b(?:.*?\b(?:from|about|on|in)\s+(?:the\s+)?(?P<topic>[a-z][\w /&-]*?)\s*[?.!]*$)?"
    r"|\bwhat\s+(?:are\s+you|is\s+this(?:\s+assistant)?)\s+connected\s+to\b"
    r"|^\s*(?:tell\s+me\s+)?(?:about|what\s+(?:is|about))\s+(?:the\s+)?(?P<topic2>inventory|hr|hrms|quality|production|"
    r"orders?|lcs?|finance|sourcing|stores?|vistaq|planning)\s*(?:data|database|module)?\s*[?.!]*$"
    r"|\bwhat\s+can\s+(?:i|you)\s+(?:ask|get|find|tell|show|answer)\b(?:.*?\b(?:from|about|on|in)\s+(?:the\s+)?"
    r"(?P<topic3>[a-z][\w /&-]*?)\s*[?.!]*$)?", re.I)
# a capability question is put to the assistant — "which data shows the PCD for TAL?" is a data question
_ADDRESSED = re.compile(r"\b(?:you|your|can\s+i|could\s+i|do\s+i|may\s+i|i\s+can|tell\s+me)\b"
                        r"|^\s*(?:about|what\s+(?:is|about))\b", re.I)


def _record(word: str) -> str:
    """The record a word names, upper-cased ("tal" -> TAL, "TAL-25-1493-215"), or '' when it names none."""
    if not word:
        return ""
    try:
        from ..data.entities import question_entities
        e = question_entities(word)
        if e.factories:
            return e.factories[0]
    except Exception:
        pass
    if re.fullmatch(r"[A-Za-z]{2,4}-\d{2,4}-\d{2,5}(?:-\d+)?|\d{3,5}", word):
        return word.upper()
    return ""


def classify(message: str, state: Optional[ConversationState]) -> Optional[MetaTurn]:
    text = (message or "").strip()
    if not text or len(text) > 200:
        return None
    has_last = state is not None and bool(state.last_request_id)
    if has_last and _NO_REPLY.search(text):
        return MetaTurn("no_reply")
    if has_last and _REPEAT.match(text):
        return MetaTurn("repeat")
    if has_last and state.last_question:
        m = _WRONG.search(text)
        if m:
            asked = _record(m.group("asked") or m.group("asked2") or "")
            shown = _record(m.group("shown") or m.group("shown2") or "")
            if asked and asked != shown:
                return MetaTurn("wrong_entity", asked=asked, shown=shown)
    m = _CAPABILITY.search(text)
    if m and len(text.split()) <= 16 and _ADDRESSED.search(text):
        topic = (m.group("topic") or m.group("topic2") or m.group("topic3") or "").strip()
        return MetaTurn("capability", topic=topic)
    if _GREETING.match(text):
        return MetaTurn("greeting")
    return None


def swap(question: str, asked: str, shown: str = "") -> Optional[str]:
    """The previous question with the record the user meant in place of the one it covered: the shown record when it
    is in the question, else the question's own factory; else the record is added. None when nothing changes."""
    q = question.strip()
    if not q or not asked:
        return None
    if shown and re.search(rf"\b{re.escape(shown)}\b", q, re.I):
        out = re.sub(rf"\b{re.escape(shown)}\b", asked, q, count=1, flags=re.I)
    else:
        try:
            from ..data.entities import question_entities
            facs = question_entities(q).factories
        except Exception:
            facs = []
        if facs and re.search(rf"\b{re.escape(facs[0])}\b", q, re.I):
            out = re.sub(rf"\b{re.escape(facs[0])}\b", asked, q, count=1, flags=re.I)
        else:
            end = q[-1] if q[-1] in "?.!" else ""
            out = f"{q[:-1] if end else q} for {asked}{end}"
    return out if out.lower() != q.lower() else None


_MARKER = re.compile(r"\[(?:P|D)\d+\]")
_SQLISH = re.compile(r"\b(?:select|from|where|join)\b.*\b(?:from|where|by)\b|\brag\.|\bdbo\.|\bvw_", re.I | re.S)


def social_ok(text: str, table_names: set[str]) -> bool:
    """A social reply may be shown only if it states no fact: no digit, no source marker, nothing that reads as SQL,
    no table or view name, and it is short. It is not checked against sources (it has none), so this is its check."""
    t = (text or "").strip()
    if not t or len(t) > 600 or re.search(r"\d", t) or _MARKER.search(t) or _SQLISH.search(t):
        return False
    low = t.lower()
    return not any(re.search(rf"\b{re.escape(n)}\b", low) for n in table_names)
