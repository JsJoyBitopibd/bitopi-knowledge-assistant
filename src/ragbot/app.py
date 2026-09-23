"""Streamlit pilot UI. Run: streamlit run src/ragbot/app.py

Shows the answer with [P#]/[D#] markers, a References panel (PDF file/topic/page/quote or
database/view/row keys/SQL), and thumbs up/down logged to logs/chat.csv (docs/PLAN.md M5).
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from ragbot.agent.orchestrator import answer, log_feedback  # noqa: E402
from ragbot.config import log_dir, settings  # noqa: E402
from ragbot.index_version import index_version  # noqa: E402
from ragbot.store import Registry  # noqa: E402

s = settings()
st.set_page_config(page_title=s["ui.title"], layout="centered")
st.title(s["ui.title"])


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
        load_catalogs()
    except Exception:
        pass  # DB catalogs are optional for document-only pilots
    return True


with st.status("Loading models…", expanded=False) as _status:
    _warm()
    _status.update(label="Ready", state="complete")


@st.cache_data(ttl=300)
def _categories(_version: int) -> list[str]:
    # _version (ingest_state.json mtime) keys the cache so a newly ingested category appears here.
    try:
        rows = Registry().db.execute("SELECT DISTINCT category FROM document WHERE status='ok' ORDER BY category")
        cats = [r[0] for r in rows if r[0]]
        return cats or ["General"]
    except Exception:
        return ["General"]


@st.cache_data(ttl=300)
def _index_caption(_version: int) -> str:
    import json
    try:
        d = json.loads((s.path("index_dir") / "ingest_state.json").read_text(encoding="utf-8"))
        return f"Index updated {d.get('finished_at', '?')} · {d.get('documents', '?')} documents"
    except Exception:
        return "No documents indexed yet. Add PDFs to data/pdfs/<Category>/."


@st.cache_data(ttl=600)
def _pdf_bytes(source: str, _version: int) -> bytes | None:
    """Read a source PDF's bytes for the download button, cached so repeated renders don't re-read."""
    for p in s.path("pdf_root").rglob(source):
        try:
            return p.read_bytes()
        except OSError:
            return None
    return None


ver = index_version()

if "history" not in st.session_state:
    st.session_state.history = []      # [{"role","text","refs"}]
if "pending_q" not in st.session_state:
    st.session_state.pending_q = None

with st.sidebar:
    user = st.text_input("Your name", value="")
    cats = st.multiselect("Document categories (empty = all)", _categories(ver))
    admin = st.toggle("Admin details", value=False, help="Show routing and token counts under each answer")
    if st.button("Clear chat"):
        st.session_state.history = []
        st.rerun()
    st.caption(_index_caption(ver))


def feedback(i: int) -> None:
    val = st.session_state.get(f"fb{i}")
    turn = st.session_state.history[i]
    log_feedback(user, i, val, turn["text"])


def show_refs(refs) -> None:
    if not refs:
        return
    with st.expander(f"References ({len(refs)})", expanded=True):
        for r in refs:
            if r.kind == "pdf":
                st.markdown(f"**[{r.marker}] {r.source}** · Topic: {r.section} · Page {r.page} · Category: {r.category}")
                st.caption(f"“{r.quote}”")
                data = _pdf_bytes(r.source, ver)
                if data:
                    st.download_button("Open PDF", data=data, file_name=r.source,
                                       key=f"dl{r.marker}{r.chunk_id}", mime="application/pdf")
            else:
                asof = r.as_of.strftime("%d %b %Y %H:%M") if r.as_of else ""
                st.markdown(f"**[{r.marker}] {r.database} ({r.engine}) · {', '.join(r.views or [])}**  \n"
                            f"Row key: {'; '.join(r.row_keys or []) or '—'} · {r.row_count} row(s) · "
                            f"as of {asof} · tool: {r.tool}")
                with st.expander("show SQL"):
                    st.code(r.sql or "", language="sql")
                    if r.params:
                        st.json(r.params)


def render_answer(a) -> None:
    if a.error_kind:
        st.warning(a.text)   # a friendly quota/timeout/auth message, not a normal answer
        return
    st.markdown(a.text)
    if a.not_found:
        st.caption("No source in the system covers this question.")
    show_refs(a.references)
    if admin:
        st.caption(f"route: {a.route} · tokens in/out: {a.usage.input_tokens}/{a.usage.output_tokens}"
                   + (f" · {' | '.join(a.warnings)}" if a.warnings else ""))


for i, t in enumerate(st.session_state.history):
    with st.chat_message(t["role"]):
        st.markdown(t["text"])
        if t["role"] == "assistant":
            show_refs(t.get("refs"))
            st.feedback("thumbs", key=f"fb{i}", on_change=feedback, args=(i,))

# Suggested-question chips on an empty conversation.
if not st.session_state.history:
    picks = s.get("ui.suggestions", []) or []
    if picks:
        st.caption("Try one of these:")
        cols = st.columns(min(len(picks), 2))
        for j, sug in enumerate(picks):
            if cols[j % len(cols)].button(sug, key=f"sug{j}"):
                st.session_state.pending_q = sug
                st.rerun()

typed = st.chat_input("Ask about export orders, PCDs, PPM meetings, SOPs, IT policies…")
q = typed or st.session_state.pending_q
st.session_state.pending_q = None

if q:
    st.session_state.history.append({"role": "user", "text": q})
    with st.chat_message("user"):
        st.markdown(q)
    hist = [{"role": t["role"], "text": t["text"]} for t in st.session_state.history[:-1]]
    where = {"category": cats} if cats else None
    with st.chat_message("assistant"):
        with st.spinner("Searching documents and data…"):
            try:
                a = answer(q, history=hist, where=where, user=user)
            except Exception as e:  # last-resort guard: never show a raw traceback to a user
                with open(log_dir() / "errors.log", "a", encoding="utf-8") as fh:
                    fh.write(f"\n=== {datetime.now().isoformat()} q={q!r}\n{traceback.format_exc()}")
                st.error("Something went wrong answering that. The team has been notified — please try again.")
                st.stop()
        render_answer(a)
    st.session_state.history.append({"role": "assistant", "text": a.text, "refs": a.references})
    st.rerun()
