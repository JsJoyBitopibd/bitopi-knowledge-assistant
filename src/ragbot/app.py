"""Streamlit pilot UI. Run: streamlit run src/ragbot/app.py

Shows the answer with [P#]/[D#] markers, a Sources rail (PDF file/topic/page/quote or database/view/row keys/SQL),
and thumbs up/down logged to logs/chat.csv (docs/PLAN.md M5). The look follows docs/design/DESIGN.md: screen 1 (home
briefing) on an empty conversation, screen 2 (thread + Sources rail) once a question is asked; the pieces are in
ragbot/ui/ and the base theme in .streamlit/config.toml.
"""
from __future__ import annotations

import sys
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from ragbot.agent.events import Final, Replace, Stage, Token  # noqa: E402
from ragbot.agent.orchestrator import answer_stream, log_feedback  # noqa: E402
from ragbot.auth.filters import scope_where  # noqa: E402
from ragbot.auth.providers import open_user, sign_in, sign_in_required  # noqa: E402
from ragbot.config import log_dir, settings  # noqa: E402
from ragbot.index_version import index_version  # noqa: E402
from ragbot.store import Registry  # noqa: E402
from ragbot.ui import components as ui  # noqa: E402
from ragbot.ui import view  # noqa: E402

s = settings()
st.set_page_config(page_title="Knowledge Assistant · Bitopi", page_icon=ui.AVATAR, layout="wide")
ui.inject_css()

_SIGN_IN_ERRORS = {
    "invalid": "User name or password is wrong.",
    "not_enrolled": "Your account is not enabled for the assistant yet. Ask IT to add you to an assistant group.",
    "locked": "Too many failed attempts. Wait a few minutes and try again.",
    "unavailable": "The sign-in service cannot be reached. Try again, or tell IT.",
}
_PLACEHOLDER = "Ask about export orders, PCDs, PPM meetings, SOPs, IT policies…"


def _sign_in_form() -> None:
    """PRD FR-4.1: nothing below this renders until the user has signed in. Signing in starts a fresh
    conversation, so no answer given to someone else can be seen or re-rendered."""
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        st.html(view.brand_html(light=True))
        with st.form("sign_in"):
            name = st.text_input("User name", placeholder="your Windows (domain) account")
            pw = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
        if submitted:
            res = sign_in(name, pw)
            if res.user is not None:
                st.session_state.clear()
                st.session_state.user = res.user
                st.rerun()
            st.error(_SIGN_IN_ERRORS.get(res.reason, _SIGN_IN_ERRORS["invalid"]))
    st.stop()


if "user" not in st.session_state:
    guest = open_user()                   # auth.provider: none — one shared, still scoped, user; no form
    if guest is None:
        _sign_in_form()
    st.session_state.user = guest
me = st.session_state.user


@st.cache_resource(show_spinner=False)
def _warm() -> bool:
    """Load the heavy models and open the index once, at first render, instead of lazily inside the
    first question's spinner (which cost the first user 60-120 s). Cached for the process lifetime."""
    from ragbot.embed import get_embedder, get_reranker
    from ragbot.retrieve.keyword import get_keyword_index
    from ragbot.store import get_store
    from ragbot.data.catalog import load_catalogs
    get_embedder()
    get_reranker()
    get_keyword_index()
    get_store()
    try:
        from ragbot.data.schema_index import get_index
        for cat in load_catalogs().values():
            if cat.offered_tables:   # the per-database table indexes the router reads on every data question
                get_index(cat)
    except Exception:
        pass  # DB catalogs are optional for document-only pilots
    return True


with st.spinner("Loading models…"):
    _warm()


