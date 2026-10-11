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

from .. import trace
from . import not_found
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
from .templated import is_blank_result

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
    scope: what this user may see (PRD FR-4); required, so no call can forget it.
    Each call is one request (ragbot/trace.py): the Final answer carries its request_id and timings."""
    drafting = False
    _, ctx = trace.start()
    try:
        for ev in trace.run_steps(ctx, _answer_stream(question, history, where, user, refresh, scope)):
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
        yield Final(ctx.run(_log, out, user, scope))


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
    q = question
    if history:
        with trace.span("rewrite"):
            q = standalone_question(question, history, user)
    with trace.span("route"):
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
        from ..data import empty_retry
        from ..data.tools import answer_from_data  # lazy: DB drivers optional at import time
    if r == "both":
        # Independent sources: search the documents while the database query (and its SQL-generation
        # call) runs, instead of one after the other. A worker's exception re-raises in .result().
        # No empty-result repair here (P2): a document search already runs, and the 4 cores are shared.
        yield Stage("searching+querying", "Searching documents and querying the database…")
        docs_f = trace.submit(_POOL, retrieve, q, where=where, scope=scope)
        with empty_retry.allowed(False):
            data_f = trace.submit(_POOL, answer_from_data, q, user, refresh, scope=scope)
        chunks = docs_f.result()[:top]
        results = list(data_f.result())
    elif r == "documents":
        yield Stage("searching", "Searching documents…")
        chunks = retrieve(q, where=where, scope=scope)[:top]
    elif r == "data":
        yield Stage("querying", "Querying the database…")
        with empty_retry.allowed(True):
            results = list(answer_from_data(q, user, refresh, scope=scope))
    if r in ("data", "both"):
        out.agent = next((x.agent for x in results if x.agent), "")   # domain agent (Phase K), for the log
        # which catalogs the SQL model was shown, kept on the answer so a not-found row in chat.csv says it too
        out.databases_considered = list(dict.fromkeys(d for x in results for d in x.databases_considered))
        # a failed query is not a source: never let the model cite an error message
        out.warnings += [f"data {x.database}: {x.error}" for x in results if x.error]
        denied = any(x.denied for x in results)
        # Read before the errored results are dropped (Phase Q). A fixed tool that timed out is still proof
        # that this is a database question — it matched a tool's pattern — and the error itself is what the
        # user needs to be told. Until 2026-10-11 `_tool_answered` was evaluated after the drop, so a 30 s
        # timeout was followed by a 10 s document search and an answer-model call, 45 s to say nothing.
        failed = [x for x in results if x.error and not x.denied]
        tool_ran = _tool_answered(results)
        checked = any(x.checked for x in results)
        results = [x for x in results if not x.error]
        if r == "data" and denied and not _has_rows(results):
            # The question names a factory outside the user's scope: say so now. A document search and a
            # model call cannot answer it, and would only hide the reason behind a generic not-found.
            out.text, out.not_found = nf + " " + prompt("scope_denied").strip(), True
            yield Final(_log(out, user, scope)); return
        if r == "data" and failed and not _has_rows(results) \
                and (tool_ran or checked or _names_a_record(q)):
            # The query failed (a timeout, a type conversion). The PDFs hold no production rows, so a document
            # search and an answer-model call would only hide the reason behind a generic not-found: say what
            # happened and offer Refresh data. `error_kind` stays empty on purpose — the UI renders an
            # error_kind turn as a warning box without the action row, which would take Refresh and the chips
            # away (app.py answer_body).
            out.text = nf + " " + (not_found.failed(failed) or not_found.general(q))
            out.not_found = True
            out.follow_ups = not_found.suggestions(q, results)
            yield Final(_log(out, user, scope)); return
        if r == "data" and not _has_rows(results) and not chunks and not tool_ran and not checked:
            # data route found nothing usable: fall back to documents once (a PDF may hold it). Not when a fixed
            # tool or a pasted statement ran: the question is a data question by construction (it matched a tool's
            # pattern), and the PDFs hold no stock balances — the search cost 54 s on this VM for "item 9999999"
            # and ended in a model-written not-found instead of the plain "I checked … and found no records" (P1).
            yield Stage("searching", "Searching documents…")
            chunks = retrieve(q, where=where, scope=scope)[:top]

    if not chunks and not _has_rows(results):
        # one plain sentence after the fixed line (agent/not_found.py): what was checked, or where else to look
        out.text = nf + " " + (prompt("scope_denied").strip() if denied
                               else not_found.empty(results) or not_found.empty_filter(results) or not_found.general(q))
        out.not_found = True
        if not denied:
            out.follow_ups = not_found.suggestions(q, results)
        yield Final(_log(out, user, scope)); return

    # A single data result is written from a template, no model call: fixed tools (C7) and, unless
    # answer.templated_generated is off, model-written SQL too (J3) — the rows are the answer.
    # A fixed tool's companion (e.g. each VistaQ file's export orders from the ERP) is written the same way: [D2].
    companion = len(results) == 2 and results[1].tool.endswith(":companion")
    # a repaired model-written result comes with the DISTINCT lookups that fixed it ([D2]…, P2): still templated
    with_lookups = len(results) > 1 and all(x.tool == "lookup" for x in results[1:])
    if r == "data" and not chunks and (len(results) == 1 or companion or with_lookups) and s.get("answer.templated", True):
        if _templated_answer(out, results, nf, allow_generated=bool(s.get("answer.templated_generated", True))):
            yield Final(_log(out, user, scope)); return

    yield _writing(chunks, results)
    with trace.span("answer"):
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
            yield _writing(doc_chunks, [])
            with trace.span("answer"):
                yield from _attempt_stream(retry, question, q, history, doc_chunks, [], user, s, nf, usage)
            if not retry.not_found:
                yield Final(_log(retry, user, scope)); return
            out = retry

    if out.not_found and not out.text:
        out.text = nf + " " + not_found.unverified()
        _log_verify_failure(question, q, out.warnings)
    if out.not_found and not out.follow_ups and not out.error_kind:
        out.follow_ups = not_found.suggestions(q, results)
    # Cache only verified documents answers: database rows go stale in minutes (they have their own
    # short cache in data/cache.py) and a not-found may become answerable after the next ingest.
    if key is not None and r == "documents" and not out.not_found and not out.error_kind:
        _ANSWERS.put(key, out.model_copy(deep=True), ttl)
    yield Final(_log(out, user, scope))


def _names_a_record(question: str) -> bool:
    """Does the question name something only a database holds — a line, a unit, a style, an export order, a PO
    or a file (data/entities.py)? A factory alone does not: "What is the TAL holiday policy?" is a document
    question. Used to decide whether a failed query may still be rescued by the documents."""
    try:
        from ..data.entities import question_entities      # lazy: the data package pulls in the SQL libraries
        return question_entities(question).names_a_record
    except Exception:
        return False


def _tool_answered(results: list[QueryResult]) -> bool:
    """Did a fixed tool or a pasted statement run (whatever it returned)? Such a question needs no documents fallback."""
    return any(x.tool not in ("generated", "lookup") for x in results)


def _has_rows(results: list[QueryResult]) -> bool:
    """Did any data result answer something? A model-written statement that returned one NULL/0/empty cell
    (SUM over no rows) is treated as no rows (P0): `[[None]]` is truthy, so `any(x.rows)` used to call it an
    answer, spend an answer-model call on it and only then search the documents. A fixed tool's 0 stays an
    answer (C7). A DISTINCT lookup (P2) is evidence of what is recorded, never an answer by itself."""
    return any(x.rows and x.tool != "lookup" and not (x.tool == "generated" and is_blank_result(x.columns, x.rows))
               for x in results)


def _writing(chunks: list[Chunk], results: list[QueryResult], limit: int = 4) -> Stage:
    """The "writing" stage with the sources the answer is written from. They come from the scoped search
    and the user's own query, so showing them reveals nothing the answer's references would not."""
    names: list[str] = []
    for c in chunks:
        name = f"{c.title} p. {c.page}"
        if name not in names:
            names.append(name)
    for r in results:
        name = f"{r.database}: {', '.join(r.views) if r.views else 'query'}"
        if name not in names:
            names.append(name)
    trace.first("sources_shown")
    return Stage("writing", "Writing the answer…", sources=names[:limit])


def _templated_answer(out: Answer, results: list[QueryResult], nf: str, allow_generated: bool = False) -> bool:
    """Fill `out` from agent/templated.py when the results qualify and the text verifies: one result, or a fixed
    tool's result [D1] followed by its companion [D2] (an empty companion is left out)."""
    from ..data.tools import companion_tool, load_fixed_tools, merge_companion
    from .templated import try_template
    tool = next((t for t in load_fixed_tools() if t.get("name") == results[0].tool), None)
    comp = companion_tool(tool) if tool is not None else None
    merged = None
    if comp is not None and comp.get("merge") and len(results) > 1 and not results[1].error and results[1].rows:
        # P3: the companion's columns joined onto the main rows — one table from two databases, cited [D1][D2];
        # both results stay in `used`, so every merged cell is licensed by the row set it came from
        merged = merge_companion(results[0], results[1], comp["merge"])
    text = try_template(merged or results[0], tool, allow_generated, marker="D1][D2" if merged else "D1")
    if not text:
        return False
    used = [results[0]] + ([results[1]] if merged else [])
    if merged is None and len(results) > 1 and tool is not None and not results[1].error and results[1].rows:
        more = try_template(results[1], comp, marker="D2")
        if more:
            text += "\n\n" + more
            used.append(results[1])
    for col, asked, matched in results[0].corrections:
        # P2: a literal of the question was replaced by a recorded value — say so, citing the lookup that holds it
        holder = next((x for x in results[1:] if x.tool == "lookup" and any(str(v) == matched for row in x.rows for v in row)),
                      None)
        if holder is not None:
            used.append(holder)
            text += "\n\n" + prompt("matched_value_note").strip().format(asked=asked, matched=matched, marker=f"D{len(used)}")
    block, refs = sources_block([], used)
    ok, problems = verify(text, [], used, nf)
    if not ok:
        out.warnings.append(f"template failed verification, using the model: {problems}")
        return False
    out.text, out.sources_text, out.not_found = text, block, False
    out.results = used
    out.references = [x for x in refs if x.marker in set(_MARK.findall(text))]
    out.follow_ups = _suggest(used, out.question)
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
    # the row budget per provider call is shared by every result (a companion or a lookup used to add its own 50)
    block, refs = sources_block(chunks, results, max(5, int(s["data.max_rows_to_llm"]) // max(1, len(results))))
    out.sources_text = block
    hist = "\n".join(f"{t['role']}: {t['text']}" for t in history[-2 * int(s["answer.history_turns"]):])
    user_msg = ((f"Earlier in this conversation:\n{hist}\n\n" if hist else "") + block +
                f"\n\nQuestion: {question}" + (f"\n(searched as: {q})" if q != question else "") +
                "\n\nAnswer from the sources only and cite each fact with its [P#]/[D#] id.")
    chat = get_chat()
    agent = out.agent if results else ""    # the charter speaks about database rows; not for a documents retry
    system = _system("system_answer", agent)
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
                trace.first("first_token")        # when the user saw the first word of an answer
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
        with trace.span("verify"):
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
        system = _system("system_answer_strict", agent)

    out.not_found = True
    out.text = ""


def _system(name: str, agent: str) -> str:
    """The answer prompt, plus the domain agent's charter (prompts/agents/<agent>.txt) when one answered."""
    from ..domain_agents import charter_text
    extra = charter_text(agent) if agent else ""
    return prompt(name) + (f"\n\n{extra}" if extra else "")


def log_feedback(user: str, index: int, thumb: Any, answer_text: str, scope: str = "",
                 request_id: str = "") -> None:
    """Thumbs up/down from the UI go to logs/chat.csv as a feedback row (docs/PLAN.md M5)."""
    append_row("chat.csv", _CHAT_HEADER, [datetime.now().isoformat(timespec="seconds"), user, scope, "feedback",
                                          f"turn {index}", "", "", "", "", "", f"thumb={thumb} | {answer_text[:200]}",
                                          "", request_id, "", "", "", ""])


# `scope` (PRD FR-6.2): what the user was allowed to see when they asked, so an administrator can
# reconstruct what each user saw (FR-4.8). `request_id` joins the row to its calls.csv and sql.csv rows;
# `seconds` and `timings` (JSON, ragbot/trace.py) say where the time went; `agent` is the domain agent
# that answered a data question (Phase K).
_CHAT_HEADER = ["ts", "user", "scope", "route", "question", "rewritten", "not_found", "refs", "in_tokens",
                "out_tokens", "warnings", "error_kind", "request_id", "seconds", "timings", "agent",
                "databases_considered"]


def _log(a: Answer, user: str, scope: Scope) -> Answer:
    """Every Final answer passes here: stamp it with its request's id and timings, and log it."""
    t = trace.current()
    if t is not None:
        a.request_id, a.timings = t.id, t.snapshot()
    append_row("chat.csv", _CHAT_HEADER, [
        datetime.now().isoformat(timespec="seconds"), user, scope.key(), a.route, a.question, a.rewritten_question,
        a.not_found, ";".join(r.marker + ":" + (r.source or r.database or "") for r in a.references),
        a.usage.input_tokens, a.usage.output_tokens, " | ".join(a.warnings), a.error_kind, a.request_id,
        a.timings.get("total", ""), json.dumps(a.timings, separators=(",", ":")) if a.timings else "", a.agent,
        ";".join(a.databases_considered)])
    return a


def _log_verify_failure(question: str, rewritten: str, problems: list[str]) -> None:
    with open(log_dir() / "verify_failures.csv", "a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerow([datetime.now().isoformat(timespec="seconds"), question, rewritten, " | ".join(problems)])
