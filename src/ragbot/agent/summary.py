"""One line above a long table that says what is in it (Phase T8).

A 143-row list is hard to take in. Above it, one sentence states what the rows themselves hold: how many factories
(or units, lines, buyers, styles) they span, the range of their first date column, and — when the list is complete —
the total of their first quantity. For example:

    Across these 143 rows: 3 factories (BGL, RHL, TAL); ShipDate from 2 Nov 2026 to 9 Nov 2026; total OrderQty 45200 [D1].

Every figure is computed here from the result's own cells — nothing that is not len(rows) or derived from the rows
(CLAUDE.md non-negotiable 2: Python computes) — and is carried in a `*_label` parameter of the result, so
citations.verify() licenses it the way it licenses a templated lead's filter values, and the source card does not
show it. A total is never given for a list that stopped at its row limit: it would be the total of a cut.
"""
from __future__ import annotations

from typing import Optional

from ..config import settings
from ..models import QueryResult

_DIMS = (("Factory", "factory", "factories"), ("Unit", "unit", "units"), ("Line", "line", "lines"),
         ("LineNumber", "line", "lines"), ("Buyer", "buyer", "buyers"), ("Style", "style", "styles"),
         ("StyleName", "style", "styles"))


def summarize(r: QueryResult, marker: str = "D1") -> Optional[tuple[str, dict[str, str]]]:
    """(the sentence, the params that license it), or None when there is nothing worth saying."""
    from ..ui.view import display_date
    from .refine import _sum, additive, capped, kind_of
    from .templated import fmt
    min_rows = int(settings().get("answer.summary_min_rows", 10))
    if r.error or r.transform or len(r.rows) < min_rows or len(r.columns) < 2:
        return None
    parts: list[str] = []
    params: dict[str, str] = {}
    for col, one, many in _DIMS:
        if col not in r.columns:
            continue
        i = r.columns.index(col)
        vals = sorted({str(x[i]).strip() for x in r.rows if x[i] is not None and str(x[i]).strip()})
        if 1 < len(vals) < len(r.rows):
            listed = f" ({', '.join(vals)})" if len(vals) <= 4 else ""
            parts.append(f"{len(vals)} {many}{listed}")
            params["summary_groups_label"] = f"{len(vals)} " + ", ".join(vals[:4])
            break
    dates = [c for c in r.columns if kind_of(r, c) == "date"]
    if dates:
        i = r.columns.index(dates[0])
        cells = sorted(fmt(x[i]) for x in r.rows if x[i] is not None)
        lo, hi = display_date(cells[0][:10]), display_date(cells[-1][:10])
        if lo != hi:
            parts.append(f"{dates[0]} from {lo} to {hi}")
            params["summary_dates_label"] = f"{lo} {hi}"
    max_rows = int(settings().get("data.max_rows", 200))
    nums = [c for c in r.columns if additive(r, c) and c != "Count"]
    if nums and not capped(r, max_rows) and not r.params.get("shown_label"):
        i = r.columns.index(nums[0])
        total = _sum([x[i] for x in r.rows]) or 0
        parts.append(f"total {nums[0]} {fmt(total)}")
        params["summary_total_label"] = fmt(total)
    if not parts:
        return None
    params["summary_rows_label"] = str(len(r.rows))
    return f"Across these {len(r.rows)} rows: {'; '.join(parts)} [{marker}].", params
