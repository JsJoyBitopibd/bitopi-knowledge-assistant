"""Heading-aware chunking with overlap and a title › section prefix on every chunk."""
from __future__ import annotations

import re

# "3.2 Change after fabric in-house", "1.7  Exceptions", "4) Scope", "১.২ ..." — number, then a capitalised title.
# Case-sensitive on purpose: a wrapped sentence line such as "10 minutes before the meeting" must not match.
HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})[.)]?\s+[A-Zঀ-৿]")
# "30 Days after…", "2 Years for trend analysis" — a number followed by a unit word is prose, not a heading.
_UNIT_AFTER = re.compile(r"^\d[\d.]*[.)]?\s+(days?|hours?|hrs?|minutes?|months?|years?|weeks?|working|business|"
                         r"calendar|pcs|percent|copies|times)\b", re.I)
# "SOP-02 — New Joiner Setup"
SOP_HEADING = re.compile(r"^SOP-\d{2,3}\s*[—–-]\s*\S")
# Whole-line block headings only: "PART B — IT OPERATIONS POLICIES", "Annexure F — Standard Forms".
# ("Part A (Chapters 3–9) applies to…" and "Annexure A and may be updated…" are wrapped prose.)
BLOCK_HEADING = re.compile(r"^(PART\s+[A-Z]|Annexure\s+[A-Z]|Appendix\s+[A-Z0-9]+|Chapter\s+\d+)\s*(?:[—–:-]\s*\S.*)?$")


def is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 120 or s[-1] in ".,;:":
        return False
    if SOP_HEADING.match(s) or BLOCK_HEADING.match(s):
        return True
    return bool(HEADING.match(s)) and not _UNIT_AFTER.match(s) and len(s.split()) <= 12


def split_sections(page_text: str) -> list[tuple[str, str]]:
    """[(heading, body)] using headings as boundaries; '' heading for leading text.
    Number-only lines ("1.7.1", "2.") are never headings, so sub-clauses and procedure steps stay in the body."""
    sections: list[tuple[str, str]] = []
    heading, buf = "", []
    for line in page_text.splitlines():
        if is_heading(line):
            if buf:
                sections.append((heading, "\n".join(buf).strip()))
            heading, buf = line.strip(), []
        else:
            buf.append(line)
    if buf:
        sections.append((heading, "\n".join(buf).strip()))
    return [(h, b) for h, b in sections if b or h]


def split_long(body: str, target: int, overlap: int) -> list[str]:
    """Cut at paragraph breaks, then sentences, carrying `overlap` chars of the previous piece."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    pieces, buf = [], ""
    for p in paras:
        if len(p) > target:  # very long paragraph: split at sentence ends
            sents = re.split(r"(?<=[.!?।])\s+", p)
            for s in sents:
                if buf and len(buf) + len(s) > target:
                    pieces.append(buf)
                    buf = buf[-overlap:] + " " + s
                else:
                    buf = (buf + " " + s).strip()
            continue
        if buf and len(buf) + len(p) > target:
            pieces.append(buf)
            buf = buf[-overlap:] + "\n\n" + p
        else:
            buf = (buf + "\n\n" + p).strip()
    if buf:
        pieces.append(buf)
    return pieces


def chunk_page(doc_title: str, page_text: str, target: int = 2400, minimum: int = 400,
               overlap: int = 250, carry_heading: str = "") -> tuple[list[tuple[str, str]], str]:
    """Return ([(section, stored_text)], last_heading). Tiny sections merge into the next.
    `carry_heading` lets a section that started on the previous page keep its heading."""
    out: list[tuple[str, str]] = []
    carry = ""
    last_heading = carry_heading
    for heading, body in split_sections(page_text):
        heading = heading or last_heading
        last_heading = heading or last_heading
        body = (carry + "\n\n" + body).strip() if carry else body
        carry = ""
        if len(body) < minimum:
            carry = body
            continue
        for piece in split_long(body, target, overlap):
            prefix = f"{doc_title} › {heading}" if heading else doc_title
            out.append((heading, f"{prefix}\n{piece}"))
    if carry:
        prefix = f"{doc_title} › {last_heading}" if last_heading else doc_title
        out.append((last_heading, f"{prefix}\n{carry}"))
    return out, last_heading
