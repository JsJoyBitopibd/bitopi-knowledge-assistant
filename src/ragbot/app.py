"""Streamlit pilot UI. Run: streamlit run src/ragbot/app.py

Shows the answer with [P#]/[D#] markers, a References panel (PDF file/topic/page/quote or
database/view/row keys/SQL), and thumbs up/down logged to logs/chat.csv (docs/PLAN.md M5).
"""
from __future__ import annotations

import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from ragbot.agent.events import Final, Replace, Stage, Token  # noqa: E402
from ragbot.agent.orchestrator import answer_stream, log_feedback  # noqa: E402
from ragbot.auth.filters import scope_where  # noqa: E402
from ragbot.auth.providers import open_user, sign_in, sign_in_required  # noqa: E402
from ragbot.config import log_dir, settings  # noqa: E402
from ragbot.data import present  # noqa: E402
from ragbot.index_version import index_version  # noqa: E402
from ragbot.store import Registry  # noqa: E402

s = settings()
st.set_page_config(page_title=s["ui.title"], layout="centered")
st.title(s["ui.title"])

_SIGN_IN_ERRORS = {
    "invalid": "User name or password is wrong.",
    "not_enrolled": "Your account is not enabled for the assistant yet. Ask IT to add you to an assistant group.",
    "locked": "Too many failed attempts. Wait a few minutes and try again.",
    "unavailable": "The sign-in service cannot be reached. Try again, or tell IT.",
}


def _sign_in_form() -> None:
    """PRD FR-4.1: nothing below this renders until the user has signed in. Signing in starts a fresh
    conversation, so no answer given to someone else can be seen or re-rendered."""
    with st.form("sign_in"):
        name = st.text_input("User name", placeholder="your Windows (domain) account")
        pw = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
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
scope = me.scope


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


with st.status("Loading models…", expanded=False) as _status:
    _warm()
    _status.update(label="Ready", state="complete")


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
        return "?"


@st.cache_data(ttl=300)
def _index_caption(version: int, scan_version: int, admin: bool) -> str:
    """Index status for the sidebar; files that could not be indexed are counted for administrators."""
    import json
    idx = s.path("index_dir")
    try:
        d = json.loads((idx / "ingest_state.json").read_text(encoding="utf-8"))
    except Exception:
        return "No documents indexed yet. Add PDFs to data/pdfs/<Category>/."
    try:
        scan = json.loads((idx / "last_scan.json").read_text(encoding="utf-8"))
    except Exception:
        scan = {}                                   # an index built before H3
    alert = scan.get("alert", d.get("alert"))
    if alert:      # an update refused because PDFs went missing (share offline?); IT sees it here
        return (f"⚠ Index not updated at {_when(scan.get('scanned_at', d.get('finished_at')))}: PDF files are missing "
                f"from the document folder (see logs/ingest.log) · {d.get('documents', '?')} documents")
    parts = [f"Index updated {_when(d.get('index_updated_at', d.get('finished_at')))}",
             f"{scan.get('documents', d.get('documents', '?'))} documents"]
    if scan.get("scanned_at"):
        parts.append(f"folder checked {_when(scan['scanned_at'])}")
    problems = scan.get("failed_total", 0) + len(scan.get("duplicates", []))
    if admin and problems:
        parts.append(f"{problems} file(s) failed or skipped (scripts/inspect.py --failed)")
    if admin and scan.get("pending_removal"):
        parts.append(f"{scan['pending_removal']} missing, removed at the next check")
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


ver = index_version()

if "history" not in st.session_state:
    st.session_state.history = []      # [{"role","text","refs","q"}]; "q" = the question an answer replied to
if "pending_q" not in st.session_state:
    st.session_state.pending_q = None

user = me.name
with st.sidebar:
    if sign_in_required():
        st.caption(f"Signed in as **{me.display}** ({me.name})")
        if st.button("Sign out"):
            st.session_state.clear()      # the next person starts with no history, refs or PDF buttons
            st.rerun()
    else:
        st.caption(f"**{me.display}**")
    allowed = tuple(sorted((k, tuple(v)) for k, v in scope_where(scope).items()))
    cats = st.multiselect("Document categories (empty = all)", _categories(ver, allowed))
    # route, tokens and raw warnings (which can quote database errors) are for administrators only
    admin = st.toggle("Admin details", value=False, help="Show routing and token counts under each answer") \
        if scope.is_admin else False
    if st.button("Clear chat"):
        st.session_state.history = []
        st.rerun()
    st.caption(_index_caption(ver, _scan_version(), scope.is_admin))
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


