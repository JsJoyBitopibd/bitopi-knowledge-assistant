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
from ragbot.agent.orchestrator import answer_stream, log_feedback, log_stopped  # noqa: E402
from ragbot.agent.state import ConversationState  # noqa: E402
from ragbot import conversations  # noqa: E402
from ragbot.auth import session as auth_session  # noqa: E402
from ragbot.auth.filters import scope_where  # noqa: E402
from ragbot.auth.providers import (end_session, open_user, restore_session, session_hours, sign_in,  # noqa: E402
                                   sign_in_hint, sign_in_required, start_session)
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
_PLACEHOLDER = "Ask about export orders, PCDs, line quality and DHU, SOPs, IT policies…"


def _sign_in_form() -> None:
    """PRD FR-4.1: nothing below this renders until the user has signed in. Signing in starts a fresh
    conversation, so no answer given to someone else can be seen or re-rendered."""
    _, mid, _ = st.columns([1, 1.1, 1])
    with mid:
        _browser_script()                 # after a sign-out: the browser forgets its session cookie
        st.html(view.brand_html(light=True))
        with st.form("sign_in"):
            name = st.text_input("User name", placeholder=sign_in_hint())
            pw = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", type="primary", width="stretch")
        if submitted:
            res = sign_in(name, pw)
            if res.user is not None:
                token = start_session(res.user)
                st.session_state.clear()
                st.session_state.user = res.user
                if token:                 # stay signed in on this browser for auth.session_hours
                    st.session_state.browser_script = auth_session.set_cookie_js(token, session_hours())
                st.rerun()
            st.error(_SIGN_IN_ERRORS.get(res.reason, _SIGN_IN_ERRORS["invalid"]))
    st.stop()


def _browser_script() -> None:
    """Run a pending cookie script once: store the session token after a sign-in, forget it after a sign-out."""
    js = st.session_state.pop("browser_script", None)
    if js:
        st.html(js, unsafe_allow_javascript=True)


def _cookie_user():
    """The user a valid session cookie names (auth/session.py), or None. st.context.cookies holds the cookies the
    browser sent when this page was loaded."""
    try:
        return restore_session(st.context.cookies.get(auth_session.COOKIE))
    except Exception:
        return None


if "user" not in st.session_state:
    guest = open_user()                   # auth.provider: none — one shared, still scoped, user; no form
    if guest is None:
        guest = _cookie_user()            # signed in earlier on this browser: no form after a reload
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
    # one tiny run each: a model's first inference also allocates its buffers and thread pools, which would
    # otherwise land on the first user's question after a restart
    get_embedder().embed(["warm up"])
    rr = get_reranker()
    if rr is not None:
        rr.score("warm up", ["warm up"])
    get_keyword_index()
    get_store()
    try:
        from ragbot.llm import get_chat, get_small_chat
        get_small_chat(); get_chat()          # the provider SDK's import cost ~1 s on the first routed question
    except Exception:
        pass  # a missing key surfaces as a friendly error on the first question, as before
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
def _database_count(groups: tuple[str, ...] = ()) -> int:
    """The databases this user may read (Phase S4): a restricted one is not counted for someone outside its
    groups, so the sidebar does not announce a database that will never answer them."""
    try:
        from ragbot.auth.models import Scope
        from ragbot.data.catalog import readable_databases
        return len(readable_databases(Scope(factories=frozenset({"*"}), groups=frozenset(groups))))
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


# ── Phase U1: conversations kept per user on the server (ragbot/conversations.py) ─────────────────────────────
def _conv_owner() -> str:
    """Whom this browser's conversations are stored under: the signed-in account, or under open access this
    browser's random id (cookie ka_browser) — '' when keeping them is switched off."""
    s = settings()
    if not s.get("ui.conversations.enabled", True):
        return ""
    if sign_in_required():
        return me.name
    if str(s.get("ui.conversations.open_access", "browser")).lower() != "browser":
        return ""
    bid = st.session_state.get("browser_id")
    if not bid:
        try:
            bid = auth_session.valid_browser_id(st.context.cookies.get(auth_session.BROWSER_COOKIE))
        except Exception:
            bid = None
        if not bid:
            bid = auth_session.new_browser_id()
            st.session_state.browser_script = (st.session_state.get("browser_script") or "") \
                + auth_session.set_browser_cookie_js(bid)
        st.session_state.browser_id = bid
    return f"open:{bid}"


