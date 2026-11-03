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


# ── tables in an answer ───────────────────────────────────────────────────────────────────────────────────────

_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_PIPE = re.compile(r"(?<!\\)\|")
_NUM = re.compile(r"^-?(\d{1,3}(,\d{3})+|\d+)?(\.\d+)?%?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?$")


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in _PIPE.split(s)]


def split_md_tables(text: str) -> list[tuple[str, Any]]:
    """The answer as ("text", markdown) and ("table", (header, rows)) pieces, in order, so each Markdown table can
    be drawn as a sortable table. A run of '|' lines counts as a table only when its second line is a separator."""
    lines = (text or "").splitlines()
    out: list[tuple[str, Any]] = []
    buf: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1]):
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            if buf:
                out.append(("text", "\n".join(buf)))
                buf = []
            header = _cells(lines[i])
            rows = [(_cells(ln) + [""] * len(header))[:len(header)] for ln in lines[i + 2:j]]
            out.append(("table", (header, rows)))
            i = j
            continue
        buf.append(lines[i])
        i += 1
    if buf:
        out.append(("text", "\n".join(buf)))
    return out


PREVIEW_NOTE = re.compile(r"\s*Only the first rows are shown here; the query returned \d+ in total\.")


def full_rows(header: Sequence[str], rows: Sequence[Sequence[str]],
              results: Sequence[tuple[Sequence[str], Sequence[Sequence[str]]]]) -> Optional[list[list[str]]]:
    """All rows of the result an answer's table previews (agent/templated.py writes the first 25), so sorting
    covers every row the query returned: same columns, and the table's rows are the result's first rows, both as
    the answer formats them. None when the table is not such a preview (e.g. a table the model wrote)."""
    want = [[_plain(c) for c in r] for r in rows]
    for columns, formatted in results:
        if list(columns) == [_plain(h) for h in header] and len(formatted) > len(rows) \
                and [list(r) for r in formatted[:len(rows)]] == want:
            return [list(r) for r in formatted]
    return None


def column_kinds(rows: Sequence[Sequence[str]], ncols: int) -> list[str]:
    """num | date | text per column, from the non-blank cells ('—' is the templated answers' empty value)."""
    kinds = []
    for c in range(ncols):
        vals = [_plain(r[c]) for r in rows if c < len(r) and _plain(r[c]) not in ("", "—")]
        if vals and all(_NUM.match(v) and any(ch.isdigit() for ch in v) for v in vals):
            kinds.append("num")
        elif vals and all(_DATE.match(v) for v in vals):
            kinds.append("date")
        else:
            kinds.append("text")
    return kinds


def _plain(cell: Any) -> str:
    """Cell text without Markdown emphasis or code marks."""
    s = "" if cell is None else str(cell).strip()
    for mark in ("**", "__", "`"):
        if s.startswith(mark) and s.endswith(mark) and len(s) > 2 * len(mark):
            s = s[len(mark):-len(mark)].strip()
    return s


