"""The "Try asking" chips on the home page, for this user (Phase U3).

They were five fixed strings from settings (`ui.suggestions`) for everyone. Now, in this order and each once:

1. up to two questions the user asked before and got an answer to (ragbot/conversations.py `recent_questions`);
2. up to two fixed-tool example questions over databases the user may read (agent/capabilities.examples), with
   another factory's code replaced by the user's own factory when their scope allows it — each one still has to
   reach its tool, so a click is answered without a model call;
3. the configured `ui.suggestions` to fill the rest.

Pure: no Streamlit, no database; the caller hands in what it has.
"""
from __future__ import annotations

import re
from typing import Callable, Optional, Sequence

_FACTORY = re.compile(r"\b(TAL|RHL|BGL|KTL|CKDL)\b")


def localize(example: str, factory: Optional[str], still_matches: Callable[[str], bool]) -> str:
    """The example with its factory code changed to the user's, when that still reaches a fixed tool; else as is."""
    if not factory or not _FACTORY.search(example) or factory in _FACTORY.findall(example):
        return example
    mine = _FACTORY.sub(factory, example, count=1)
    return mine if still_matches(mine) else example


def home_suggestions(*, own_recent: Sequence[str] = (), examples: Sequence[str] = (), factory: Optional[str] = None,
                     fallback: Sequence[str] = (), still_matches: Callable[[str], bool] = lambda q: True,
                     limit: int = 6) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def add(q: str) -> None:
        key = " ".join(str(q).split()).lower()
        if key and key not in seen and len(out) < limit:
            seen.add(key)
            out.append(" ".join(str(q).split()))

    for q in list(own_recent)[:2]:
        add(q)
    picked = 0
    for ex in examples:
        if picked >= 2:
            break
        before = len(out)
        add(localize(ex, factory, still_matches))
        picked += len(out) > before
    for q in fallback:
        add(q)
    return out
