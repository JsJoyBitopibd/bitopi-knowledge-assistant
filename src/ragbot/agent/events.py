"""Events yielded by orchestrator.answer_stream(), in the order a UI should handle them.

Stage   — a pipeline step started (show it as progress).
Token   — a piece of the answer text as the model writes it (append it to the visible draft).
Replace — the draft so far is void: verification failed and a stricter attempt follows, or the
          question is being retried against another source. Clear the draft.
Final   — the verified Answer. Its text is authoritative: render it in place of the streamed draft.
          Only a Final's answer may enter chat history or logs; streamed text never does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from ..models import Answer


@dataclass
class Stage:
    name: str      # understanding | searching | querying | searching+querying | writing
    label: str     # human-readable progress text
    # "writing" only: what the answer is being written from ("SOP Manual p. 12", "BitopiSplint: rag.vw_X"),
    # shown at once — the provider sends the answer in one burst, so this is what the user sees first (I3)
    sources: list[str] = field(default_factory=list)


@dataclass
class Token:
    text: str


@dataclass
class Replace:
    reason: str = ""


@dataclass
class Final:
    answer: Answer


Event = Union[Stage, Token, Replace, Final]
