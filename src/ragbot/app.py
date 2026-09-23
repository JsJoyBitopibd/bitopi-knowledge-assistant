"""Streamlit pilot UI. Run: streamlit run src/ragbot/app.py

Shows the answer with [P#]/[D#] markers, a References panel (PDF file/topic/page/quote or
database/view/row keys/SQL), and thumbs up/down logged to logs/chat.csv (docs/PLAN.md M5).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from ragbot.agent.orchestrator import answer, log_feedback  # noqa: E402
from ragbot.config import settings  # noqa: E402
from ragbot.store import Registry  # noqa: E402

s = settings()
st.set_page_config(page_title=s["ui.title"], layout="centered")
st.title(s["ui.title"])


def _categories() -> list[str]:
    try:
        rows = Registry().db.execute("SELECT DISTINCT category FROM document WHERE status='ok' ORDER BY category")
        cats = [r[0] for r in rows if r[0]]
        return cats or ["General"]
    except Exception:
        return ["General"]


with st.sidebar:
    user = st.text_input("Your name", value="")
    cats = st.multiselect("Document categories (empty = all)", _categories())
    st.caption("PDFs live in data/pdfs/<Category>/. Run `python scripts/ingest.py` after adding files.")

if "history" not in st.session_state:
    st.session_state.history = []      # [{"role","text","refs"}]


def feedback(i: int) -> None:
    val = st.session_state.get(f"fb{i}")
    turn = st.session_state.history[i]
    log_feedback(user, i, val, turn["text"])


def _pdf_link(source: str) -> str:
    pdf_root = s.path("pdf_root")
    for p in pdf_root.rglob(source):
        return p.resolve().as_uri()
    return ""


def show_refs(refs) -> None:
    if not refs:
        return
    with st.expander(f"References ({len(refs)})", expanded=True):
        for r in refs:
            if r.kind == "pdf":
                link = _pdf_link(r.source)
                title = f"[{r.source}]({link}#page={r.page})" if link else r.source
                st.markdown(f"**[{r.marker}] {title}**  \nTopic: {r.section} · Page {r.page} · Category: {r.category}")
                st.caption(f"“{r.quote}”")
            else:
                asof = r.as_of.strftime("%d %b %Y %H:%M") if r.as_of else ""
                st.markdown(f"**[{r.marker}] {r.database} ({r.engine}) · {', '.join(r.views or [])}**  \n"
                            f"Row key: {'; '.join(r.row_keys or []) or '—'} · {r.row_count} row(s) · "
                            f"as of {asof} · tool: {r.tool}")
                with st.expander("show SQL"):
                    st.code(r.sql or "", language="sql")
                    if r.params:
                        st.json(r.params)


for i, t in enumerate(st.session_state.history):
    with st.chat_message(t["role"]):
        st.markdown(t["text"])
        if t["role"] == "assistant":
            show_refs(t.get("refs"))
            st.feedback("thumbs", key=f"fb{i}", on_change=feedback, args=(i,))

if q := st.chat_input("Ask about export orders, PCDs, PPM meetings, SOPs, IT policies…"):
    st.session_state.history.append({"role": "user", "text": q})
    with st.chat_message("user"):
        st.markdown(q)
    hist = [{"role": t["role"], "text": t["text"]} for t in st.session_state.history[:-1]]
    where = {"category": cats} if cats else None
    with st.chat_message("assistant"):
        with st.spinner("Searching documents and data…"):
            a = answer(q, history=hist, where=where, user=user)
        st.markdown(a.text)
        if a.not_found:
            st.caption("No source in the system covers this question.")
        show_refs(a.references)
        st.caption(f"route: {a.route} · tokens in/out: {a.usage.input_tokens}/{a.usage.output_tokens}")
    st.session_state.history.append({"role": "assistant", "text": a.text, "refs": a.references})
    st.rerun()