_SPLIT_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def humanize(col: Any) -> str:
    """A column name as a heading: 'ShipDate' -> 'Ship date', 'ExportOrderID' -> 'Export order ID', 'order_qty' ->
    'Order qty'; acronyms ('DHU', 'PCD') and names that already read as words ('Total OrderQty') are kept (Phase T8).
    The real name stays on the column's sort button, so a reader can still match it to Show query."""
    s = _plain(col)
    if not s or " " in s or (s.isupper() and len(s) <= 5):
        return s
    words = [w for part in re.split(r"[_\s]+", s) for w in _SPLIT_CAMEL.split(part) if w]
    if len(words) <= 1:
        return s[:1].upper() + s[1:]
    out = [w if (w.isupper() and len(w) > 1) else w.lower() for w in words]
    return out[0][:1].upper() + out[0][1:] + " " + " ".join(out[1:])


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def display_date(text: str) -> str:
    """'2026-10-12' -> '12 Oct 2026', '2026-10-12 09:15' -> '12 Oct 2026 09:15' (CLAUDE.md: dates are shown that way;
    the ISO text stays in the answer, Copy, Export and the cell's data-v for sorting)."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?: (\d{2}:\d{2})(?::\d{2})?)?$", text or "")
    if not m or not 1 <= int(m[2]) <= 12:
        return text
    return f"{int(m[3])} {_MONTHS[int(m[2]) - 1]} {m[1]}" + (f" {m[4]}" if m[4] else "")


def sortable_table_html(header: Sequence[str], rows: Sequence[Sequence[Any]], mark: Iterable[str] = ()) -> str:
    """A table whose column names are sort buttons (ui/sort_table.js does the sorting in the browser). Numbers sit
    right-aligned; a 'Total' row keeps its place at the bottom; every cell is escaped. Phase T8: headings read as
    words with the real name on the button's title; dates show as '12 Oct 2026' and sort by their ISO `data-v`; a
    row holding one of `mark` (the records the question names) is highlighted."""
    kinds = column_kinds(rows, len(header))
    num = ' class="ka-num"'
    head = "".join(
        f'<th scope="col" data-kind="{k}" data-col="{esc(_plain(h))}"{num if k == "num" else ""}>'
        f'<button type="button" class="ka-sort" title="Sort by {esc(_plain(h))}">{esc(humanize(h))}'
        '<span class="ka-sort-ic" aria-hidden="true"></span></button></th>'
        for h, k in zip(header, kinds))
    marks = {str(m).strip().casefold() for m in mark if str(m).strip()}

    def cell(v: Any, k: str) -> str:
        text = _plain(v)
        if k == "date":
            return f'<td data-v="{esc(text)}">{esc(display_date(text))}</td>'
        return f'<td{num if k == "num" else ""}>{esc(text)}</td>'

    body = []
    for i, r in enumerate(rows):
        classes = ["ka-total"] if r and _plain(r[0]).lower() == "total" else []
        if marks and any(_plain(v).casefold() in marks for v in r):
            classes.append("ka-hit")
        cls = f' class="{" ".join(classes)}"' if classes else ""
        body.append(f'<tr data-i="{i}"{cls}>{"".join(cell(v, k) for v, k in zip(r, kinds))}</tr>')
    return (f'<div class="ka-table-wrap"><table class="ka-sortable"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


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


def done_label(refs: Sequence[Any], seconds: Optional[float] = None, kind: str = "", understood: str = "") -> str:
    """The progress line once an answer is done (the design's "✓ Queried rag.vw_X · 432 rows"). Phase T8: rows
    reshaped from the previous answer say so ("Refined the previous result · 5 of 143 rows"), a page says which,
    a reply that needed no source says that instead of "No source found", and a question completed from a reply to
    a clarifying question says how it was understood."""
    data = [r for r in refs or [] if r.kind != "pdf"]
    pdfs = [r for r in refs or [] if r.kind == "pdf"]
    parts = [f"Understood as “{understood}”"] if understood else []
    if data:
        first = data[0]
        view = ", ".join(first.views or []) or first.tool or first.database
        n = first.row_count
        rows = f" · {n:,} row{'s' if n != 1 else ''}" if n is not None else ""
        more = f" + {len(data) - 1} more" if len(data) > 1 else ""
        transform = getattr(first, "transform", None) or ""
        if transform.startswith("page "):
            parts.append(f"Read the next page of {view}{rows}{more}")
        elif transform:
            src = getattr(first, "source_rows", None)
            of = f" · {n:,} of {src:,} rows" if n is not None and src else rows
            parts.append(f"Refined the previous result{of} · no new query")
        else:
            parts.append(f"Queried {view}{rows}{more}")
    if pdfs:
        files = sorted({r.source for r in pdfs})
        pages = sorted({r.page for r in pdfs if r.source == files[0] and r.page is not None})
        where = f"{files[0]} p. {', '.join(map(str, pages))}" if pages else files[0]
        parts.append(f"Read {where}" + (f" + {len(files) - 1} more" if len(files) > 1 else ""))
    if not data and not pdfs:
        parts.append("No database or document needed" if kind in ("social", "meta", "clarify") else "No source found")
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
                    derived = f" · derived from the previous answer: {r.transform}" \
                        if getattr(r, "transform", None) and not str(r.transform).startswith("page ") else ""
                    lines.append(f"- [{r.marker}] {r.database} · {', '.join(r.views or [])} · "
                                 f"{r.row_count if r.row_count is not None else '?'} row(s){asof}{derived}")
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
    # `*_label` parameters exist only to fill an answer's wording ("TAL ", "at company 02"); the card shows
    # what the query filtered on, so they would read as duplicates of the value beside them.
    params = " · ".join(f"{k} {v}" for k, v in (ref.params or {}).items()
                        if not str(k).endswith("_label") and v not in (None, ""))
    # Phase T8: rows reshaped from the previous answer keep that query's card, and say what was done to them
    transform = getattr(ref, "transform", None) or ""
    derived = bool(transform) and not transform.startswith("page ")
    src = getattr(ref, "source_rows", None)
    count = (f"{ref.row_count:,} of {src:,}" if derived and src and ref.row_count is not None
             else f"{ref.row_count:,}" if ref.row_count is not None else "")
    rows = [("As of", f"{ref.as_of:%d %b %Y %H:%M}" if ref.as_of else "", False),
            ("Changed", transform if derived else "", False),
            ("Page", transform[5:] if transform.startswith("page ") else "", False),
            ("Rows", count, False),
            ("Filter", params, False),
            ("Row key", "; ".join(ref.row_keys or []), False),
            ("Tool", ref.tool if ref.tool and ref.tool != "generated" else "", True)]
    engine = {"sqlserver": "SQL Server", "mysql": "MySQL", "local": "local copy"}.get(ref.engine or "", ref.engine or "")
    if derived:
        engine = "no new query: the rows of the previous answer"
    kind = "Derived from the previous answer · " if derived else "Live data · "
    return (f'<div class="ka-src-head"><span class="ka-src-n">{n}</span><span class="ka-src-kind">{kind}'
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


def risk_html(r: Any) -> Optional[str]:
    """The Stage-2 risk line: counts, the worst few files, and the caveat when a red is provisional.

    The caveat is rendered from the row, not from the rules file, so a level read back later still
    carries the reason its red is not a judgement. Everything is escaped: a file number, buyer or style
    comes from the database and reaches the page as text (Phase U1 — Streamlit's sanitizer drops a
    <style> holding "<" + a letter and drops <svg>, so nothing here builds markup from data).
    """
    if r is None or not r.total:
        return None
    parts = [f'<div class="ka-risk"><div class="ka-risk-head">'
             f'<span class="ka-risk-title">Quality risk</span>'
             f'<span class="ka-risk-count">{r.red:,} of {r.total:,} running files need attention</span></div>']
    parts.append('<div class="ka-risk-counts">'
                 + " · ".join(f'{n:,} {esc(lv)}' for lv, n in
                              (("critical", r.red), ("watch", r.amber), ("on target", r.green),
                               ("too few checked", r.info)) if n)
                 + "</div>")
    for a in r.worst:
        dhu = next((m for m in a.measures if m.measure == "dhu"), None)
        detail = f"DHU {dhu.value}" if dhu is not None and dhu.value is not None else "not enough checked"
        parts.append(f'<div class="ka-risk-row">{badge_html(a.level)}'
                     f'<span class="ka-risk-subject">{esc(a.factory)} · {esc(a.subject)}</span>'
                     f'<span class="ka-risk-detail">{esc(detail)} over {int(a.window_days)} days, '
                     f'{float(a.totals.get("Checked", 0) or 0):,.0f} pieces checked</span></div>')
    if r.caveat:
        parts.append(f'<div class="ka-risk-caveat">{esc(r.caveat)}</div>')
    parts.append("</div>")
    return "".join(parts)


def dangling_question(history: Sequence[dict]) -> Optional[str]:
    """The question of a turn that never got its answer — the last turn is the user's, because Stop (or a click on
    anything else) restarted the page while the answer was being written (Phase T8). None when every question has
    its answer."""
    if history and history[-1].get("role") == "user":
        return str(history[-1].get("text") or "")
    return None


def unit_line_html(factory: str, unit_id: str = "", company: str = "") -> str:
    """'Your factory: TAL · Unit 1 · TARASIMA APPARELS LTD.' under the greeting (Phase U3), from the sign-in's identity."""
    parts = [f"Your factory: {factory}"] + ([f"Unit {unit_id}"] if unit_id else []) + ([company] if company else [])
    return f'<div class="ka-unit-line">{esc(" · ".join(parts))}</div>'


def ago(then_iso: str, now: datetime) -> str:
    """'just now', '25 minutes ago', '3 hours ago', 'yesterday', '12 Oct 2026'."""
    try:
        then = datetime.fromisoformat(str(then_iso))
    except ValueError:
        return ""
    secs = (now - then).total_seconds()
    if secs < 120:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} minutes ago"
    if secs < 86400 and then.date() == now.date():
        return f"{int(secs // 3600)} hour{'s' if secs >= 7200 else ''} ago"
    if (now.date() - then.date()).days == 1:
        return "yesterday"
    return f"{then.day} {then:%b %Y}"


def slots_hint(agent: str) -> str:
    """Phase V: what a not-found caption tells the user to add, in the domain agent's own words (its YAML's
    `slots_hint:`, e.g. "a buyer, a file number such as FR-26-2493, a lot, or a date"); "" for none."""
    if not agent:
        return ""
    try:
        from ..domain_agents import load_agents
        a = load_agents().get(agent)
        return str(a.extra.get("slots_hint") or "").strip() if a is not None else ""
    except Exception:
        return ""
