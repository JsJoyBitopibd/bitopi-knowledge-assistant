"""Pure pieces of the web UI (docs/design/DESIGN.md, screens 1 and 2): HTML fragments, labels and the factory
pick. No Streamlit import, so every function here is unit-testable; components.py renders what these return.
Every value that reaches HTML goes through esc(): answers, quotes, SQL and file names can hold any text."""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Any, Iterable, Optional, Sequence

from ..auth.models import Scope

ALL_FACTORIES = "All"
RING_CIRCUMFERENCE = 213.6          # 2π × r=34, the design's health ring


def esc(x: Any) -> str:
    return escape("" if x is None else str(x), quote=True)


# ── scope ─────────────────────────────────────────────────────────────────────────────────────────────────────

def narrow_scope(scope: Scope, pick: Optional[str]) -> Scope:
    """The user's scope limited to one factory (the sidebar's Factory pills). Never wider than `scope`: "All",
    no pick, or a factory the user may not see returns `scope` itself. Group-wide documents (SOPs, IT policy)
    stay visible because auth/filters.scope_where always adds the group-wide tag."""
    if not pick or pick == ALL_FACTORIES or not scope.allows_factory(pick):
        return scope
    return Scope(**{**scope.model_dump(), "factories": {pick}})


def factories_offered(scope: Scope, seen: Iterable[str], configured: Iterable[str]) -> list[str]:
    """Factory pills: the factories the watcher has data for, else the configured codes; only those in scope."""
    pool = {str(f).upper() for f in seen if f} or {str(f).upper() for f in configured if f}
    return sorted(f for f in pool if f not in ("*", "ALL") and scope.allows_factory(f))


def join_names(names: Sequence[str]) -> str:
    """'BGL, RHL and TAL'."""
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def scope_text(pick: Optional[str]) -> str:
    return pick if pick and pick != ALL_FACTORIES else "All factories"


# ── conversation ──────────────────────────────────────────────────────────────────────────────────────────────

