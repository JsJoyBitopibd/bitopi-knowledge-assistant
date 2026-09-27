"""Turn a follow-up into a standalone question (small model, last N turns)."""
from __future__ import annotations

import re

from ..config import prompt, settings
from ..llm import get_small_chat

# A follow-up needs rewriting only when it leans on the previous turn: a pronoun or demonstrative, an
# ellipsis ("what about next month?"), or so few words it cannot stand alone. A self-contained question
# ("What is the PCD approval SOP?") is passed through untouched, saving a small-model call per turn.
_CONTEXT_DEP = re.compile(r"\b(it|its|it's|that|this|those|these|they|them|their|there|he|she|his|her|"
                          r"same|also|another|again|the (former|latter)|what about|how about|and (what|the))\b", re.I)


def _needs_rewrite(latest: str) -> bool:
    if _CONTEXT_DEP.search(latest) or re.match(r"^\s*(and|also|what about|how about|then|so)\b", latest, re.I):
        return True
    # A short question that fully matches a fixed tool ("What is the next PCD?", "TAL next PCD") already
    # carries everything the query needs; rewriting it only costs a model call (one took 27 s, 2026-09-27).
    from ..data.tools import load_fixed_tools, match_fixed_tool   # lazy: DB deps optional at import time
    if match_fixed_tool(latest, load_fixed_tools()):
        return False
    return len(re.findall(r"\w+", latest)) < 6


def standalone_question(latest: str, history: list[dict], user: str = "") -> str:
    """history: [{"role": "user"|"assistant", "text": str}, ...] oldest first."""
    if not history or not _needs_rewrite(latest):
        return latest
    n = int(settings()["answer.history_turns"])
    recent = history[-2 * n:]
    convo = "\n".join(f"{t['role']}: {t['text']}" for t in recent)
    reply = get_small_chat().chat([{"role": "user", "content": f"Conversation:\n{convo}\n\nLatest: {latest}"}],
                                  system=prompt("rewrite"), max_tokens=int(settings()["llm.max_tokens_rewrite"]),
                                  temperature=0, purpose="rewrite", user=user)
    text = reply.text.strip().strip('"')
    return text or latest