def _persist() -> None:
    """Write the turns added since the last write (one write per answered question). A failure is logged, never
    shown: the answer on screen matters more than its copy."""
    owner, hist = st.session_state.get("conv_owner") or "", st.session_state.history
    done = int(st.session_state.get("saved_upto") or 0)
    if not owner or len(hist) <= done:
        return
    try:
        conversations.save(st.session_state.cid, owner, scope.key(), view.conversation_title(hist), hist,
                           from_idx=done)
        st.session_state.saved_upto = len(hist)
        st.query_params["c"] = st.session_state.cid            # a reload resumes this conversation
    except Exception:
        with open(log_dir() / "errors.log", "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {datetime.now().isoformat()} conversations.save\n{traceback.format_exc()}")


def open_saved(cid: str) -> None:
    """Open one of this user's saved conversations (Recent), in place of the current one, which is already saved."""
    loaded = conversations.load(cid, st.session_state.get("conv_owner") or "")
    if loaded is None:
        return
    st.session_state.history, st.session_state.cid = loaded, cid
    st.session_state.saved_upto, st.session_state.src_turn = len(loaded), None
    st.query_params["c"] = cid


def rename_current() -> None:
    title = (st.session_state.get("rename_box") or "").strip()
    if title and conversations.rename(st.session_state.cid, st.session_state.get("conv_owner") or "", title):
        st.session_state.titles = {**st.session_state.get("titles", {}), st.session_state.cid: title}


def delete_current() -> None:
    """Two clicks: the first arms it, the second deletes and starts an empty conversation."""
    if not st.session_state.get("confirm_delete"):
        st.session_state.confirm_delete = True
        return
    conversations.delete(st.session_state.cid, st.session_state.get("conv_owner") or "")
    st.session_state.confirm_delete = False
    st.session_state.history, st.session_state.saved_upto = [], 0
    st.session_state.cid, st.session_state.src_turn = uuid.uuid4().hex[:8], None
    st.query_params.clear()


def new_chat() -> None:
    """New chat starts empty. With conversations kept on the server the current one is already saved and listed
    under Recent; otherwise it is filed under Recent for this browser session only."""
    if not st.session_state.get("conv_owner"):
        _file_current()
    st.session_state.history = []
    st.session_state.cid = uuid.uuid4().hex[:8]
    st.session_state.src_turn = None
    st.session_state.saved_upto = 0
    st.query_params.clear()


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

# ── Phase U1: whose conversations these are, resume after a reload, and the retention purge ──────────────────
@st.cache_resource(show_spinner=False)
def _purge_once() -> int:
    """Old and deleted conversations are removed once per app process (and by scripts/purge_conversations.py)."""
    try:
        return conversations.purge(int(s.get("ui.conversations.retention_days", 90)))
    except Exception:
        return 0