def conversation_title(history: Sequence[dict], limit: int = 60) -> str:
    """The first question, on one line and trimmed at a word boundary."""
    first = next((t["text"] for t in history if t.get("role") == "user" and t.get("text")), "")
    first = " ".join(first.split())
    if not first:
        return "New conversation"
    if len(first) <= limit:
        return first
    return first[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


_MARKER = re.compile(r"\[([PD]\d+)\]")


def style_markers(text: str) -> str:
    """[P1] / [D1] markers as small badges in Streamlit markdown (the stored text keeps the plain markers). A badge
    directive must follow a space, so one is added after a word ("invoice[D1]" → "invoice :green-badge[D1]")."""
    def badge(m: re.Match) -> str:
        gap = " " if m.start() > 0 and not text[m.start() - 1].isspace() else ""
        return f"{gap}:green-badge[{m.group(1)}]"
    return _MARKER.sub(badge, text)


def read_list(refs: Sequence[Any]) -> list[str]:
    """What an answer was built from, one line per source: 'BitopiSplint · rag.vw_X · 432 rows', 'File.pdf p. 12'."""
    out = []
    for r in refs or []:
        if r.kind == "pdf":
            out.append(f"{r.source} p. {r.page}")
        else:
            rows = f" · {r.row_count:,} row{'s' if r.row_count != 1 else ''}" if r.row_count is not None else ""
            out.append(f"{r.database} · {', '.join(r.views or []) or r.tool}{rows}")
    return out


def done_label(refs: Sequence[Any], seconds: Optional[float] = None) -> str:
    """The progress line once an answer is done (the design's "✓ Queried rag.vw_X · 432 rows")."""
    data = [r for r in refs or [] if r.kind != "pdf"]
    pdfs = [r for r in refs or [] if r.kind == "pdf"]
    parts = []
    if data:
        first = data[0]
        view = ", ".join(first.views or []) or first.tool or first.database
        rows = f" · {first.row_count:,} row{'s' if first.row_count != 1 else ''}" if first.row_count is not None else ""
        more = f" + {len(data) - 1} more" if len(data) > 1 else ""
        parts.append(f"Queried {view}{rows}{more}")
    if pdfs:
        files = sorted({r.source for r in pdfs})
        pages = sorted({r.page for r in pdfs if r.source == files[0] and r.page is not None})
        where = f"{files[0]} p. {', '.join(map(str, pages))}" if pages else files[0]
        parts.append(f"Read {where}" + (f" + {len(files) - 1} more" if len(files) > 1 else ""))
    if not parts:
        parts.append("No source found")
    if seconds is not None:
        parts.append(f"{seconds:.1f} s")
    return " · ".join(parts)


def export_markdown(history: Sequence[dict], title: str, scope_label: str, when: datetime) -> str:
    """The conversation and every answer's references as Markdown (the header's Export button)."""
    lines = [f"# {title}", "", f"{scope_label} · exported {when:%d %b %Y %H:%M}", ""]
    for t in history:
        if t.get("role") == "user":
            lines += [f"## {t.get('text', '')}", ""]
            continue
        lines += [t.get("text", ""), ""]
        refs = t.get("refs") or []
        if refs:
            lines.append("**References**")
            lines.append("")
            for r in refs:
                if r.kind == "pdf":
                    lines.append(f"- [{r.marker}] {r.source} · {r.section or '—'} · page {r.page}"
                                 + (f" · “{r.quote}”" if r.quote else ""))
                else:
                    asof = f" · as of {r.as_of:%d %b %Y %H:%M}" if r.as_of else ""
                    lines.append(f"- [{r.marker}] {r.database} · {', '.join(r.views or [])} · "
                                 f"{r.row_count if r.row_count is not None else '?'} row(s){asof}")
                    if r.sql:
                        lines += ["", "  ```sql", *("  " + ln for ln in r.sql.strip().splitlines()), "  ```"]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ── freshness ─────────────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Fresh:
    label: str
    stamp: str        # for the sidebar's Data status card ("07 Oct 03:52", "17 days old")
    age: str          # for the Sources rail's Data freshness card ("today", "17 days")
    tone: str         # fresh | stale | plain


def _parse(iso: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None


def _age(then: datetime, now: datetime) -> str:
    days = (now.date() - then.date()).days
    return "today" if days <= 0 else "1 day" if days == 1 else f"{days} days"


def freshness(run: Optional[dict], data_signals: Sequence[Any], index_state: Optional[dict], now: datetime,
              stale_minutes: int = 180) -> list[Fresh]:
    """Order data = when the watcher last read the databases; one row per data-age check (e.g. line data), worst
    factory first; the document index. Times are shown as stored (the server's clock), like the rest of the app."""
    rows: list[Fresh] = []
    finished = _parse((run or {}).get("finished"))
    if finished:
        stale = (now - finished).total_seconds() > stale_minutes * 60
        rows.append(Fresh("Order data", f"{finished:%d %b %H:%M}", _age(finished, now), "stale" if stale else "fresh"))
    by_rule: dict[str, list[Any]] = {}
    for s in data_signals or []:
        by_rule.setdefault(s.rule, []).append(s)
    for rule, sigs in by_rule.items():
        worst = max(sigs, key=lambda s: (s.value or 0))
        label = rule.removesuffix("_age").replace("_", " ").capitalize()
        if len(sigs) == 1:
            label = f"{worst.factory} {label[0].lower()}{label[1:]}"
        if worst.value is not None and "age" in (worst.values or {}):
            n = int(worst.value)
            age = "today" if n <= 0 else "1 day" if n == 1 else f"{n} days"
            stamp = "today" if n <= 0 else f"{age} old"
        else:
            age = stamp = worst.title
        rows.append(Fresh(label, stamp, age, "stale" if worst.level in ("amber", "red") else "fresh"))
    if index_state:
        updated = _parse(index_state.get("index_updated_at") or index_state.get("finished_at"))
        n = index_state.get("documents")
        rows.append(Fresh("Document index", f"{updated:%d %b %H:%M}" if updated else "—",
                          f"{n} indexed · {updated:%d %b}" if updated and n is not None else "—", "plain"))
    return rows


# ── HTML pieces ───────────────────────────────────────────────────────────────────────────────────────────────

def brand_html(subtitle: str = "Bitopi Group · Pilot", light: bool = False) -> str:
    """The B mark and name; `light` = on the light page (the sign-in form) instead of the dark sidebar."""
    return (f'<div class="ka-brand{" ka-brand-light" if light else ""}"><div class="ka-logo">B</div><div class="ka-brand-text">'
            f'<span class="ka-brand-name">Knowledge Assistant</span><span class="ka-brand-sub">{esc(subtitle)}</span>'
            '</div></div>')


def label_html(text: str, extra_class: str = "") -> str:
    return f'<div class="ka-label {esc(extra_class)}">{esc(text)}</div>'


def search_in_html(databases: int, documents: Optional[int]) -> str:
    docs = f"{documents} indexed" if documents is not None else "none indexed"
    return ('<div class="ka-search-in">'
            f'<div class="ka-search-row"><span class="ka-tick">✓</span>Live data · {databases} database{"s" if databases != 1 else ""}</div>'
            f'<div class="ka-search-row"><span class="ka-tick">✓</span>Documents · {esc(docs)}</div>'
            '<div class="ka-search-note">The assistant picks the source for each question.</div></div>')


def data_status_html(rows: Sequence[Fresh], footer: str, note: str = "") -> str:
    tone = {"fresh": "ka-fresh", "stale": "ka-stale", "plain": ""}
    body = "".join(f'<div class="ka-status-row"><span>{esc(r.label)}</span>'
                   f'<span class="ka-mono {tone[r.tone]}">{esc(r.stamp)}</span></div>' for r in rows)
    if not rows:
        body = '<div class="ka-status-row"><span>No data checks yet</span></div>'
    extra = f'<div class="ka-status-note">{esc(note)}</div>' if note else ""
    return (f'<div class="ka-status" id="data-status"><div class="ka-status-title">Data status</div>{body}{extra}'
            f'<div class="ka-status-foot">{esc(footer)}</div></div>')


def recent_current_html(title: str) -> str:
    return f'<div class="ka-recent-current" title="{esc(title)}">{esc(title)}</div>'


def status_pill_html(text: str, ok: bool = True) -> str:
    return f'<span class="ka-pill-status"><span class="ka-dot {"" if ok else "ka-dot-warn"}"></span>{esc(text)}</span>'


def topline_html(date_text: str, status: str, ok: bool = True) -> str:
    return f'<div class="ka-topline"><span class="ka-date">{esc(date_text)}</span>{status_pill_html(status, ok)}</div>'


def greeting_html(hello: str, subtitle: str) -> str:
    return f'<div class="ka-greeting"><h1>{esc(hello)}</h1><p>{esc(subtitle)}</p></div>'


def svg_img(svg: str, size: int, css_class: str = "") -> str:
    """An inline SVG as an <img> data URI: Streamlit's HTML sanitizer removes <svg> elements but keeps images."""
    data = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return (f'<img class="{esc(css_class)}" src="data:image/svg+xml;base64,{data}" width="{size}" height="{size}" '
            'alt="" aria-hidden="true">')


_WARN_ICON = svg_img('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" '
                     'stroke="#6E3A06" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
                     '<path d="M12 3 2 20h20L12 3z"/><path d="M12 10v4M12 17h.01"/></svg>', 20, "ka-icon")


def banner_html(data_warnings: Sequence[Any], link: str = "#data-status") -> Optional[str]:
    """One amber banner for the data-age checks: 'Line data is 17 days old for BGL, RHL and TAL (last update …)'."""
    if not data_warnings:
        return None
    by_rule: dict[str, list[Any]] = {}
    for s in data_warnings:
        by_rule.setdefault(s.rule, []).append(s)
    parts = []
    for rule, sigs in by_rule.items():
        facts = join_names(sorted({s.factory for s in sigs}))
        worst = max(sigs, key=lambda s: (s.value or 0))
        vals = worst.values or {}
        if "age" in vals and worst.value is not None:
            label = rule.removesuffix("_age").replace("_", " ").capitalize()
            day = _parse(str(vals.get("day", "")))
            last = f" (last update {day:%d %b %Y})" if day else ""
            parts.append(f"<strong>{esc(label)} is {int(worst.value)} days old</strong> for {esc(facts)}{esc(last)}. "
                         "Health score and schedule figures may be out of date.")
        else:
            parts.append(f"<strong>{esc(worst.title)}</strong> ({esc(facts)}).")
    return (f'<div class="ka-banner" role="status">{_WARN_ICON}<p>{" ".join(parts)}</p>'
            f'<a href="{esc(link)}">View data status</a></div>')


def caution_html(text: str) -> str:
    return f'<div class="ka-caution">{_WARN_ICON}<p>{esc(text)}</p></div>'


def health_ring_svg(score: Optional[int]) -> str:
    """The design's ring: grey track, an arc for the score (red < 50, amber < 80, green from 80)."""
    track = '<circle cx="40" cy="40" r="34" fill="none" stroke="#ECEEEA" stroke-width="8"></circle>'
    arc = ""
    if score is not None:
        s = max(0, min(100, int(score)))
        colour = "#B42318" if s < 50 else "#D9822B" if s < 80 else "#1D7A45"
        dash = round(RING_CIRCUMFERENCE * s / 100, 1)
        arc = (f'<circle cx="40" cy="40" r="34" fill="none" stroke="{colour}" stroke-width="8" stroke-linecap="round" '
               f'stroke-dasharray="{dash} {round(RING_CIRCUMFERENCE - dash, 1)}" transform="rotate(-90 40 40)"></circle>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="76" height="76" viewBox="0 0 80 80">{track}{arc}</svg>'


def health_card_html(score: Optional[int], red: int, amber: int, green: int) -> str:
    number = f"{score}" if score is not None else "—"
    return ('<div class="ka-kpi"><span class="ka-kpi-label">Factory health</span>'
            f'<div class="ka-health">{svg_img(health_ring_svg(score), 76, "ka-ring")}<div class="ka-health-num"><span class="ka-kpi-value ka-kpi-value-sm">'
            f'{esc(number)}</span><span class="ka-kpi-note">out of 100</span></div></div>'
            f'<div class="ka-counts"><span><i class="ka-c-red"></i>{red} red</span><span><i class="ka-c-amber"></i>'
            f'{amber} amber</span><span><i class="ka-c-green"></i>{green} green</span></div></div>')


def kpi_html(label: str, value: float, note: str) -> str:
    return (f'<div class="ka-kpi"><span class="ka-kpi-label">{esc(label)}</span>'
            f'<span class="ka-kpi-value">{value:,.0f}</span><span class="ka-kpi-note">{esc(note)}</span></div>')


def badge_html(level: str) -> str:
    text, cls = {"red": ("Critical", "ka-badge-crit"), "amber": ("Watch", "ka-badge-watch")}.get(
        level, ("Info", "ka-badge-info"))
    return f'<span class="ka-badge {cls}">{text}</span>'


def attention_html(sig: Any, as_of: str) -> str:
    views = ", ".join(sig.views or [])
    meta = " · ".join(x for x in (sig.database, views, f"as of {as_of}" if as_of else "") if x)
    return (f'<div class="ka-attn">{badge_html(sig.level)}<div class="ka-attn-text">'
            f'<div class="ka-attn-title"><strong>{esc(sig.factory)}</strong> · {esc(sig.title)}</div>'
            f'<div class="ka-attn-meta ka-mono">{esc(meta)}</div></div></div>')


def section_head_html(title: str, aside: str = "") -> str:
    return f'<div class="ka-section-head"><h2>{esc(title)}</h2><span>{esc(aside)}</span></div>'


def user_bubble_html(text: str) -> str:
    return f'<div class="ka-user"><div class="ka-user-bubble">{esc(text)}</div></div>'


def header_html(title: str, scope_line: str) -> str:
    return f'<div class="ka-head"><h1>{esc(title)}</h1><span>{esc(scope_line)}</span></div>'


def _dl(rows: Sequence[tuple[str, str, bool]]) -> str:
    return '<dl class="ka-dl">' + "".join(
        f'<dt>{esc(k)}</dt><dd class="{"ka-mono" if mono else ""}">{esc(v)}</dd>' for k, v, mono in rows if v) + "</dl>"


def source_card_html(ref: Any) -> str:
    """One card in the Sources rail: a [D#] result set or a [P#] passage (docs/REFERENCE_FORMAT.md)."""
    n = esc(ref.marker)
    if ref.kind == "pdf":
        rows = [("Topic", ref.section or "—", False), ("Page", str(ref.page), False),
                ("Category", ref.category or "", False)]
        quote = f'<blockquote class="ka-quote">“{esc(ref.quote)}”</blockquote>' if ref.quote else ""
        return (f'<div class="ka-src-head"><span class="ka-src-n">{n}</span><span class="ka-src-kind">Document</span></div>'
                f'<span class="ka-src-name">{esc(ref.source)}</span>{_dl(rows)}{quote}')
    params = " · ".join(f"{k} {v}" for k, v in (ref.params or {}).items())
    rows = [("As of", f"{ref.as_of:%d %b %Y %H:%M}" if ref.as_of else "", False),
            ("Rows", f"{ref.row_count:,}" if ref.row_count is not None else "", False),
            ("Filter", params, False),
            ("Row key", "; ".join(ref.row_keys or []), False),
            ("Tool", ref.tool if ref.tool and ref.tool != "generated" else "", True)]
    engine = {"sqlserver": "SQL Server", "mysql": "MySQL", "local": "local copy"}.get(ref.engine or "", ref.engine or "")
    return (f'<div class="ka-src-head"><span class="ka-src-n">{n}</span><span class="ka-src-kind">Live data · '
            f'{esc(ref.database)}</span></div>'
            f'<span class="ka-src-name ka-mono">{esc(", ".join(ref.views or []))}</span>{_dl(rows)}'
            + (f'<span class="ka-src-engine">{esc(engine)}</span>' if engine else ""))


def freshness_card_html(rows: Sequence[Fresh]) -> str:
    dot = {"fresh": '<i class="ka-c-green"></i>', "stale": '<i class="ka-c-amber"></i>', "plain": ""}
    body = "".join(f'<div class="ka-fresh-row"><span>{esc(r.label)}</span><span class="ka-fresh-{r.tone}">'
                   f'{dot[r.tone]}{esc(r.age)}</span></div>' for r in rows)
    return f'<div class="ka-card ka-fresh-card"><span class="ka-card-title">Data freshness</span>{body}</div>'


def composer_meta_html(scope_label: str) -> str:
    return (f'<div class="ka-composer-meta"><span class="ka-chip">{esc(scope_label)} · Data + Documents</span>'
            '<span>Every answer shows its source</span></div>')
