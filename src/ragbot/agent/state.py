"""What the conversation so far hands to the next question (Phase T1).

Until v1.14.0 the orchestrator saw the history as text only: the rows, SQL and parameters of the previous answer
stayed in the UI, so "sort that by date", "only TAL" or "line wise?" became brand-new questions. A
`ConversationState` carries the last few data results across that boundary, read-only and bounded, so a follow-up
can reshape the rows already read (agent/refine.py), re-run the previous query with a change (data/tools.requery)
or give the SQL model a short summary of the previous turn (SqlContext) — never the rows themselves to the model.

It is built from the turn dicts app.py already keeps (`from_history`), and has no Streamlit import, so scripts and
the eval harness build it the same way.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from pydantic import BaseModel, Field

from ..models import QueryResult


class PriorTurn(BaseModel):
    """One earlier answer that returned rows."""
    question: str                       # what the user typed
    rewritten: str = ""                 # what was searched (Answer.rewritten_question)
    route: str = ""
    text: str = ""                      # the verified answer text
    results: list[QueryResult] = Field(default_factory=list)   # Answer.results, [D1], [D2] … order
    request_id: str = ""
    page: int = 0                       # which page of its query this turn showed (T4 paging)


class ConversationState(BaseModel):
    conversation_id: str = ""
    turns: list[PriorTurn] = Field(default_factory=list)     # oldest first; data turns with rows only
    # the very last assistant turn, whatever it was (documents, not-found, clarify): what a meta reply
    # ("you haven't replied?", "you showed me BGL, not TAL") or a clarification answer refers to
    last_question: str = ""
    last_rewritten: str = ""
    last_route: str = ""
    last_request_id: str = ""
    last_not_found: bool = False
    pending_clarification: Optional[dict[str, Any]] = None   # set when the last turn asked a question (T6)

    @classmethod
    def from_history(cls, history: Sequence[dict], conversation_id: str = "", limit: int = 3) -> "ConversationState":
        """history: the app's turn dicts, oldest first (user turns {role, text}; assistant turns also carry q,
        results, route, request_id, rewritten, not_found, clarify). Keeps the newest `limit` assistant turns whose
        results hold rows; an errored or denied result is never kept."""
        turns: list[PriorTurn] = []
        last: Optional[dict] = None
        for t in history or ():
            if not isinstance(t, dict) or t.get("role") != "assistant":
                continue
            last = t
            if t.get("error_kind") or t.get("not_found"):
                continue
            results = [r for r in (t.get("results") or [])
                       if isinstance(r, QueryResult) and r.rows and not r.error and not r.denied]
            if not results:
                continue
            turns.append(PriorTurn(question=str(t.get("q") or ""), rewritten=str(t.get("rewritten") or ""),
                                   route=str(t.get("route") or ""), text=str(t.get("text") or ""),
                                   results=results, request_id=str(t.get("request_id") or ""),
                                   page=int(t.get("page") or 0)))
        state = cls(conversation_id=conversation_id, turns=turns[-limit:] if limit > 0 else [])
        if last is not None:
            state.last_question = str(last.get("q") or "")
            state.last_rewritten = str(last.get("rewritten") or "")
            state.last_route = str(last.get("route") or "")
            state.last_request_id = str(last.get("request_id") or "")
            state.last_not_found = bool(last.get("not_found"))
            clar = last.get("clarify")
            if last.get("route") == "clarify" and isinstance(clar, dict):
                state.pending_clarification = {**clar, "question": state.last_question}
        return state

    @property
    def last(self) -> Optional[PriorTurn]:
        return self.turns[-1] if self.turns else None

    def last_result(self) -> Optional[QueryResult]:
        """The result a follow-up acts on: the first result with rows of the newest data turn — but only when that
        turn is also the conversation's last answer. After a documents answer or a not-found, "sort by date" has
        nothing of its own to sort, and reaching back past it would act on rows the user is no longer looking at."""
        t = self.last
        if t is None or (self.last_request_id and t.request_id != self.last_request_id):
            return None
        return next((r for r in t.results if r.rows and r.tool != "lookup"), None)

    def key(self) -> str:
        """Joins cache keys that depend on this conversation (a modified statement, a follow-up's SQL context)."""
        return f"{self.conversation_id}:{self.last.request_id if self.last else ''}"