st.session_state.conv_owner = _conv_owner()
if st.session_state.conv_owner:
    _purge_once()
    wanted = st.query_params.get("c")
    if wanted and not st.session_state.history and not st.session_state.get("resumed"):
        st.session_state.resumed = True                       # once per browser session: then New chat means new
        open_saved(wanted)

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
    st.html(view.search_in_html(_database_count(tuple(sorted(scope.groups))), ingest_state.get("documents")))
    allowed = tuple(sorted((k, tuple(v)) for k, v in scope_where(scope).items()))
    cats = st.multiselect("Document categories", _categories(ver, allowed), placeholder="All categories")
    ui.label("Recent")
    if st.session_state.conv_owner:
        # Phase U1: the user's own conversations from the server, newest first; the open one is highlighted
        saved = conversations.recent(st.session_state.conv_owner, int(s.get("ui.conversations.recent", 20)))
        here = next((c["title"] for c in saved if c["id"] == st.session_state.cid), "")
        if st.session_state.history:
            st.html(view.recent_current_html(here or view.conversation_title(st.session_state.history)))
        for c in saved:
            if c["id"] != st.session_state.cid:
                st.button(c["title"], key=f"recent_{c['id']}", type="tertiary", on_click=open_saved, args=(c["id"],),
                          width="stretch", help=c["title"])
        if not st.session_state.history and not saved:
            st.html('<div class="ka-empty-note">Your conversations appear here and are kept for '
                    f'{int(s.get("ui.conversations.retention_days", 90))} days.</div>')
    else:
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
        end_session(me)               # this account's session tokens stop working, on every browser
        st.session_state.clear()      # the next person starts with no history, refs or PDF buttons
        st.session_state.browser_script = auth_session.clear_cookie_js()
        st.rerun()
    _browser_script()                 # after a sign-in: the browser keeps the session token
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
                from ragbot.data.catalog import denied_databases, load_catalogs
                from ragbot.domain_agents import agent_readable, load_agents
                from ragbot.logs import rows_for_user, withhold_restricted
                # Phase S4: an admin who may not read a restricted database sees that someone asked, not what.
                # If the catalogs cannot be read the rule cannot be applied, so the log is not shown at all.
                try:
                    known = load_catalogs()
                    denied = denied_databases(scope, known)
                    readable = set(known) - denied
                    gone = [a.name for a in load_agents().values() if not agent_readable(a, readable, known)]
                    rows = withhold_restricted(rows_for_user("chat.csv", account_name(who) or who.strip()),
                                               denied, gone)
                except Exception:
                    rows = None
                asked = [r for r in rows or [] if r.get("route") != "feedback"]
                if rows is None:
                    st.caption("The audit cannot be shown: the database catalogs could not be read.")
                elif not rows:
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
    # Phase N2/N3b: the Stage-3 preview, administrators only and off unless agents.recommendations.enabled.
    from ragbot.domain_agents import recommend as _rec
    if _rec.enabled(s.get, scope):
        with st.expander("Recommendations — preview (admin)"):
            st.caption("Actions from the departments' playbooks for open red and amber Quality files. A preview: "
                       "the risk levels have not passed their backtest yet, and a cost is shown only when the "
                       "playbook is approved and every rate it uses has an owner and a date.")
            _opts = _rec.options(scope, limit=20)
            # N2: the program ranks a file's ready options; the executive-tier model explains that order, verified
            for _pid in dict.fromkeys(o.prediction_id for o in _rec.rank(_opts)):
                _first = next(o for o in _opts if o.prediction_id == _pid)
                if st.button(f"Rank and explain the options for {_first.factory} {_first.subject}",
                             key=f"rec-explain-{_pid}"):
                    from ragbot.agent.citations import render_references as _render_refs
                    with st.spinner("Ranking and explaining…"):
                        _ex = _rec.explain(_pid, scope, opts=_opts, user=user)
                    st.markdown("**Order (the program's):** " + "; ".join(
                        f"{i}. {o.action_title}" for i, o in enumerate(_ex.ranked, start=1)))
                    if _ex.ok:
                        st.markdown(_ex.text)
                        st.markdown(_render_refs(_ex.references))
                    if _ex.problems:
                        st.caption("Not explained: " + "; ".join(_ex.problems))
            for k, o in enumerate(_opts):
                st.markdown(f"**{o.level}** · {o.factory} {o.subject} — {o.action_title} "
                            f"*({o.playbook}, {o.playbook_status})*")
                if o.cost:
                    st.caption("Cost: " + _rec.cost_text(o.cost))
                if o.blocked:
                    st.caption("Not yet: " + "; ".join(o.blocked))
                    continue
                chosen = {n: st.number_input(n, min_value=p.min, max_value=p.max, key=f"rec-{k}-{n}")
                          for n, p in o.params.items()}
                if st.button("Recommend", key=f"rec-{k}"):
                    try:
                        st.success(f"Recommendation {_rec.recommend(o, chosen)} stored.")
                    except _rec.decisions.Refused as e:
                        st.error(str(e))
            for r in _rec.stored(scope)[:10]:
                d = r.get("decision")
                st.markdown(f"#{r['id']} {r['factory']} {r['subject']} — {r['action']}: "
                            + (f"**{d['verdict']}** by {d['decided_by']}" if d else "no decision yet"))
                why = st.text_input("Reason (needed to reject)", key=f"why-{r['id']}")
                c1, c2 = st.columns(2)
                for col, verdict in ((c1, "approve"), (c2, "reject")):
                    if col.button(verdict.capitalize(), key=f"{verdict}-{r['id']}"):
                        try:
                            _rec.decide(r["id"], me, verdict, why)
                            st.rerun()
                        except _rec.decisions.Refused as e:
                            st.error(str(e))
    # Phase O2: copy-only drafts from cited facts, administrators only and off unless agents.drafts.enabled.
    from ragbot.agent import drafts as _drafts
    if _drafts.enabled(s.get, scope):
        with st.expander("Drafts — copy only (admin)"):
            st.caption("A draft written only from cited facts, for you to edit and send yourself. Nothing is sent, "
                       "approved or changed anywhere.")
            kind = st.selectbox("Draft", list(_drafts.KINDS), format_func=_drafts.KINDS.get, key="draft-kind")
            about = st.text_input("About (an order, PO, file, line or LC)", key="draft-about")
            if st.button("Write draft", key="draft-go") and about.strip():
                with st.spinner("Reading the facts and writing…"):
                    d = _drafts.make(kind, about, user=user, scope=scope)
                if d.ok:
                    st.code(d.text, language=None)          # the code block has its own copy button
                    blanks = _drafts.placeholders(d.text)
                    if blanks:
                        st.caption("To fill in before sending: " + ", ".join(blanks))
                else:
                    st.warning("No draft: " + "; ".join(d.problems))
            for r in _drafts.stored(user, limit=5):
                st.caption(f"#{r['id']} {r['created']} — {_drafts.KINDS.get(r['kind'], r['kind'])}: {r['subject']}")