def feedback(i: int) -> None:
    val = st.session_state.get(f"fb{i}")
    turn = st.session_state.history[i]
    log_feedback(user, i, val, turn["text"], scope.key(), turn.get("request_id", ""))


def show_result(res, key: str) -> None:
    """C8: the rows behind a [D#] reference — sortable table, bar chart for label→number lists, CSV."""
    if not res.rows:
        return
    st.caption(present.caption(res))
    df = present.frame(res)
    st.dataframe(df, hide_index=True, width="stretch")
    chart = present.chart_columns(res)
    if chart:
        st.bar_chart(df.set_index(chart[0])[chart[1]])
    st.download_button("Download CSV", data=present.csv_bytes(res), file_name=present.csv_name(res),
                       mime="text/csv", key=f"csv{key}")


def show_refs(refs, results=None, turn: int = 0) -> None:
    """`turn` makes widget keys unique per chat turn: two turns citing the same chunk used to collide."""
    if not refs:
        return
    results = results or []
    with st.expander(f"References ({len(refs)})", expanded=True):
        for r in refs:
            if r.kind == "pdf":
                st.markdown(f"**[{r.marker}] {r.source}** · Topic: {r.section} · Page {r.page} · Category: {r.category}")
                st.caption(f"“{r.quote}”")
                data = _pdf_bytes(r.source, ver)
                if data:
                    st.download_button("Open PDF", data=data, file_name=r.source,
                                       key=f"dl{turn}{r.marker}{r.chunk_id}", mime="application/pdf")
            else:
                asof = r.as_of.strftime("%d %b %Y %H:%M") if r.as_of else ""
                st.markdown(f"**[{r.marker}] {r.database} ({r.engine}) · {', '.join(r.views or [])}**  \n"
                            f"Row key: {'; '.join(r.row_keys or []) or '—'} · {r.row_count} row(s) · "
                            f"as of {asof} · tool: {r.tool}")
                k = int(r.marker[1:]) - 1
                if 0 <= k < len(results):
                    show_result(results[k], f"{turn}{r.marker}")
                with st.expander("show SQL"):
                    st.code(r.sql or "", language="sql")
                    if r.params:
                        st.json(r.params)


def render_answer(a, turn: int) -> None:
    if a.error_kind:
        st.warning(a.text)   # a friendly quota/timeout/auth message, not a normal answer
        return
    st.markdown(a.text)
    if a.not_found:
        st.caption("No source in the system covers this question.")
    show_refs(a.references, a.results, turn)
    if admin:
        t = a.timings
        # where the seconds went (ragbot/trace.py); the request id finds the answer's rows in the logs
        took = " · ".join(f"{k} {t[k]:.1f}s" for k in ("route", "retrieve", "data", "first_token", "answer")
                          if k in t)
        st.caption(f"route: {a.route}" + (f" · agent: {a.agent}" if a.agent else "")
                   + f" · tokens in/out: {a.usage.input_tokens}/{a.usage.output_tokens}"
                   + (f" · {t['total']:.1f}s ({took})" if "total" in t else "")
                   + (f" · request {a.request_id}" if a.request_id else "")
                   + (f" · {' | '.join(a.warnings)}" if a.warnings else ""))


def follow_up_chips(i: int, suggestions: list[str] | None) -> None:
    """G3: next questions under the latest fixed-tool answer; a click asks it like a suggestion chip."""
    if not suggestions:
        return
    cols = st.columns(len(suggestions))
    for j, sug in enumerate(suggestions):
        if cols[j].button(sug, key=f"fu{i}_{j}"):
            st.session_state.pending_q = sug
            st.rerun()


def refresh_button(i: int, question: str, refs) -> None:
    """Database answers may come from the short SQL result cache (their as-of time shows when the rows
    were read). This re-asks the question with the cache bypassed."""
    if question and any(r.kind != "pdf" for r in refs or []):
        if st.button("↻ Refresh data", key=f"rf{i}", help="Re-run the database query live"):
            st.session_state.pending_q = question
            st.session_state.pending_refresh = True
            st.rerun()


