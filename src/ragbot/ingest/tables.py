"""Tables become their own chunks (Markdown + caption) so that approval matrices and
escalation tables are retrievable and readable by the model."""
from __future__ import annotations

import pymupdf


def tables_on_page(page: pymupdf.Page) -> list[tuple[str, str]]:
    """Return [(caption, markdown)] for each table found on the page. Caption = nearest text
    line above the table, or 'Table'."""
    out: list[tuple[str, str]] = []
    try:
        found = page.find_tables()
    except Exception:
        return out
    for t in found.tables:
        try:
            md = t.to_markdown().strip()
        except Exception:
            continue
        if md.count("|") < 4:
            continue
        x0, y0, x1, y1 = t.bbox
        caption = "Table"
        try:
            above = page.get_text("blocks", clip=(x0 - 20, max(0, y0 - 40), x1 + 20, y0))
            if above:
                caption = above[-1][4].strip().splitlines()[-1][:120] or caption
        except Exception:
            pass
        out.append((caption, md))
    return out
