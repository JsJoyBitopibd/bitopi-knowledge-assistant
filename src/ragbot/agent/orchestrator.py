"""The answer path (CLAUDE.md §"How the answer path works"). One public function: answer()."""
from __future__ import annotations

import csv
import re
from datetime import datetime
from typing import Any, Optional

from ..config import log_dir, prompt, settings
from ..data.errors import friendly_llm
from ..llm import get_chat
from ..llm.base import LLMError
from ..models import Answer, Chunk, QueryResult, Usage
from ..retrieve.retriever import retrieve
from .citations import normalize_markers, sources_block, verify
from .rewrite import standalone_question
from .router import _DOC_HINT, route

_MARK = re.compile(r"\[([PD]\d+)\]")


def _route(q: str, user: str) -> str:
    """A fixed tool matching the question is proof it is a database query, so skip the routing LLM call
    (unless a document noun is also present, where the model should still decide documents-vs-both)."""
    from ..data.tools import load_fixed_tools, match_fixed_tool  # lazy: DB deps optional at import time
    if match_fixed_tool(q, load_fixed_tools()) and not _DOC_HINT.search(q):
        return "data"
    return route(q, user)


def answer(question: str, history: Optional[list[dict]] = None, where: Optional[dict[str, Any]] = None,
           user: str = "") -> Answer:
    """Public entry point. Translates a provider failure (quota, timeout, auth) into a soft answer the
    UI can show instead of a traceback; the raw cause stays in warnings and the call log."""
    try:
        return _answer(question, history, where, user)
    except LLMError as e:
        out = Answer(text=friendly_llm(e.kind), question=question, error_kind=e.kind, not_found=True)
        out.warnings.append(f"llm {e.kind}: {e}")
        return _log(out, user)


def _answer(question: str, history: Optional[list[dict]] = None, where: Optional[dict[str, Any]] = None,
            user: str = "") -> Answer:
    from ..index_version import refresh_if_changed
    refresh_if_changed()   # pick up a background ingest without a restart (cheap: one stat)
    s = settings()
    nf = s["answer.not_found_text"]
    usage = Usage()
    history = history or []

    q = standalone_question(question, history, user) if history else question
    r = _route(q, user)
    out = Answer(text="", question=question, rewritten_question=q, route=r, usage=usage)

    if r == "refuse":
        out.text = prompt("refusal").strip(); return _log(out, user)
    if r == "chitchat":
        out.text = prompt("chitchat").strip()
        return _log(out, user)

    chunks: list[Chunk] = []
    results: list[QueryResult] = []
    if r in ("documents", "both"):
        chunks = retrieve(q, where=where)[: int(s["llm.max_chunks_to_llm"])]
    if r in ("data", "both"):
        from ..data.tools import answer_from_data  # lazy: DB drivers optional at import time
        results = [x for x in answer_from_data(q, user)]
        # a failed query is not a source: never let the model cite an error message
        out.warnings += [f"data {x.database}: {x.error}" for x in results if x.error]
        results = [x for x in results if not x.error]
        if r == "data" and not any(x.rows for x in results) and not chunks:
            # data route found nothing usable: fall back to documents once (a PDF may hold it)
            chunks = retrieve(q, where=where)[: int(s["llm.max_chunks_to_llm"])]

    if not chunks and not any(x.rows for x in results):
        out.text = nf + " No document or database view in the system covers this question."
        out.not_found = True
        return _log(out, user)

    _attempt_answer(out, question, q, history, chunks, results, user, s, nf, usage)

    # A "data" question that the rows could not answer may still be answerable from the documents —
    # the router mistakes policy questions phrased as counts ("how many licences does the Group
    # have?") for database questions, and the SQL then returns rows that are real but irrelevant, so
    # the earlier no-rows fallback never fires. Retry once against the documents before giving up.
    if out.not_found and r == "data" and not chunks:
        doc_chunks = retrieve(q, where=where)[: int(s["llm.max_chunks_to_llm"])]
        if doc_chunks:
            out.warnings.append("data route returned no answer; retried against documents")
            retry = Answer(text="", question=question, rewritten_question=q, route=r, usage=usage)
            retry.warnings = out.warnings
            _attempt_answer(retry, question, q, history, doc_chunks, [], user, s, nf, usage)
            if not retry.not_found:
                return _log(retry, user)
            out = retry

    if out.not_found and not out.text:
        out.text = nf + " The retrieved sources did not support a verifiable answer."
        _log_verify_failure(question, q, out.warnings)
    return _log(out, user)