scope_label = view.scope_text(pick)
cid = st.session_state.cid


def feedback(i: int) -> None:
    val = st.session_state.get(f"fb_{cid}_{i}")
    turn = st.session_state.history[i]
    log_feedback(user, i, val, turn["text"], turn.get("scope", scope.key()), turn.get("request_id", ""))


def _pdf(source: str) -> bytes | None:
    return _pdf_bytes(source, ver)


def _status_text() -> str:
    n_db, n_docs = _database_count(tuple(sorted(scope.groups))), ingest_state.get("documents")
    parts = ["Ready"]
    if n_db:
        parts.append(f"{n_db} databases")
    if n_docs is not None:
        parts.append(f"{n_docs} documents")
    return " · ".join(parts)


# ── home briefing (screen 1) ──────────────────────────────────────────────────────────────────────────────────
def _home_suggestions() -> list[str]:
    """Phase U3 (ui/suggest.py): the user's own recent questions, examples for their factory, then the defaults."""
    from ragbot.agent.capabilities import examples
    from ragbot.data.tools import load_fixed_tools, match_fixed_tool
    from ragbot.ui.suggest import home_suggestions
    owner = st.session_state.get("conv_owner") or ""
    try:
        own = conversations.recent_questions(owner, 6) if owner else []
    except Exception:
        own = []
    mine = me.identity.home_factory if me.identity.home_factory and scope.allows_factory(me.identity.home_factory) \
        else None
    tools = load_fixed_tools()
    return home_suggestions(own_recent=own, examples=examples(scope, limit=8), factory=mine,
                            fallback=s.get("ui.suggestions", []) or [],
                            still_matches=lambda q: match_fixed_tool(q, tools) is not None,
                            limit=int(s.get("ui.suggestion_count", 6)))


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
        # Phase U3: the signed-in person's own factory (never a narrowing of what they see: the pills stay on All)
        mine = me.identity.home_factory
        if mine and scope.allows_factory(mine):
            company = ""
            try:
                from ragbot.data.entities import load_entities
                c = load_entities().company(me.identity.company_id) if me.identity.company_id else None
                company = c.name if c is not None else ""
            except Exception:
                company = ""
            st.html(view.unit_line_html(mine, me.identity.unit_id, company))
        # Phase U1/U3: carry on where the user left off
        last = conversations.recent(st.session_state.conv_owner, 1) if st.session_state.conv_owner else []
        if last:
            st.button(f"Continue: {last[0]['title']} · {view.ago(last[0]['updated'], datetime.now())}",
                      key="continue_last", type="tertiary", icon=":material/history:", on_click=open_saved,
                      args=(last[0]["id"],))
        if run is not None and signals:
            limit = int(s.get("brief.max_items", 5))
            # Phase M: Stage-2 levels, read from their own tables (signals.latest() reads the
            # watcher's run; a risk run is deliberately not one, or it would replace the brief).
            from ragbot.domain_agents.signals import risk_latest
            _rrun, _preds = risk_latest(scope)
            risk = bf.risk_summary("quality", _preds, (_rrun or {}).get("finished", ""), limit)
            b = bf.build(signals, 1000, risk=risk)
            banner = view.banner_html(b.data_warnings)
            if banner:
                st.html(banner)
            ui.kpi_cards(b, ops, ask)
            risk_block = view.risk_html(b.risk)
            if risk_block:
                st.html(risk_block)
            if b.attention:
                shown = b.attention if st.session_state.brief_more else b.attention[:limit]
                ui.attention_list(shown, len(b.attention), _when, ask)
                if len(b.attention) > limit:
                    st.button("Show fewer" if st.session_state.brief_more else f"Show {len(b.attention) - limit} more",
                              key="more", type="tertiary", on_click=toggle_more)
            note = f"Checked {_when(run['finished'])} by the watcher; the figures are read from the databases. Quality risk levels are calculated from them, not forecast by AI."
            if bf.is_stale(run["finished"], datetime.now(), int(s.get("brief.stale_after_minutes", 180))):
                note = "⚠ " + note + " This check is older than expected — the watcher may have stopped."
            if scope.is_admin and run.get("errors"):
                note += " Watch rules that failed: " + " | ".join(run["errors"])
            st.html(f'<div class="ka-note">{view.esc(note)}</div>')
        ui.suggestion_pills("Try asking", _home_suggestions(), ask)
        ui.composer(_PLACEHOLDER, scope_label, _take_typed)