fu_slot = st.empty()   # follow-up chips of the latest answer; cleared below when a new question arrives
for i, t in enumerate(st.session_state.history):
    with st.chat_message(t["role"]):
        st.markdown(t["text"])
        if t["role"] == "assistant":
            show_refs(t.get("refs"), t.get("results"), i)
            refresh_button(i, t.get("q", ""), t.get("refs"))
            if i == len(st.session_state.history) - 1:          # only under the latest answer
                fu_slot = st.empty()
                with fu_slot.container():
                    follow_up_chips(i, t.get("follow_ups"))
            st.feedback("thumbs", key=f"fb{i}", on_change=feedback, args=(i,))

# Suggested-question chips on an empty conversation.
chips = st.empty()   # a slot, so the chips can be cleared the moment a question is asked
if not st.session_state.history:
    picks = s.get("ui.suggestions", []) or []
    if picks:
        with chips.container():
            st.caption("Try one of these:")
            cols = st.columns(min(len(picks), 2))
            for j, sug in enumerate(picks):
                if cols[j % len(cols)].button(sug, key=f"sug{j}"):
                    st.session_state.pending_q = sug
                    st.rerun()

typed = st.chat_input("Ask about export orders, PCDs, PPM meetings, SOPs, IT policies…")
q = typed or st.session_state.pending_q
refresh = bool(st.session_state.get("pending_refresh")) and not typed
st.session_state.pending_q = None
st.session_state.pending_refresh = False


def stream_answer(q: str, hist: list[dict], where: dict | None, refresh: bool = False):
    """Run answer_stream(): progress in a status box, the draft text as it is written, then the
    verified answer in its place. Returns the Final Answer."""
    t0 = time.perf_counter()
    status = st.status("Understanding the question…", expanded=False)
    draft = st.empty()
    text, final = "", None
    for ev in answer_stream(q, history=hist, where=where, user=user, refresh=refresh, scope=scope):
        if isinstance(ev, Stage):
            # the sources appear as soon as the search is done; the model's answer arrives in one burst (I3)
            label = ev.label + (f" Reading: {'; '.join(ev.sources)}" if ev.sources else "")
            status.update(label=label, state="running")
        elif isinstance(ev, Token):
            text += ev.text
            draft.markdown(text + " ▌")
        elif isinstance(ev, Replace):
            text = ""          # the draft failed verification (or the source changed): drop it
            draft.empty()
        elif isinstance(ev, Final):
            final = ev.answer
    draft.empty()              # the verified text below replaces the provisional draft
    status.update(label=f"Answered in {time.perf_counter() - t0:.1f} s", state="complete")
    return final


if q:
    chips.empty()
    fu_slot.empty()        # the previous answer's follow-ups; the new answer brings its own
    st.session_state.history.append({"role": "user", "text": q})
    with st.chat_message("user"):
        st.markdown(q)
    hist = [{"role": t["role"], "text": t["text"]} for t in st.session_state.history[:-1]]
    where = {"category": cats} if cats else None
    with st.chat_message("assistant"):
        try:
            a = stream_answer(q, hist, where, refresh)
        except Exception:  # last-resort guard: never show a raw traceback to a user
            with open(log_dir() / "errors.log", "a", encoding="utf-8") as fh:
                fh.write(f"\n=== {datetime.now().isoformat()} q={q!r}\n{traceback.format_exc()}")
            st.error("Something went wrong answering that. The team has been notified — please try again.")
            st.stop()
        i = len(st.session_state.history)   # index this assistant turn will have in history
        render_answer(a, i)
        st.session_state.history.append({"role": "assistant", "text": a.text, "refs": a.references, "q": q,
                                         "results": a.results, "follow_ups": a.follow_ups,
                                         "request_id": a.request_id})
        # Feedback now, not after a rerun; the history loop re-renders it with the same key next run.
        refresh_button(i, q, a.references)
        follow_up_chips(i, a.follow_ups)
        st.feedback("thumbs", key=f"fb{i}", on_change=feedback, args=(i,))