@st.cache_data(ttl=300)
def _categories(version: int, allowed: tuple) -> list[str]:
    """Categories of the documents this user may see (for the filter only; the search itself applies the
    scope). `version` (ingest_state.json mtime) keys the cache so a newly ingested category appears here
    (no leading underscore: st.cache_data leaves such arguments out of the key); `allowed` is the user's
    document filter as a tuple of (field, values)."""
    # doc_hash set = an indexed version exists (also when its latest update failed and the old one is kept)
    sql, args = "SELECT DISTINCT category FROM document WHERE doc_hash IS NOT NULL", []
    for field, values in allowed:
        sql += f" AND {field} IN ({','.join('?' * len(values))})"
        args += list(values)
    try:
        rows = Registry().db.execute(sql + " ORDER BY category", args)
        cats = [r[0] for r in rows if r[0]]
        return cats or ["General"]
    except Exception:
        return ["General"]


def _scan_version() -> int:
    try:
        return (s.path("index_dir") / "last_scan.json").stat().st_mtime_ns
    except OSError:
        return 0


def _when(iso: str | None) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y %H:%M")
    except (TypeError, ValueError):
        return iso or "?"


@st.cache_data(ttl=300)
def _index_state(version: int, scan_version: int) -> tuple[dict, dict]:
    """(ingest_state.json, last_scan.json); empty dicts when missing."""
    import json
    idx = s.path("index_dir")
    out = []
    for name in ("ingest_state.json", "last_scan.json"):
        try:
            out.append(json.loads((idx / name).read_text(encoding="utf-8")))
        except Exception:
            out.append({})
    return out[0], out[1]


def _index_note(version: int, scan_version: int, admin: bool) -> str:
    """What the Data status card adds to the index line: an update refused because PDFs went missing (share
    offline?), and for administrators the files that could not be indexed."""
    d, scan = _index_state(version, scan_version)
    if not d:
        return "No documents indexed yet. Add PDFs to data/pdfs/<Category>/."
    alert = scan.get("alert", d.get("alert"))
    if alert:
        return (f"⚠ Index not updated at {_when(scan.get('scanned_at', d.get('finished_at')))}: PDF files are missing "
                "from the document folder (see logs/ingest.log)")
    parts = []
    problems = scan.get("failed_total", 0) + len(scan.get("duplicates", []))
    if admin and problems:
        parts.append(f"{problems} file(s) failed or skipped (scripts/inspect.py --failed)")
    if admin and scan.get("pending_removal"):
        parts.append(f"{scan['pending_removal']} missing, removed at the next check")
    if admin and scan.get("scanned_at"):
        parts.append(f"folder checked {_when(scan['scanned_at'])}")
    return " · ".join(parts)


@st.cache_data(ttl=600)
def _pdf_bytes(source: str, version: int) -> bytes | None:
    """The indexed PDF's bytes for the download button, cached so repeated renders don't re-read. Read from
    the exact path the index recorded, never found by name: another folder may hold a file of the same name
    that was not indexed (H1) and that this user may not be allowed to see."""
    rel = Registry().indexed_path(source)
    if not rel:
        return None                                 # not indexed since H3: offered again after the next pass
    try:
        return (s.path("pdf_root") / rel).read_bytes()
    except OSError:
        return None


@st.cache_data(ttl=300)
def _database_count() -> int:
    try:
        from ragbot.data.catalog import load_catalogs
        return len(load_catalogs())
    except Exception:
        return 0


ver = index_version()
_scan = _scan_version()
ingest_state, _ = _index_state(ver, _scan)

# ── session state ─────────────────────────────────────────────────────────────────────────────────────────────
for k, v in {"history": [], "pending_q": None, "pending_refresh": False, "convos": [], "cid": uuid.uuid4().hex[:8],
             "src_turn": None, "brief_more": False}.items():
    if k not in st.session_state:
        st.session_state[k] = v           # history: [{"role","text","refs","q",…}]; "q" = the question answered


def ask(question: str, refresh: bool = False) -> None:
    """Every way of asking (typing, a suggestion, a follow-up, Ask about this) lands here; the next run answers."""
    if question and question.strip():
        st.session_state.pending_q = question.strip()
        st.session_state.pending_refresh = refresh