# ── conversation (screen 2) ───────────────────────────────────────────────────────────────────────────────────
def stream_answer(q: str, hist: list[dict], where: dict | None, refresh: bool, on_sources):
    """Run answer_stream(): progress in a status line, the draft text as it is written, then the verified
    answer in its place. Returns (the Final Answer, seconds)."""
    t0 = time.perf_counter()
    # Phase T8: Stop. A click restarts the page, which ends this run at its next update (the next stage or token —
    # a running database query or model call finishes first); the next run records the question as stopped.
    stop = st.empty()
    stop.button("Stop", key=f"stop_{st.session_state.cid}_{len(st.session_state.history)}", type="tertiary",
                icon=":material/stop_circle:", help="Stop writing this answer")
    status = st.status("Understanding the question…", expanded=False)
    draft = st.empty()
    text, final = "", None
    # Phase T: the earlier answers' rows and queries travel with the question, so a follow-up can reshape them
    state = ConversationState.from_history(st.session_state.history[:-1], st.session_state.cid,
                                           int(settings().get("answer.prior_turns", 3)))
    for ev in answer_stream(q, history=hist, where=where, user=user, refresh=refresh, scope=scope, state=state,
                            me=me.identity):
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
    stop.empty()
    secs = time.perf_counter() - t0
    if final is not None:
        ui.progress_done(status, final.references, secs, final.kind,
                         _understood({"q": q, "rewritten": final.rewritten_question, "kind": final.kind,
                                      "route": final.route}))
    return final, secs


def _understood(t: dict) -> str:
    """How a question was read, when that differs from what was typed (a follow-up rewritten with the conversation,
    a reply to a clarifying question completed) — shown on the progress line (Phase T8). Not for reshapes, re-runs
    or replies about the conversation, whose rewritten text is internal."""
    asked, read = str(t.get("q") or "").strip(), str(t.get("rewritten") or "").strip()
    if not read or read.lower().rstrip("?.! ") == asked.lower().rstrip("?.! "):
        return ""
    if t.get("kind") in ("refine", "requery", "meta", "social") or t.get("route") in ("refine", "requery", "meta"):
        return ""
    return read


def _marks(question: str) -> list[str]:
    """The records a question names (orders, POs, files, lines, styles, employees): rows holding one are highlighted
    in the answer's table (Phase T8). Never a factory — that would mark every row."""
    if not question:
        return []
    try:
        from ragbot.data.entities import question_entities
        e = question_entities(question)
        return [str(x) for x in (*e.eos, *e.pos, *e.files, *e.lines, *e.styles, *e.employees)]
    except Exception:
        return []


