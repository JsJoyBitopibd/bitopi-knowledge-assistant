"""Build the <sources> block, and verify the answer against it (docs/REFERENCE_FORMAT.md rules).

verify() returns (ok, problems). Callers regenerate once on failure, then return not-found.
normalize_markers() rewrites the marker variants models produce ("[P1, P2]", "\\[P1\\]", "[P1-P3]")
into the canonical "[P1][P2]" form before verification and display.
"""
from __future__ import annotations

import re
from typing import Any

from ..models import Chunk, QueryResult, Reference

_MARK = re.compile(r"\[([PD]\d+)\]")
_NUM = re.compile(r"(?<![\w-])\d[\d,\.]*(?![\w-])")
_CODE = re.compile(r"\b[A-Z]{2,}-?\d{2,}(?:-\d+)?\b")
_ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_DIGIT_RUN = re.compile(r"\d+")

# marker variants -> canonical
_ESC = re.compile(r"\\([\[\]])")                                                       # \[P1\]
_RANGE = re.compile(r"\[\s*([PD])\s*(\d+)\s*[-–]\s*(?:([PD])\s*)?(\d+)\s*\]", re.I)     # [P1-P3], [P1–3]
_GROUP = re.compile(r"\[\s*((?:[PD]\s*\d+)(?:\s*(?:,|;|/|&|\+|and)\s*(?:[PD]\s*\d+))+)\s*\]", re.I)  # [P1, P2] [P1; D1]
_SINGLE = re.compile(r"\[\s*([PD])\s*(\d+)\s*\]", re.I)                                  # [p1], [P 1]
_ITEM = re.compile(r"([PD])\s*(\d+)", re.I)


def normalize_markers(text: str) -> str:
    text = _ESC.sub(r"\1", text)

    def _range(m: re.Match) -> str:
        kind, kind2 = m.group(1).upper(), (m.group(3) or m.group(1)).upper()
        a, b = int(m.group(2)), int(m.group(4))
        if kind2 != kind or b < a or b - a >= 10:
            return m.group(0)
        return "".join(f"[{kind}{i}]" for i in range(a, b + 1))

    text = _RANGE.sub(_range, text)
    text = _GROUP.sub(lambda m: "".join(f"[{k.upper()}{n}]" for k, n in _ITEM.findall(m.group(1))), text)
    text = _SINGLE.sub(lambda m: f"[{m.group(1).upper()}{m.group(2)}]", text)
    return text


def sources_block(chunks: list[Chunk], results: list[QueryResult], max_rows: int = 50) -> tuple[str, list[Reference]]:
    parts: list[str] = []
    refs: list[Reference] = []
    for n, c in enumerate(chunks, start=1):
        m = f"P{n}"
        parts.append(f"[{m}] PDF: {c.source} · Topic: {c.section or c.title} · Page {c.page} · Category: {c.category}\n{c.text}")
        refs.append(Reference(marker=m, kind="pdf", source=c.source, title=c.title, section=c.section or c.title,
                              page=c.page, category=c.category, quote=_quote(c), chunk_id=c.id, superseded=c.superseded))
    for n, r in enumerate(results, start=1):
        m = f"D{n}"
        if r.error:
            parts.append(f"[{m}] DATABASE {r.database} ({r.engine}) · query failed: {r.error}")
        else:
            head = " | ".join(r.columns)
            body = "\n".join(" | ".join("" if v is None else str(v) for v in row) for row in r.rows[:max_rows])
            more = f"\n… {len(r.rows) - max_rows} more rows not shown" if len(r.rows) > max_rows else ""
            # the query's filter (factory, date window, order id …): without it neither the answering model
            # nor a reviewer can tell WHICH orders a bare count of 632 refers to
            filt = f" · filter: {', '.join(f'{k}={v}' for k, v in r.params.items())}" if r.params else ""
            parts.append(f"[{m}] DATABASE {r.database} ({r.engine}) · views: {', '.join(r.views)}{filt} · "
                         f"{len(r.rows)} row(s) as of {r.as_of:%d %b %Y %H:%M}\n{head}\n{body}{more}")
        refs.append(Reference(marker=m, kind="data", database=r.database, engine=r.engine, views=r.views,
                              row_keys=r.row_keys, row_count=len(r.rows), as_of=r.as_of, tool=r.tool, sql=r.sql,
                              params={k: str(v) for k, v in r.params.items()}))
    return "<sources>\n" + "\n\n".join(parts) + "\n</sources>", refs


