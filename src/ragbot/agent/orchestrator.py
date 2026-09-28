"""The answer path (CLAUDE.md §"How the answer path works").

Two public entry points over one pipeline:
- answer_stream() yields events (agent/events.py): progress stages, answer tokens as the model writes
  them, a Replace when a streamed draft fails verification, and the verified Answer last.
- answer() drains the stream and returns the verified Answer (scripts/eval.py, scripts/ask.py).

Streaming never weakens verification: the draft a user watches is provisional until Final, and
only the Final answer (verified by citations.verify) enters history and logs/chat.csv.
"""
from __future__ import annotations

import csv
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Iterator, Optional

from ..auth.models import Scope
from ..config import log_dir, prompt, settings
from ..data.errors import friendly_llm
from ..llm import get_chat
from ..logs import append_row
from ..llm.base import ChatReply, LLMError
from ..models import Answer, Chunk, QueryResult, Usage
from ..retrieve.retriever import retrieve
from ..ttl_cache import TTLCache
from .citations import normalize_markers, sources_block, verify
from .events import Event, Final, Replace, Stage, Token
from .rewrite import standalone_question
from .router import _DOC_HINT, route

_MARK = re.compile(r"\[([PD]\d+)\]")
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="answer")


def _route(q: str, user: str) -> str:
    """A fixed tool matching the question is proof it is a database query, so skip the routing LLM call
    (unless a document noun is also present, where the model should still decide documents-vs-both)."""
    from ..data.tools import load_fixed_tools, match_fixed_tool  # lazy: DB deps optional at import time
    if match_fixed_tool(q, load_fixed_tools()) and not _DOC_HINT.search(q):
        return "data"
    return route(q, user)


_ANSWERS = TTLCache(maxsize=512)


def _answer_key(question: str, where: Optional[dict[str, Any]], scope: Scope) -> tuple:
    """Documents-answer cache key: the normalized question, the retrieval filter, the user's scope (a
    user never receives an answer built from sources outside their scope, PRD FR-4.5) and the index
    version, so any ingest makes every earlier entry unreachable."""
    from ..index_version import index_version
    norm = re.sub(r"\s+", " ", question.strip().lower()).rstrip("?!. ")
    return norm, json.dumps(where or {}, sort_keys=True, default=str), scope.key(), index_version()


def answer(question: str, history: Optional[list[dict]] = None, where: Optional[dict[str, Any]] = None,
           user: str = "", refresh: bool = False, *, scope: Scope) -> Answer:
    """Blocking entry point: the verified Answer from answer_stream()."""
    final: Optional[Answer] = None
    for ev in answer_stream(question, history, where, user, refresh, scope=scope):
        if isinstance(ev, Final):
            final = ev.answer
    assert final is not None, "answer_stream() must end with a Final event"
    return final


def answer_stream(question: str, history: Optional[list[dict]] = None, where: Optional[dict[str, Any]] = None,
                  user: str = "", refresh: bool = False, *, scope: Scope) -> Iterator[Event]:
    """Streaming entry point. Always ends with exactly one Final. A provider failure (quota, timeout,
    auth) becomes a soft answer the UI can show instead of a traceback; the raw cause stays in
    warnings and the call log. refresh=True reads the database live (skips the SQL result cache).
    scope: what this user may see (PRD FR-4); required, so no call can forget it."""
    drafting = False
    try:
        for ev in _answer_stream(question, history, where, user, refresh, scope):
            if isinstance(ev, Token):
                drafting = True
            elif isinstance(ev, Replace):
                drafting = False
            yield ev
    except LLMError as e:
        if drafting:
            yield Replace("provider error")
        out = Answer(text=friendly_llm(e.kind), question=question, error_kind=e.kind, not_found=True)
        out.warnings.append(f"llm {e.kind}: {e}")
        yield Final(_log(out, user, scope))