def answer_body(t: dict, i: int, latest: bool, shown: int | None, a=None) -> None:
    """The answer text, its tables, and the action row (Copy, CSV, Refresh, Sources, thumbs)."""
    if t.get("error_kind"):
        st.warning(t["text"])   # a friendly quota/timeout/auth message, not a normal answer
        return
    mark = _marks(t.get("q") or "")
    ui.answer_text(t["text"], t.get("results") or [], mark)    # tables in the answer become sortable tables
    refs, results = t.get("refs") or [], t.get("results") or []
    ui.result_tables(refs, results, t["text"], mark)
    if t.get("not_found"):
        # Phase V: the domain agent names what its questions need ("a buyer, a file number such as FR-26-2493, …")
        what = view.slots_hint(t.get("agent") or "") or "an order number, factory or date"
        st.caption(f"Try one of the questions below, or add {what} to your question."
                   if t.get("follow_ups") else
                   f"Adding {what} to your question, or asking it another way, often helps.")
    with st.container(horizontal=True, key=f"actions_{cid}_{i}", vertical_alignment="center"):
        with st.popover("Copy", type="tertiary", icon=":material/content_copy:"):
            st.code(t["text"], language=None, wrap_lines=True)
        ui.csv_buttons(refs, results, f"{cid}_{i}")
        # not on rows reshaped from the previous answer or a reply about the conversation: re-asking "line wise"
        # or "you haven't replied?" with the cache bypassed would not read anything new
        if t.get("q") and t.get("kind") not in ("refine", "meta", "social") and (
                any(r.kind != "pdf" for r in refs) or (t.get("not_found") and t.get("route") in ("data", "both"))):
            # database answers may come from the short SQL result cache (their as-of time shows when the rows were
            # read); this re-asks the question with the cache bypassed — also on a not-found data answer (P2), so a
            # question whose records arrived since can be asked again live
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
        from .quota import gauge
        st.caption(f"route: {a.route}" + (f" · agent: {a.agent}" if a.agent else "")
                   + (f" · databases: {', '.join(a.databases_considered)}" if a.databases_considered else "")
                   + f" · tokens in/out: {a.usage.input_tokens}/{a.usage.output_tokens}"
                   + (f" · {tm['total']:.1f}s ({took})" if "total" in tm else "")
                   + (f" · request {a.request_id}" if a.request_id else "")
                   + f" · {gauge()}"
                   + (f" · {' | '.join(a.warnings)}" if a.warnings else ""))
    if latest:
        # Phase T6: the answers a clarifying question offers; a click is the reply, merged into the question asked
        ui.follow_ups(((t.get("clarify") or {}).get("choices") or [])[:8], f"cl_{cid}_{i}", ask, label="Choose one")
        # Phase T3: reshape this answer's rows (agent/refine.py) — the chip's text is sent as the next message,
        # so the transcript shows what was asked and the refine reader answers it without a model call
        ui.follow_ups((t.get("refine_suggestions") or [])[:5], f"rf_{cid}_{i}", ask, label="Refine")
        ui.follow_ups(t.get("follow_ups") or [], f"{cid}_{i}", ask,
                      label="Questions I can answer" if t.get("not_found")
                      else "Ask again" if t.get("route") in ("stopped", "meta") else "Follow up")