def _take_typed() -> None:
    ask(st.session_state.get("ask_box") or "")


def _file_current() -> None:
    if st.session_state.history:
        st.session_state.convos.insert(0, {"cid": st.session_state.cid, "history": st.session_state.history,
                                           "title": view.conversation_title(st.session_state.history)})
        del st.session_state.convos[10:]


def new_chat() -> None:
    """New chat files the current conversation under Recent (this browser session only) and starts empty."""
    _file_current()
    st.session_state.history = []
    st.session_state.cid = uuid.uuid4().hex[:8]
    st.session_state.src_turn = None


def open_convo(j: int) -> None:
    picked = st.session_state.convos.pop(j)
    _file_current()
    st.session_state.history = picked["history"]
    st.session_state.cid = picked["cid"]
    st.session_state.src_turn = None


def show_sources(i: int) -> None:
    st.session_state.src_turn = i


def toggle_more() -> None:
    st.session_state.brief_more = not st.session_state.brief_more


# ── watcher signals (the brief, the factory pills, the data status) ──────────────────────────────────────────
def _signals():
    from ragbot.domain_agents.signals import latest
    if not s.get("brief.enabled", True):
        return None, []
    try:
        return latest(me.scope)
    except Exception:
        return None, []


run, all_signals = _signals()
factories = view.factories_offered(me.scope, {x.factory for x in all_signals if x.kind == "ops"},
                                   s.get("scope.factory_codes", []) or [])

# ── sidebar ───────────────────────────────────────────────────────────────────────────────────────────────────
user = me.name
with st.sidebar:
    st.html(view.brand_html())
    st.button("New chat", key="new_chat", icon=":material/add:", on_click=new_chat, width="stretch")
    if len(factories) > 1 or not me.scope.all_factories:
        ui.label("Factory")
        pick = st.pills("Factory", [view.ALL_FACTORIES, *factories], default=view.ALL_FACTORIES, key="factory",
                        label_visibility="collapsed")
    else:
        pick = view.ALL_FACTORIES
    scope = view.narrow_scope(me.scope, pick)       # never wider than the user's own scope
    ui.label("Search in")
    st.html(view.search_in_html(_database_count(), ingest_state.get("documents")))
    allowed = tuple(sorted((k, tuple(v)) for k, v in scope_where(scope).items()))
    cats = st.multiselect("Document categories", _categories(ver, allowed), placeholder="All categories")
    ui.label("Recent")
    if st.session_state.history:
        st.html(view.recent_current_html(view.conversation_title(st.session_state.history)))
    for j, c in enumerate(st.session_state.convos):
        st.button(c["title"], key=f"recent_{c['cid']}", type="tertiary", on_click=open_convo, args=(j,),
                  width="stretch", help=c["title"])
    if not st.session_state.history and not st.session_state.convos:
        st.html('<div class="ka-empty-note">Conversations from this session appear here.</div>')
    signals = [x for x in all_signals if scope.allows_factory(x.factory)]
    fresh = view.freshness(run, [x for x in signals if x.kind == "data"], ingest_state, datetime.now(),
                           int(s.get("brief.stale_after_minutes", 180)))
    footer = f"Signed in as {me.display or me.name}" if sign_in_required() else "Open access · no sign-in"
    st.html(view.data_status_html(fresh, footer, _index_note(ver, _scan, scope.is_admin)))
    if sign_in_required() and st.button("Sign out", key="sign_out", width="stretch"):
        st.session_state.clear()      # the next person starts with no history, refs or PDF buttons
        st.rerun()
    # route, tokens and raw warnings (which can quote database errors) are for administrators only
    admin = st.toggle("Admin details", value=False, help="Show routing and token counts under each answer") \
        if scope.is_admin else False
    if scope.is_admin:
        # PRD FR-4.8: a user's effective scope and the audit trail of what they asked and were shown
        with st.expander("Access and audit (admin)"):
            st.caption("Your scope")
            st.code(scope.key(), language=None)
            who = st.text_input("Look up a user", placeholder="account name")
            if who:
                from ragbot.auth.providers import account_name
                from ragbot.logs import rows_for_user
                rows = rows_for_user("chat.csv", account_name(who) or who.strip())
                asked = [r for r in rows if r.get("route") != "feedback"]
                if not rows:
                    st.caption("No questions logged for this account.")
                else:
                    last = next((r.get("scope") for r in reversed(asked) if r.get("scope")), "")
                    st.caption(f"{len(asked)} question(s), last {asked[-1]['ts'] if asked else '—'}")
                    if last:
                        st.code(last, language=None)
                    import csv as _csv
                    import io as _io
                    buf = _io.StringIO()
                    w = _csv.DictWriter(buf, fieldnames=list(rows[-1].keys()), extrasaction="ignore")
                    w.writeheader(); w.writerows(rows)
                    st.download_button("Download this user's log", buf.getvalue().encode("utf-8"),
                                       file_name=f"audit_{account_name(who) or 'user'}.csv", mime="text/csv")

