from ragbot.ingest.chunker import chunk_page, split_sections

PAGE = """3.1 Standard approval
A PCD change before fabric in-house is approved by the planner. """ + "x " * 300 + """

3.2 Change after fabric in-house
A change to the planned cut date after fabric in-house requires written approval from the Planning Head. """ + "y " * 300

def test_sections_split_at_headings():
    secs = split_sections(PAGE)
    assert [h for h, _ in secs] == ["3.1 Standard approval", "3.2 Change after fabric in-house"]

def test_chunks_have_prefix():
    pieces, last = chunk_page("SOP-PPM-014", PAGE, target=2400, minimum=100, overlap=100)
    assert pieces and all(p.startswith("SOP-PPM-014 › ") for _, p in pieces)
    assert last == "3.2 Change after fabric in-house"


import pytest
from ragbot.ingest.chunker import is_heading
from ragbot.ingest.pdf_text import is_toc_page, repeated_lines


@pytest.mark.parametrize("line", [
    "1.7  Exceptions", "13.1  Architecture & Design", "1. Introduction", "3.2 Change after fabric in-house",
    "SOP-04 — Standard PC/Laptop Build & Software Installation", "PART B — IT OPERATIONS POLICIES",
    "Annexure F — Standard Forms & Registers", "10. IT Service Desk & End-User Support",
])
def test_real_headings(line):
    assert is_heading(line)


@pytest.mark.parametrize("line", [
    "1.7.1", "2.", "30 days after the request the ticket is closed", "10 Working days must elapse before",
    "2 years for trend analysis and audit.", "Part A (Chapters 3–9) applies to every employee, contractor and third",
    "Annexure A and may be updated as the Group grows.", "01  –  24/08/2026", "Section 4.1 of the policy requires that",
    "1.1.2 Every SOP execution that produces a record (ticket, log entry, form, register line) is not complete until",
])
def test_prose_is_not_heading(line):
    assert not is_heading(line)


def test_toc_page_detection():
    toc = ["Table of Contents", "1. Introduction ........................ 1", "1.1  Purpose......................................1",
           "1.2  Scope......................................1", "1.3  Policy Statement.........................1",
           "2. IT Organization ...................... 4", "Annexure A ......................... xii"]
    assert is_toc_page(toc)
    assert not is_toc_page(["1.7  Exceptions", "1.7.1", "Any deviation from a mandatory clause requires ...", "Issue 01"])


def test_repeated_lines_only_at_page_edges():
    # header on every page (edge), "1.7.1"-style sub-clause numbers on every page but in the body (middle)
    # -> only edge lines end up in the junk set, so body sub-clauses survive.
    pages = [["Bitopi Group — IT Policy Book", "Doc. No.: P-IT-01", "Section", f"{i}.1 Heading", f"{i}.1.1",
              "Body text here", "more body", "even more", "closing line", "Page %d" % i] for i in range(1, 7)]
    junk = repeated_lines(pages, 0.5, head=3, tail=3)
    assert "Bitopi Group — IT Policy Book" in junk and "Page #" in junk
    assert "#.#.#" not in junk, junk