def conversation_head(head) -> None:
    """Title, scope line and Export; filled last, so Export already holds the answer given in this run."""
    hist_all = st.session_state.history
    owner = st.session_state.get("conv_owner") or ""
    saved = next((c["title"] for c in conversations.recent(owner, 50) if c["id"] == cid), "") if owner else ""
    title = saved or view.conversation_title(hist_all)
    with head:
        left, right = st.columns([5, 2], vertical_alignment="center")
        left.html(view.header_html(title, f"Scope: {scope_label} · Live data + Documents"))
        now = datetime.now()
        with right.container(horizontal=True, horizontal_alignment="right", key="convo_actions"):
            if owner and saved:
                # Phase U1: the conversation is kept on the server — it can be renamed or deleted here
                with st.popover("Rename", icon=":material/edit:", type="tertiary"):
                    st.text_input("Title", value=title, key="rename_box", max_chars=200)
                    st.button("Save", key=f"rename_{cid}", on_click=rename_current, type="primary")
                armed = st.session_state.get("confirm_delete")
                st.button("Delete?" if armed else "Delete", key=f"delete_{cid}", on_click=delete_current,
                          type="secondary" if armed else "tertiary", icon=":material/delete:",
                          help="Click again to delete this conversation" if armed else "Delete this conversation")
            st.download_button("Export", data=view.export_markdown(hist_all, title, f"Scope: {scope_label}", now),
                               file_name=f"conversation_{now:%Y%m%d_%H%M}.md", mime="text/markdown",
                               key=f"export_{cid}", disabled=not hist_all, icon=":material/download:")


def conversation(q: str | None, refresh: bool) -> None:
    hist_all = st.session_state.history
    stopped = view.dangling_question(hist_all)
    if stopped is not None:
        # Phase T8: the previous run ended before its answer (Stop, or a click on anything else while it was being
        # written). Say so, offer the question again, and keep a line in chat.csv.
        hist_all.append({"role": "assistant", "text": "Stopped before an answer was written.", "q": stopped,
                         "refs": [], "results": [], "follow_ups": [stopped], "request_id": "", "seconds": None,
                         "not_found": False, "error_kind": "", "scope": scope.key(), "route": "stopped",
                         "kind": "meta"})
        log_stopped(user, stopped, scope.key())
    head = st.container(key="convo_head")
    with st.container(key="convo_body"):         # keyed so style.css can let the rail wrap under a narrow thread
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
                ui.replay_progress(t.get("refs") or [], t.get("seconds"), t.get("kind") or "", _understood(t))
                answer_body(t, i, latest=(i == len(hist_all) - 1 and not q), shown=shown)
        if not q:
            _persist()                    # a stopped question recorded at the top of this run
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
                msg = "Something went wrong answering that. The team has been notified — please try again."
                st.error(msg)
                # a turn of its own, so the next run does not read the unanswered question as stopped
                st.session_state.history.append({"role": "assistant", "text": msg, "q": q, "refs": [], "results": [],
                                                 "error_kind": "other", "route": "error", "request_id": "",
                                                 "seconds": None, "not_found": True, "scope": scope.key()})
                _persist()
                conversation_head(head)
                st.stop()
            i = len(st.session_state.history)   # index this assistant turn will have in history
            turn = {"role": "assistant", "text": a.text, "refs": a.references, "q": q, "results": a.results,
                    "follow_ups": a.follow_ups, "request_id": a.request_id, "seconds": secs,
                    "not_found": a.not_found, "error_kind": a.error_kind, "scope": scope.key(), "route": a.route,
                    "rewritten": a.rewritten_question, "kind": a.kind, "refine_suggestions": a.refine_suggestions,
                    "clarify": a.clarify, "repeat_of": a.repeat_of, "prior_request_id": a.prior_request_id,
                    "agent": a.agent}
            prev = next((t for t in reversed(st.session_state.history)
                         if a.repeat_of and t.get("request_id") == a.repeat_of), None)
            if prev is not None:
                # Phase T7: "you haven't replied?" / "say that again" — the earlier answer, its sources and tables,
                # shown again as this turn (its own widget keys); its query is not run again
                turn.update(text=f"{a.text}\n\n{prev['text']}", refs=prev.get("refs") or [],
                            results=prev.get("results") or [], not_found=prev.get("not_found", False),
                            refine_suggestions=prev.get("refine_suggestions") or [])
            st.session_state.history.append(turn)
            st.session_state.src_turn = None
            answer_body(turn, i, latest=True, shown=i, a=a)
    with rail.container():
        ui.sources_rail(turn["refs"], f"{cid}_{i}", fresh, _pdf)
    _persist()                        # Phase U1: the new turns go to the server once they are shown
    conversation_head(head)


q = st.session_state.pending_q
refresh = bool(st.session_state.pending_refresh)
st.session_state.pending_q = None
st.session_state.pending_refresh = False

if st.session_state.history or q:
    conversation(q, refresh)
else:
    home()