scope_label = view.scope_text(pick)
cid = st.session_state.cid


def feedback(i: int) -> None:
    val = st.session_state.get(f"fb_{cid}_{i}")
    turn = st.session_state.history[i]
    log_feedback(user, i, val, turn["text"], turn.get("scope", scope.key()), turn.get("request_id", ""))


def _pdf(source: str) -> bytes | None:
    return _pdf_bytes(source, ver)


def _status_text() -> str:
    n_db, n_docs = _database_count(), ingest_state.get("documents")
    parts = ["Ready"]
    if n_db:
        parts.append(f"{n_db} databases")
    if n_docs is not None:
        parts.append(f"{n_docs} documents")
    return " · ".join(parts)


# ── home briefing (screen 1) ──────────────────────────────────────────────────────────────────────────────────
def home() -> None:
    """Phase L (docs/AGENTS_DESIGN.md §4): the viewer's signals from the last watcher run (scripts/watch.py),
    filtered to their factories. Python only — no model call, no database query. Each item asks its question,
    and the answer that comes back carries the cited source."""
    from ragbot.domain_agents import brief as bf
    offset = float(s.get("ui.utc_offset_hours", 6))
    local = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=offset)))
    with st.container(key="home"):
        st.html(view.topline_html(f"{local:%A}, {local.day} {local:%B %Y}", _status_text()))
        hello = bf.greeting(datetime.now(timezone.utc), offset)
        first = me.display.split()[0] if sign_in_required() and me.display else ""
        ops = [x for x in signals if x.kind == "ops"]
        facts = sorted({x.factory for x in ops})
        subtitle = (f"Today's picture across {view.join_names(facts)}. Ask about any number to see where it comes from."
                    if facts else "Ask about export orders, PCDs, PPM meetings, SOPs and IT policies. "
                                  "Every answer shows its source.")
        st.html(view.greeting_html(f"{hello}{', ' + first if first else ''}", subtitle))
        if run is not None and signals:
            limit = int(s.get("brief.max_items", 5))
            b = bf.build(signals, 1000)
            banner = view.banner_html(b.data_warnings)
            if banner:
                st.html(banner)
            ui.kpi_cards(b, ops, ask)
            if b.attention:
                shown = b.attention if st.session_state.brief_more else b.attention[:limit]
                ui.attention_list(shown, len(b.attention), _when, ask)
                if len(b.attention) > limit:
                    st.button("Show fewer" if st.session_state.brief_more else f"Show {len(b.attention) - limit} more",
                              key="more", type="tertiary", on_click=toggle_more)
            note = f"Checked {_when(run['finished'])} by the watcher; signals are facts from the databases, not predictions."
            if bf.is_stale(run["finished"], datetime.now(), int(s.get("brief.stale_after_minutes", 180))):
                note = "⚠ " + note + " This check is older than expected — the watcher may have stopped."
            if scope.is_admin and run.get("errors"):
                note += " Watch rules that failed: " + " | ".join(run["errors"])
            st.html(f'<div class="ka-note">{view.esc(note)}</div>')
        ui.suggestion_pills("Try asking", s.get("ui.suggestions", []) or [], ask)
        ui.composer(_PLACEHOLDER, scope_label, _take_typed)


