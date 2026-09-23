from ragbot.agent.citations import normalize_markers, sources_block, verify
from ragbot.models import Chunk, QueryResult

NF = "System doesn't have the data."


def chunk():
    return Chunk(id="d#p4#c1", text="SOP › 3.2\nApproval on form PCD-02 by the Planning Head within 2 days.",
                 source="SOP.pdf", title="SOP", page=4, section="3.2")


def chunk2():
    return Chunk(id="d#p5#c1", text="SOP › 3.3\nThe Planning Head signs the approval.",
                 source="SOP.pdf", title="SOP", page=5, section="3.3")


def test_valid_answer():
    ok, problems = verify("Use form PCD-02 [P1].", [chunk()], [], NF)
    assert ok, problems


def test_unknown_marker_and_number():
    ok, problems = verify("Use form PCD-07 within 5 days [P3].", [chunk()], [], NF)
    assert not ok and any("unknown" in p for p in problems) and any("number" in p for p in problems)


def test_not_found_passes():
    ok, _ = verify("System doesn't have the data. Try the HR policy.", [chunk()], [], NF)
    assert ok


def test_sources_block_refs():
    block, refs = sources_block([chunk()], [])
    assert "[P1] PDF: SOP.pdf" in block and refs[0].page == 4 and refs[0].quote.startswith("Approval")


def test_normalize_marker_variants():
    assert normalize_markers("a [P1, P2] b") == "a [P1][P2] b"
    assert normalize_markers("a [P1; D1] b") == "a [P1][D1] b"
    assert normalize_markers("a [P1 and P2] b") == "a [P1][P2] b"
    assert normalize_markers("a \\[P1\\] b") == "a [P1] b"
    assert normalize_markers("a [P1-P3] b") == "a [P1][P2][P3] b"
    assert normalize_markers("a [p2] [P 3] b") == "a [P2] [P3] b"
    assert normalize_markers("[P1][P2]") == "[P1][P2]"


def test_grouped_markers_verify():
    ok, problems = verify("The Planning Head approves on form PCD-02 [P1, P2].", [chunk(), chunk2()], [], NF)
    assert ok, problems


def test_row_date_is_licensed():
    qr = QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], sql="SELECT 1",
                     columns=["ExportOrderID", "PCD"], rows=[["TAL-17-382-1", "2026-10-12 00:00:00"]],
                     key_columns=["ExportOrderID"])
    ok, problems = verify("The PCD for TAL-17-382-1 is 12 Oct 2026 [D1].", [], [qr], NF)
    assert ok, problems