def _quote(c: Chunk, limit: int = 300) -> str:
    body = c.body.strip().replace("\n", " ")
    if len(body) <= limit:
        return body
    cut = body[:limit]
    end = max(cut.rfind(". "), cut.rfind("। "))
    return (cut[: end + 1] if end > 80 else cut) + " …"


def _norm_num(s: str) -> str:
    return s.replace(",", "").rstrip(".")


def verify(answer: str, chunks: list[Chunk], results: list[QueryResult], not_found_text: str) -> tuple[bool, list[str]]:
    answer = normalize_markers(answer)
    problems: list[str] = []
    if answer.strip().startswith(not_found_text):
        return True, problems
    valid = {f"P{i}" for i in range(1, len(chunks) + 1)} | {f"D{i}" for i in range(1, len(results) + 1)}
    used = set(_MARK.findall(answer))
    bad = used - valid
    if bad:
        problems.append(f"unknown source ids: {sorted(bad)}")
    if not used:
        problems.append("no source ids in answer")

    # pool of allowed literals from cited sources only
    pool = ""
    for i, c in enumerate(chunks, start=1):
        if f"P{i}" in used:
            pool += " " + c.text
    for i, r in enumerate(results, start=1):
        if f"D{i}" in used:
            pool += " " + " ".join(str(v) for row in r.rows for v in row) + " " + " ".join(r.columns)
            # the [D#] reference card also shows the row count and the query parameters, so an answer
            # may state them ("12 orders", "for TAL"); nothing else outside the rows is licensed
            pool += f" {len(r.rows)} " + " ".join(str(v) for v in r.params.values())
    pool_nums = {_norm_num(x) for x in _NUM.findall(pool)}
    # plain digit runs too: "2026-10-12" (row dates) and "PCD-02" yield no _NUM match but must license 2026/10/12/02
    pool_nums |= set(_DIGIT_RUN.findall(pool))
    pool_up = pool.upper()

    for n in _NUM.findall(answer):
        v = _norm_num(n)
        if v and v not in pool_nums and not _looks_like_marker_or_date_part(n, answer):
            problems.append(f"number not in sources: {n}")
    for code in _CODE.findall(answer):
        if code.upper() not in pool_up:
            problems.append(f"code not in sources: {code}")
    return not problems, problems


def _looks_like_marker_or_date_part(n: str, answer: str) -> bool:
    # allow small integers used in prose like "two of the 3 orders" only if they are years/days present as words? keep strict:
    return False


def render_references(refs: list[Reference], used_markers: set[str] | None = None) -> str:
    """Plain-text references list for the CLI; the UI renders its own."""
    lines = []
    for r in refs:
        if used_markers and r.marker not in used_markers:
            continue
        if r.kind == "pdf":
            lines.append(f"[{r.marker}] {r.source}\n      Topic: {r.section} · Page {r.page} · Category: {r.category}\n      \"{r.quote}\"")
        else:
            keys = "; ".join(r.row_keys or []) or "—"
            asof = r.as_of.strftime("%d %b %Y %H:%M") if r.as_of else ""
            lines.append(f"[{r.marker}] {r.database} ({r.engine}) · {', '.join(r.views or [])}\n"
                         f"      Row key: {keys} · {r.row_count} row(s) · as of {asof}\n      Tool: {r.tool} · SQL: {r.sql}")
    return "\n".join(lines)