# ── conversation (screen 2) ───────────────────────────────────────────────────────────────────────────────────
def stream_answer(q: str, hist: list[dict], where: dict | None, refresh: bool, on_sources):
    """Run answer_stream(): progress in a status line, the draft text as it is written, then the verified
    answer in its place. Returns (the Final Answer, seconds)."""
    t0 = time.perf_counter()
    status = st.status("Understanding the question…", expanded=False)
    draft = st.empty()
    text, final = "", None
    for ev in answer_stream(q, history=hist, where=where, user=user, refresh=refresh, scope=scope):
        if isinstance(ev, Stage):
            # the sources appear as soon as the search is done; the model's answer arrives in one burst (I3)
            label = ev.label + (f" Reading: {'; '.join(ev.sources)}" if ev.sources else "")
            status.update(label=label, state="running")
            if ev.sources:
                on_sources(ev.sources)
        elif isinstance(ev, Token):
            text += ev.text
            draft.markdown(text + " ▌")
        elif isinstance(ev, Replace):
            text = ""          # the draft failed verification (or the source changed): drop it
            draft.empty()
        elif isinstance(ev, Final):
            final = ev.answer
    draft.empty()              # the verified text below replaces the provisional draft
    secs = time.perf_counter() - t0
    ui.progress_done(status, final.references if final else [], secs)
    return final, secs


def answer_body(t: dict, i: int, latest: bool, shown: int | None, a=None) -> None:
    """The answer text, its tables, and the action row (Copy, CSV, Refresh, Sources, thumbs)."""
    if t.get("error_kind"):
        st.warning(t["text"])   # a friendly quota/timeout/auth message, not a normal answer
        return
    st.markdown(view.style_markers(t["text"]))
    refs, results = t.get("refs") or [], t.get("results") or []
    ui.result_tables(refs, results, t["text"])
    if t.get("not_found"):
        st.caption("No source in the system covers this question.")
    with st.container(horizontal=True, key=f"actions_{cid}_{i}", vertical_alignment="center"):
        with st.popover("Copy", type="tertiary", icon=":material/content_copy:"):
            st.code(t["text"], language=None, wrap_lines=True)
        ui.csv_buttons(refs, results, f"{cid}_{i}")
        if t.get("q") and any(r.kind != "pdf" for r in refs):
            # database answers may come from the short SQL result cache (their as-of time shows when the rows were
            # read); this re-asks the question with the cache bypassed
            st.button("Refresh data", key=f"rf_{cid}_{i}", type="tertiary", icon=":material/refresh:",
                      help="Re-run the database query live", on_click=ask, args=(t["q"], True))
        if refs and shown != i:
            st.button("Sources", key=f"srcb_{cid}_{i}", type="tertiary", icon=":material/menu_book:",
                      on_click=show_sources, args=(i,))
        st.feedback("thumbs", key=f"fb_{cid}_{i}", on_change=feedback, args=(i,))
    if admin and a is not None:
        tm = a.timings
        # where the seconds went (ragbot/trace.py); the request id finds the answer's rows in the logs
        took = " · ".join(f"{k} {tm[k]:.1f}s" for k in ("route", "retrieve", "data", "first_token", "answer")
                          if k in tm)
        st.caption(f"route: {a.route}" + (f" · agent: {a.agent}" if a.agent else "")
                   + f" · tokens in/out: {a.usage.input_tokens}/{a.usage.output_tokens}"
                   + (f" · {tm['total']:.1f}s ({took})" if "total" in tm else "")
                   + (f" · request {a.request_id}" if a.request_id else "")
                   + (f" · {' | '.join(a.warnings)}" if a.warnings else ""))
    if latest:
        ui.follow_ups(t.get("follow_ups") or [], f"{cid}_{i}", ask)


