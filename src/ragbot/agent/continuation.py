"""Does this message continue the conversation, or start something new? (Phase T5)

Until v1.14.0 one rule decided whether a message was rewritten with the conversation (agent/rewrite.py): a pronoun,
or fewer than six words. It went wrong both ways, in logs/chat.csv:

- "b+ blood group", asked after a DHU answer, has three words, so it was merged into "Which lines are above the DHU
  target this week for b+ blood group" — a new topic glued onto the old one;
- "TAL line wise production summary of today with style showing" has no pronoun and eleven words, so it was never
  rewritten and never saw the previous turn at all.

Here the decision is made without a model, from the message, the previous question and the previous result's
columns. A follow-up is rewritten and its SQL is written knowing the previous turn (state.SqlContext); a new
question is answered on its own — no rewrite call, and no earlier turns in the answer prompt.
Refines and re-queries (agent/refine.py, data/requery.py) are decided before this runs.
"""
from __future__ import annotations

import re
from typing import Literal, Optional, Sequence

from .rewrite import _CONTEXT_DEP
from .state import ConversationState

Kind = Literal["follow_up", "new"]

_LEADING = re.compile(r"^\s*(?:and|also|what about|how about|then|so|but|same|instead|now)\b", re.I)
_STOP = frozenset("""
a an the of for to in on at by with from and or but is are was were be been am do does did have has had it its this
that these those there their them they we you your our my me i what which who whom whose when where why how show
give list tell find get please can could would will shall should may might any all each every some no not only just
also than then too very much many more most less few per wise about into over under after before between
today yesterday tomorrow week month year this next last data table rows row result results answer number total
""".split())


def _tokens(text: str) -> set[str]:
    """Content words of a text, with a column name's words ("ShipDate" -> ship, date) and singulars."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text or "")
    out = set()
    for w in re.findall(r"[a-z][a-z0-9]{2,}", text.lower()):
        if w in _STOP:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 4 else w)
    return out


def _names_records_only(message: str) -> bool:
    """A reply that is little more than a record name — "RHL?", "line 2102", "TAL-25-1493-215" — continues the
    previous question with that record in place of the old one."""
    if len(re.findall(r"\w+", message)) > 3:
        return False
    try:
        from ..data.entities import question_entities
        e = question_entities(message)
        return bool(e.factories or e.lines or e.units or e.styles or e.names_a_record)
    except Exception:
        return False


def classify(message: str, state: Optional[ConversationState], history: Sequence[dict]) -> Kind:
    """follow_up or new. Cues in this order: no history → new; a pasted statement → new; a pronoun or a leading
    "and / what about / instead …" → follow_up; a message that is only a record name → follow_up; a question a fixed
    tool fully answers → new; else shared content words with the previous question or its result's columns —
    one for a short message, two for a long one → follow_up; anything else → new."""
    if not history:
        return "new"
    if re.match(r"^\s*(select|with)\b", message, re.I):
        return "new"
    if _CONTEXT_DEP.search(message) or _LEADING.match(message):
        return "follow_up"
    if _names_records_only(message):
        return "follow_up"
    try:
        from ..data.tools import load_fixed_tools, match_fixed_tool
        if match_fixed_tool(message, load_fixed_tools()):
            return "new"
    except Exception:
        pass
    previous = ""
    if state is not None:
        previous = " ".join(x for x in (state.last_question, state.last_rewritten) if x)
        last = state.last_result()
        if last is not None:
            previous += " " + " ".join(last.columns)
    if not previous:
        previous = next((t.get("text", "") for t in reversed(history) if t.get("role") == "user"), "")
    words = len(re.findall(r"\w+", message))
    shared = _tokens(message) & _tokens(previous)
    return "follow_up" if len(shared) >= (1 if words < 6 else 2) else "new"