def _answer_stream(question: str, history: Optional[list[dict]], where: Optional[dict[str, Any]],
                   user: str, refresh: bool, scope: Scope) -> Iterator[Event]:
    from ..index_version import refresh_if_changed
    refresh_if_changed()   # pick up a background ingest without a restart (cheap: one stat)
    s = settings()
    nf = s["answer.not_found_text"]
    usage = Usage()
    history = history or []
    top = int(s["llm.max_chunks_to_llm"])

    # Repeat documents question: serve the earlier verified answer, no model call. Only a
    # self-contained question (no history) can hit, because a follow-up's meaning depends on context.
    ttl = int(s.get("answer.cache_ttl_seconds", 3600))
    key = _answer_key(question, where, scope) if not history and ttl > 0 else None
    if key is not None:
        hit = _ANSWERS.get(key)
        if hit is not None:
            a = hit.model_copy(deep=True)
            a.usage = Usage()
            a.warnings = ["answer cache hit"]
            yield Final(_log(a, user, scope)); return

    yield Stage("understanding", "Understanding the question…")
    q = standalone_question(question, history, user) if history else question
    r = _route(q, user)
    out = Answer(text="", question=question, rewritten_question=q, route=r, usage=usage)

    if r == "refuse":
        out.text = prompt("refusal").strip()
        yield Final(_log(out, user, scope)); return
    if r == "chitchat":
        out.text = prompt("chitchat").strip()
        yield Final(_log(out, user, scope)); return

    chunks: list[Chunk] = []
    results: list[QueryResult] = []
    denied = False                 # a fixed tool refused a factory outside the user's scope
    if r == "data":
        from ..data.tools import needs_clarification
        ask = needs_clarification(q)
        if ask:   # a missing order id / factory: ask instead of guessing (config/clarify.yaml)
            out.text, out.route = ask, "clarify"
            yield Final(_log(out, user, scope)); return
    if r in ("data", "both"):
        from ..data.tools import answer_from_data  # lazy: DB drivers optional at import time
    if r == "both":
        # Independent sources: search the documents while the database query (and its SQL-generation
        # call) runs, instead of one after the other. A worker's exception re-raises in .result().
        yield Stage("searching+querying", "Searching documents and querying the database…")
        docs_f = _POOL.submit(retrieve, q, where=where, scope=scope)
        data_f = _POOL.submit(answer_from_data, q, user, refresh, scope=scope)
        chunks = docs_f.result()[:top]
        results = list(data_f.result())
    elif r == "documents":
        yield Stage("searching", "Searching documents…")
        chunks = retrieve(q, where=where, scope=scope)[:top]
    elif r == "data":
        yield Stage("querying", "Querying the database…")
        results = list(answer_from_data(q, user, refresh, scope=scope))
    if r in ("data", "both"):
        # a failed query is not a source: never let the model cite an error message
        out.warnings += [f"data {x.database}: {x.error}" for x in results if x.error]
        denied = any(x.denied for x in results)
        results = [x for x in results if not x.error]
        if r == "data" and denied and not any(x.rows for x in results):
            # The question names a factory outside the user's scope: say so now. A document search and a
            # model call cannot answer it, and would only hide the reason behind a generic not-found.
            out.text, out.not_found = nf + " " + prompt("scope_denied").strip(), True
            yield Final(_log(out, user, scope)); return
        if r == "data" and not any(x.rows for x in results) and not chunks:
            # data route found nothing usable: fall back to documents once (a PDF may hold it)
            yield Stage("searching", "Searching documents…")
            chunks = retrieve(q, where=where, scope=scope)[:top]

    if not chunks and not any(x.rows for x in results):
        out.text = nf + " " + (prompt("scope_denied").strip() if denied
                               else "No document or database view in the system covers this question.")
        out.not_found = True
        yield Final(_log(out, user, scope)); return

    # A single fixed-tool result has a known shape: write it from a template, no model call (C7).
    if r == "data" and not chunks and len(results) == 1 and s.get("answer.templated", True):
        if _templated_answer(out, results[0], nf):
            yield Final(_log(out, user, scope)); return

    yield Stage("writing", "Writing the answer…")
    yield from _attempt_stream(out, question, q, history, chunks, results, user, s, nf, usage)

    # A "data" question that the rows could not answer may still be answerable from the documents —
    # the router mistakes policy questions phrased as counts ("how many licences does the Group
    # have?") for database questions, and the SQL then returns rows that are real but irrelevant, so
    # the earlier no-rows fallback never fires. Retry once against the documents before giving up.
    if out.not_found and r == "data" and not chunks:
        yield Replace("retrying against documents")   # clear the not-found draft before the search starts
        yield Stage("searching", "Searching documents…")
        doc_chunks = retrieve(q, where=where, scope=scope)[:top]
        if doc_chunks:
            out.warnings.append("data route returned no answer; retried against documents")
            retry = Answer(text="", question=question, rewritten_question=q, route=r, usage=usage)
            retry.warnings = out.warnings
            yield Stage("writing", "Writing the answer…")
            yield from _attempt_stream(retry, question, q, history, doc_chunks, [], user, s, nf, usage)
            if not retry.not_found:
                yield Final(_log(retry, user, scope)); return
            out = retry

    if out.not_found and not out.text:
        out.text = nf + " The retrieved sources did not support a verifiable answer."
        _log_verify_failure(question, q, out.warnings)
    # Cache only verified documents answers: database rows go stale in minutes (they have their own
    # short cache in data/cache.py) and a not-found may become answerable after the next ingest.
    if key is not None and r == "documents" and not out.not_found and not out.error_kind:
        _ANSWERS.put(key, out.model_copy(deep=True), ttl)
    yield Final(_log(out, user, scope))