def _attempt_answer(out: Answer, question: str, q: str, history: list[dict], chunks: list[Chunk],
                    results: list[QueryResult], user: str, s: Any, nf: str, usage: Usage) -> None:
    """Assemble the <sources> block, generate, verify, and fill `out` in place. Two attempts: the
    second uses the stricter prompt. Leaves out.not_found=True with no text if both attempts fail."""
    block, refs = sources_block(chunks, results, int(s["data.max_rows_to_llm"]))
    out.sources_text = block
    hist = "\n".join(f"{t['role']}: {t['text']}" for t in history[-2 * int(s["answer.history_turns"]):])
    user_msg = ((f"Earlier in this conversation:\n{hist}\n\n" if hist else "") + block +
                f"\n\nQuestion: {question}" + (f"\n(searched as: {q})" if q != question else "") +
                "\n\nAnswer from the sources only and cite each fact with its [P#]/[D#] id.")
    chat = get_chat()
    system = prompt("system_answer")
    chunk_ids = [c.id for c in chunks]
    rows_sent = sum(min(len(x.rows), int(s["data.max_rows_to_llm"])) for x in results)

    for attempt in range(2):
        reply = chat.chat([{"role": "user", "content": user_msg}], system=system,
                          max_tokens=int(s["llm.max_tokens_answer"]), temperature=0, purpose="answer",
                          sent_chunk_ids=chunk_ids, sent_rows=rows_sent, user=user)
        usage.input_tokens += reply.input_tokens; usage.output_tokens += reply.output_tokens
        usage.model = reply.model; usage.calls += 1
        text = normalize_markers(reply.text.strip())
        if not text:
            out.warnings.append(f"attempt {attempt + 1}: empty reply (stop_reason={reply.stop_reason})")
            continue
        ok, problems = verify(text, chunks, results, nf)
        if ok:
            out.not_found = text.startswith(nf)
            if out.not_found:
                # docs/REFERENCE_FORMAT.md: a not-found reply carries no markers and no reference
                # list. verify() short-circuits on not-found, so strip any stray marker the model
                # appended rather than leaving one that resolves to nothing.
                out.text = re.sub(r"\s{2,}", " ", _MARK.sub("", text)).strip()
                out.references = []
            else:
                used = set(_MARK.findall(text))
                out.text = text
                out.references = [x for x in refs if x.marker in used]
            return
        out.warnings += [f"attempt {attempt + 1}: {p}" for p in problems]
        system = prompt("system_answer_strict")

    out.not_found = True
    out.text = ""


def log_feedback(user: str, index: int, thumb: Any, answer_text: str) -> None:
    """Thumbs up/down from the UI go to logs/chat.csv as a feedback row (docs/PLAN.md M5)."""
    f = log_dir() / "chat.csv"
    new = not f.exists()
    with open(f, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(_CHAT_HEADER)
        w.writerow([datetime.now().isoformat(timespec="seconds"), user, "feedback", f"turn {index}", "", "",
                    "", "", "", f"thumb={thumb} | {answer_text[:200]}"])


_CHAT_HEADER = ["ts", "user", "route", "question", "rewritten", "not_found", "refs", "in_tokens", "out_tokens",
                "warnings", "error_kind"]


def _log(a: Answer, user: str) -> Answer:
    f = log_dir() / "chat.csv"
    new = not f.exists()
    with open(f, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(_CHAT_HEADER)
        w.writerow([datetime.now().isoformat(timespec="seconds"), user, a.route, a.question, a.rewritten_question,
                    a.not_found, ";".join(r.marker + ":" + (r.source or r.database or "") for r in a.references),
                    a.usage.input_tokens, a.usage.output_tokens, " | ".join(a.warnings), a.error_kind])
    return a


def _log_verify_failure(question: str, rewritten: str, problems: list[str]) -> None:
    with open(log_dir() / "verify_failures.csv", "a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow([datetime.now().isoformat(timespec="seconds"), question, rewritten, " | ".join(problems)])
