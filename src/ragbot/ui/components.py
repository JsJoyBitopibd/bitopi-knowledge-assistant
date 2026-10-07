"""Streamlit renderers for the web UI (docs/design/DESIGN.md, screens 1 and 2). They draw what view.py builds;
app.py owns the session state and passes callbacks in (`ask(question)` and the like), so nothing here decides
what a user may see: the scope is applied before any data reaches these functions."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import streamlit as st

from ..data import present
from . import view

_CSS = Path(__file__).with_name("style.css")
STATIC = Path(__file__).resolve().parents[1] / "static"
AVATAR = str(STATIC / "avatar-b.svg")
DISCLAIMER = ("Answers come only from the connected databases and the indexed documents. "
              "Numbers are read straight from the source.")


def inject_css() -> None:
    """The stylesheet, comments removed: the frontend's HTML sanitizer drops a <style> whose text contains '<'
    followed by a letter (as a comment easily does), and the comments are not needed in the browser."""
    css = re.sub(r"/\*.*?\*/", "", _CSS.read_text(encoding="utf-8"), flags=re.S)
    st.html(f"<style>{css}</style>")       # style only: Streamlit puts it where it takes no space


def label(text: str, main: bool = False) -> None:
    st.html(view.label_html(text, "ka-label-main" if main else ""))


def composer(placeholder: str, scope_label: str, on_submit: Callable[[], None]) -> None:
    """The input card: Streamlit's chat input inside a keyed container (so it sits inline, under the thread),
    then the scope chip line."""
    with st.container(key="composer"):
        st.chat_input(placeholder, key="ask_box", on_submit=on_submit)
        st.html(view.composer_meta_html(scope_label))


# ── home briefing ─────────────────────────────────────────────────────────────────────────────────────────────

def _metric_question(signals: Sequence[Any], one_factory: bool) -> str:
    """The question behind a KPI card. A watch rule's question names its factory ('Which TAL orders are behind
    schedule?'); over several factories the name is dropped so the answer covers every factory in scope."""
    top = max(signals, key=lambda s: (s.value or 0))
    q = top.question or ""
    return q if one_factory else q.replace(f"{top.factory} ", "")


def kpi_cards(brief: Any, signals: Sequence[Any], ask: Callable[[str], None]) -> None:
    by_metric: dict[str, list[Any]] = {}
    for s in signals:
        if s.metric and s.value is not None:
            by_metric.setdefault(s.metric, []).append(s)
    cards = 1 + len(brief.metrics)
    per_row = min(4, cards)
    cols = st.columns(per_row, gap="small")
    with cols[0].container(key="kpi_health"):
        h = brief.health
        st.html(view.health_card_html(h.score, h.red, h.amber, h.green))
        with st.popover("How it's calculated"):
            st.caption(h.how)
    for j, (name, total) in enumerate(brief.metrics, start=1):
        if j % per_row == 0:
            cols = st.columns(per_row, gap="small")
        sigs = by_metric.get(name, [])
        facts = sorted({s.factory for s in sigs})
        note = f"Across {view.join_names(facts)}" if len(facts) > 1 else (f"{facts[0]} only" if facts else "")
        with cols[j % per_row].container(key=f"kpi_{j}"):
            st.html(view.kpi_html(name, total, note))
            q = _metric_question(sigs, len(facts) == 1) if sigs else ""
            if q:
                st.button(q, key=f"ask_kpi_{j}", type="tertiary", on_click=ask, args=(q,))


def attention_list(items: Sequence[Any], total: int, when: Callable[[str], str], ask: Callable[[str], None]) -> None:
    st.html(view.section_head_html("Needs your attention",
                                   f"{total} item{'s' if total != 1 else ''} · sorted by severity"))
    for k, sig in enumerate(items):
        with st.container(key=f"attn_{k}"):
            left, right = st.columns([5, 1.25], vertical_alignment="center")
            left.html(view.attention_html(sig, when(sig.as_of)))
            if sig.question:
                right.button("Ask about this", key=f"ask_attn_{k}", type="primary" if k == 0 else "secondary",
                             help=sig.question, on_click=ask, args=(sig.question,), width="stretch")


def suggestion_pills(title: str, picks: Sequence[str], ask: Callable[[str], None], key: str = "try") -> None:
    if not picks:
        return
    if title:
        st.html(view.section_head_html(title))
    with st.container(horizontal=True, key=key):
        for j, sug in enumerate(picks):
            st.button(sug, key=f"{key}_q{j}", on_click=ask, args=(sug,))


# ── conversation ──────────────────────────────────────────────────────────────────────────────────────────────