def _templated_answer(out: Answer, result: QueryResult, nf: str) -> bool:
    """Fill `out` from agent/templated.py when the result is a fixed tool's and the text verifies."""
    from ..data.tools import load_fixed_tools
    from .templated import try_template
    tool = next((t for t in load_fixed_tools() if t.get("name") == result.tool), None)
    text = try_template(result, tool)
    if not text:
        return False
    block, refs = sources_block([], [result])
    ok, problems = verify(text, [], [result], nf)
    if not ok:
        out.warnings.append(f"template failed verification, using the model: {problems}")
        return False
    out.text, out.sources_text, out.not_found = text, block, False
    out.results = [result]
    out.references = [x for x in refs if x.marker in set(_MARK.findall(text))]
    out.follow_ups = _suggest([result], out.question)
    out.warnings.append("templated answer (no model call)")
    return True


def _suggest(results: list[QueryResult], question: str) -> list[str]:
    """Follow-up questions for the first fixed-tool result (config/fixed_tools.yaml `follow_ups:`)."""
    from ..data.tools import load_fixed_tools
    from .templated import follow_ups
    by_name = {t.get("name"): t for t in load_fixed_tools()}
    for r in results:
        if r.tool in by_name:
            return follow_ups(r, by_name[r.tool], question)
    return []


def _attempt_stream(out: Answer, question: str, q: str, history: list[dict], chunks: list[Chunk],
                    results: list[QueryResult], user: str, s: Any, nf: str, usage: Usage) -> Iterator[Event]:
    """Assemble the <sources> block, stream the model's answer as Tokens, verify it, and fill `out`
    in place. Two attempts: a draft that fails verification is voided with Replace and the second
    attempt uses the stricter prompt. Leaves out.not_found=True with no text if both attempts fail."""
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
        reply: Optional[ChatReply] = None
        drafted = False
        for item in chat.stream([{"role": "user", "content": user_msg}], system=system,
                                max_tokens=int(s["llm.max_tokens_answer"]), temperature=0, purpose="answer",
                                sent_chunk_ids=chunk_ids, sent_rows=rows_sent, user=user):
            if isinstance(item, ChatReply):
                reply = item
            elif item:
                drafted = True
                yield Token(item)
        assert reply is not None
        usage.input_tokens += reply.input_tokens; usage.output_tokens += reply.output_tokens
        usage.model = reply.model; usage.calls += 1
        text = normalize_markers(reply.text.strip())
        if not text:
            out.warnings.append(f"attempt {attempt + 1}: empty reply (stop_reason={reply.stop_reason})")
            if drafted:
                yield Replace("empty reply")
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
                out.results = list(results)   # index i is [D{i+1}], matching sources_block
                out.follow_ups = _suggest(results, question)
            return
        out.warnings += [f"attempt {attempt + 1}: {p}" for p in problems]
        # Record every voided draft: if replaces exceed ~10% of answers, the plan (docs/ROADMAP.md B2)
        # is to verify first and only then reveal the text.
        _log_verify_failure(question, q, [f"streamed draft replaced (attempt {attempt + 1})", *problems])
        yield Replace("verification failed")
        system = prompt("system_answer_strict")

    out.not_found = True
    out.text = ""


def log_feedback(user: str, index: int, thumb: Any, answer_text: str, scope: str = "") -> None:
    """Thumbs up/down from the UI go to logs/chat.csv as a feedback row (docs/PLAN.md M5)."""
    append_row("chat.csv", _CHAT_HEADER, [datetime.now().isoformat(timespec="seconds"), user, scope, "feedback",
                                          f"turn {index}", "", "", "", "", "", f"thumb={thumb} | {answer_text[:200]}", ""])


# `scope` (PRD FR-6.2): what the user was allowed to see when they asked, so an administrator can
# reconstruct what each user saw (FR-4.8).
_CHAT_HEADER = ["ts", "user", "scope", "route", "question", "rewritten", "not_found", "refs", "in_tokens",
                "out_tokens", "warnings", "error_kind"]


def _log(a: Answer, user: str, scope: Scope) -> Answer:
    append_row("chat.csv", _CHAT_HEADER, [
        datetime.now().isoformat(timespec="seconds"), user, scope.key(), a.route, a.question, a.rewritten_question,
        a.not_found, ";".join(r.marker + ":" + (r.source or r.database or "") for r in a.references),
        a.usage.input_tokens, a.usage.output_tokens, " | ".join(a.warnings), a.error_kind])
    return a


def _log_verify_failure(question: str, rewritten: str, problems: list[str]) -> None:
    with open(log_dir() / "verify_failures.csv", "a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow([datetime.now().isoformat(timespec="seconds"), question, rewritten, " | ".join(problems)])
