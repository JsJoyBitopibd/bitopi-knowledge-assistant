"""How a database result is shown in the UI (Phase C8): table, CSV download, and a bar chart when the
result is a simple "label → number" list. Pure functions over QueryResult so they are testable
without Streamlit; app.py only renders what these return."""
from __future__ import annotations

import io
import re
from decimal import Decimal
from typing import Any, Optional

from ..models import QueryResult

MAX_CHART_ROWS = 25


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def frame(r: QueryResult):
    """A pandas DataFrame of the rows (Decimal -> float so the table and chart can sort and plot)."""
    import pandas as pd
    rows = [[float(v) if isinstance(v, Decimal) else v for v in row] for row in r.rows]
    return pd.DataFrame(rows, columns=r.columns)


def csv_bytes(r: QueryResult) -> bytes:
    """UTF-8 CSV with a BOM, so Excel opens Bangla text and '—' correctly."""
    buf = io.StringIO()
    frame(r).to_csv(buf, index=False)
    return ("﻿" + buf.getvalue()).encode("utf-8")


def csv_name(r: QueryResult) -> str:
    base = r.tool if r.tool and r.tool != "generated" else (r.views[0] if r.views else "result")
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", base) + f"_{r.as_of:%Y%m%d_%H%M}.csv"


def chart_columns(r: QueryResult) -> Optional[tuple[str, str]]:
    """(label, value) when the result is 2 columns, a text/date label and a number, 2–25 rows."""
    if len(r.columns) != 2 or not (2 <= len(r.rows) <= MAX_CHART_ROWS):
        return None
    labels, values = [row[0] for row in r.rows], [row[1] for row in r.rows]
    if all(_is_number(v) or v is None for v in values) and not all(_is_number(v) for v in labels):
        if len({str(x) for x in labels}) == len(labels):          # one bar per distinct label
            return r.columns[0], r.columns[1]
    return None


def caption(r: QueryResult) -> str:
    what = ", ".join(r.views) if r.views else r.database
    return f"{len(r.rows)} row(s) · as of {r.as_of:%d %b %Y %H:%M} · {what}"