def progress_done(status: Any, refs: Sequence[Any], seconds: Optional[float]) -> None:
    """Finish a progress line: the design's "✓ Queried rag.vw_X · 432 rows", listing what was read inside."""
    status.update(label=view.done_label(refs, seconds), state="complete", expanded=False)
    reads = view.read_list(refs)
    if reads:
        with status:
            st.html('<div class="ka-read">' + "".join(f"<span>{view.esc(x)}</span>" for x in reads) + "</div>")


def replay_progress(refs: Sequence[Any], seconds: Optional[float]) -> None:
    progress_done(st.status(view.done_label(refs, seconds), state="complete", expanded=False), refs, seconds)


def _has_table(text: str) -> bool:
    return any(line.lstrip().startswith("|") for line in (text or "").splitlines())


def result_tables(refs: Sequence[Any], results: Sequence[Any], answer_text: str = "") -> None:
    """C8: the rows behind each [D#] reference, in the answer: sortable table, and a bar chart for label → number.
    A templated answer already carries the rows as a table in its text; the sortable copy then waits in a
    collapsed section instead of repeating them."""
    in_text = _has_table(answer_text)
    for r in refs or []:
        if r.kind == "pdf":
            continue
        k = int(r.marker[1:]) - 1
        if not (0 <= k < len(results)) or not results[k].rows:
            continue
        res = results[k]
        df = present.frame(res)
        chart = present.chart_columns(res)
        if chart:
            st.bar_chart(df.set_index(chart[0])[chart[1]], color="#0E5E50")
        if in_text:
            with st.expander(f"Sortable table · {len(res.rows):,} row{'s' if len(res.rows) != 1 else ''}"):
                st.dataframe(df, hide_index=True, width="stretch")
        else:
            st.dataframe(df, hide_index=True, width="stretch")
        st.caption(present.caption(res))


def csv_buttons(refs: Sequence[Any], results: Sequence[Any], key: str) -> None:
    for r in refs or []:
        if r.kind == "pdf":
            continue
        k = int(r.marker[1:]) - 1
        if 0 <= k < len(results) and results[k].rows:
            res = results[k]
            n = len(res.rows)
            st.download_button(f"Download {n:,} row{'s' if n != 1 else ''} (CSV)", data=present.csv_bytes(res),
                               file_name=present.csv_name(res), mime="text/csv", key=f"{key}_csv{r.marker}",
                               type="tertiary")


def follow_ups(suggestions: Sequence[str], key: str, ask: Callable[[str], None]) -> None:
    """G3: next questions under the latest fixed-tool answer; a click asks it like a suggestion."""
    if not suggestions:
        return
    st.html('<div class="ka-followup-label">Follow up</div>')
    with st.container(horizontal=True, key=f"fu_{key}"):
        for j, sug in enumerate(suggestions):
            st.button(sug, key=f"fub_{key}_{j}", on_click=ask, args=(sug,))


def rail_waiting(sources: Sequence[str] = ()) -> None:
    st.html('<h2 class="ka-rail-title">Sources</h2>')
    if sources:
        st.html('<div class="ka-card"><span class="ka-card-title">Reading</span><div class="ka-read">'
                + "".join(f"<span>{view.esc(x)}</span>" for x in sources) + "</div></div>")
    else:
        st.html('<div class="ka-rail-empty">The sources appear here as soon as the search is done.</div>')


def sources_rail(refs: Sequence[Any], key: str, fresh: Sequence[view.Fresh],
                 pdf_bytes: Callable[[str], Optional[bytes]]) -> None:
    """One card per reference ([D#] result set or [P#] passage), then Data freshness and the disclaimer."""
    st.html('<h2 class="ka-rail-title">Sources</h2>')
    if not refs:
        st.html('<div class="ka-rail-empty">This answer cites no source.</div>')
    for r in refs or []:
        with st.container(key=f"src_{key}_{r.marker}"):
            st.html(view.source_card_html(r))
            if r.kind == "pdf":
                data = pdf_bytes(r.source)
                if data:
                    st.download_button("Open PDF", data=data, file_name=r.source, mime="application/pdf",
                                       key=f"dl_{key}_{r.marker}_{r.chunk_id}", icon=":material/description:")
            else:
                with st.expander("Show query"):
                    st.code(r.sql or "", language="sql", wrap_lines=True)
                    if r.params:
                        st.json(r.params)
    if fresh:
        st.html(view.freshness_card_html(fresh))
    st.html(f'<p class="ka-disclaimer">{view.esc(DISCLAIMER)}</p>')