def conversation_head(head) -> None:
    """Title, scope line and Export; filled last, so Export already holds the answer given in this run."""
    hist_all = st.session_state.history
    title = view.conversation_title(hist_all)
    with head:
        left, right = st.columns([6, 1], vertical_alignment="center")
        left.html(view.header_html(title, f"Scope: {scope_label} · Live data + Documents"))
        now = datetime.now()
        right.download_button("Export", data=view.export_markdown(hist_all, title, f"Scope: {scope_label}", now),
                              file_name=f"conversation_{now:%Y%m%d_%H%M}.md", mime="text/markdown",
                              key=f"export_{cid}", disabled=not hist_all, width="stretch",
                              icon=":material/download:")


def conversation(q: str | None, refresh: bool) -> None:
    hist_all = st.session_state.history
    head = st.container(key="convo_head")
    thread_col, rail_col = st.columns([2.6, 1], gap="large")
    with thread_col:
        thread = st.container(key="thread")
        ui.composer("Ask a follow-up…", scope_label, _take_typed)
    rail = rail_col.empty()

    answers = [i for i, t in enumerate(hist_all) if t["role"] == "assistant"]
    shown = st.session_state.src_turn if st.session_state.src_turn in answers else (answers[-1] if answers else None)
    if q:
        shown = None                       # the new answer's sources replace the rail when it is done
        with rail.container():
            ui.rail_waiting()
    elif shown is not None:
        with rail.container():
            ui.sources_rail(hist_all[shown].get("refs") or [], f"{cid}_{shown}", fresh, _pdf)

    with thread:
        for i, t in enumerate(hist_all):
            if t["role"] == "user":
                st.html(view.user_bubble_html(t["text"]))
                continue
            with st.chat_message("assistant", avatar=ui.AVATAR):
                ui.replay_progress(t.get("refs") or [], t.get("seconds"))
                answer_body(t, i, latest=(i == len(hist_all) - 1 and not q), shown=shown)
        if not q:
            conversation_head(head)
            return
        st.session_state.history.append({"role": "user", "text": q})
        st.html(view.user_bubble_html(q))
        hist = [{"role": t["role"], "text": t["text"]} for t in st.session_state.history[:-1]]
        where = {"category": cats} if cats else None

        def reading(sources):
            with rail.container():
                ui.rail_waiting(sources)

        with st.chat_message("assistant", avatar=ui.AVATAR):
            try:
                a, secs = stream_answer(q, hist, where, refresh, reading)
            except Exception:  # last-resort guard: never show a raw traceback to a user
                with open(log_dir() / "errors.log", "a", encoding="utf-8") as fh:
                    fh.write(f"\n=== {datetime.now().isoformat()} q={q!r}\n{traceback.format_exc()}")
                st.error("Something went wrong answering that. The team has been notified — please try again.")
                conversation_head(head)
                st.stop()
            i = len(st.session_state.history)   # index this assistant turn will have in history
            turn = {"role": "assistant", "text": a.text, "refs": a.references, "q": q, "results": a.results,
                    "follow_ups": a.follow_ups, "request_id": a.request_id, "seconds": secs,
                    "not_found": a.not_found, "error_kind": a.error_kind, "scope": scope.key()}
            st.session_state.history.append(turn)
            st.session_state.src_turn = None
            answer_body(turn, i, latest=True, shown=i, a=a)
    with rail.container():
        ui.sources_rail(a.references, f"{cid}_{i}", fresh, _pdf)
    conversation_head(head)


q = st.session_state.pending_q
refresh = bool(st.session_state.pending_refresh)
st.session_state.pending_q = None
st.session_state.pending_refresh = False

if st.session_state.history or q:
    conversation(q, refresh)
else:
    home()
