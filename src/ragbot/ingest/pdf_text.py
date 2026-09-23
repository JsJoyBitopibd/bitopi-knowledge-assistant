"""Page text extraction: header/footer stripping, TOC-page detection and OCR fallback for scanned pages."""
from __future__ import annotations

import logging
import os
import re
from collections import Counter

import pymupdf

log = logging.getLogger("ingest")

_DIGITS = re.compile(r"\d+")
# "1.1  Purpose...........................1"  /  "Table of Contents.....v"
_LEADER = re.compile(r"(?:\.\s?){4,}\s*[ivxlcdm\d]{1,4}\s*$", re.I)


def _norm(line: str) -> str:
    return _DIGITS.sub("#", line.strip())


def repeated_lines(pages: list[list[str]], min_share: float = 0.5, head: int = 3, tail: int = 3) -> set[str]:
    """Header/footer candidates: lines (digits normalised) that sit in the top `head` or bottom `tail` lines
    of at least `min_share` of pages. Only the page edges are considered — sub-clause numbers ("1.7.1") and
    SOP labels ("Purpose", "Procedure") recur on most pages too, but in the body, and must survive."""
    if len(pages) < 3:
        return set()
    counts: Counter[str] = Counter()
    for lines in pages:
        edge = {_norm(x) for x in lines[:head]} | {_norm(x) for x in lines[-tail:]}
        for l in edge:
            counts[l] += 1
    return {l for l, n in counts.items() if n >= min_share * len(pages)}


def is_toc_page(lines: list[str], min_lines: int = 5, min_share: float = 0.5) -> bool:
    """A table-of-contents page: most lines end in dotted leaders + a page number (arabic or roman)."""
    dotted = sum(1 for l in lines if _LEADER.search(l))
    return dotted >= min_lines and dotted >= min_share * max(1, len(lines))


def page_text(page: pymupdf.Page, ocr_min_chars: int = 50, langs: str | None = None) -> tuple[str, bool]:
    """Return (text, was_ocr). OCR only when the page has (almost) no text layer."""
    text = page.get_text()
    if len(text.strip()) >= ocr_min_chars:
        return text, False
    langs = langs or os.getenv("TESSERACT_LANGS", "eng+ben")
    try:
        tp = page.get_textpage_ocr(full=True, language=langs)
        return page.get_text(textpage=tp), True
    except Exception as e:  # Tesseract missing or failed: keep what we have, never pollute the chunk text
        log.warning("OCR unavailable for page %d of %s: %s", page.number + 1,
                    getattr(page.parent, "name", "?"), e.__class__.__name__)
        return text, False


def clean_pages(doc: pymupdf.Document, ocr_min_chars: int = 50, min_share: float = 0.5,
                head: int = 3, tail: int = 3) -> tuple[list[str], int, set[int]]:
    """All pages' text with repeated header/footer lines removed.
    Returns (pages, ocr_page_count, toc_pages) — toc_pages holds 1-based page numbers to skip."""
    raw: list[list[str]] = []
    ocr_pages = 0
    for page in doc:
        text, was_ocr = page_text(page, ocr_min_chars)
        ocr_pages += int(was_ocr)
        raw.append([l.strip() for l in text.splitlines() if l.strip()])
    junk = repeated_lines(raw, min_share, head, tail)
    pages: list[str] = []
    toc: set[int] = set()
    for pno, lines in enumerate(raw, start=1):
        n = len(lines)
        kept = [l for j, l in enumerate(lines) if not ((j < head or j >= n - tail) and _norm(l) in junk)]
        if is_toc_page(kept):
            toc.add(pno)
        pages.append("\n".join(kept))
    return pages, ocr_pages, toc
