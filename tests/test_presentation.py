"""Phase T8: answer presentation — headings, dates, highlighted rows, the summary line, derived and paged sources,
the progress line and Stop."""
from datetime import date, datetime
from decimal import Decimal

import pytest

import ragbot.agent.orchestrator as orch
from ragbot.agent.citations import verify
from ragbot.agent.summary import summarize
from ragbot.data import present
from ragbot.models import Answer, QueryResult, Reference
from ragbot.ui import view

from test_ui_view import EVIL, _data_ref


@pytest.mark.parametrize("col,want", [
    ("ShipDate", "Ship date"), ("ExportOrderID", "Export order ID"), ("OrderQty", "Order qty"),
    ("order_qty", "Order qty"), ("DHU", "DHU"), ("PCD", "PCD"), ("Count", "Count"), ("Total OrderQty", "Total OrderQty"),
    ("ExportPONo", "Export PO no"), ("buyer", "Buyer"), ("", ""),
])
def test_humanize(col, want):
    assert view.humanize(col) == want


def test_display_date():
    assert view.display_date("2026-10-12") == "12 Oct 2026"
    assert view.display_date("2026-10-12 09:15") == "12 Oct 2026 09:15"
    assert view.display_date("2026-10-12 09:15:30") == "12 Oct 2026 09:15"
    assert view.display_date("2026-13-01") == "2026-13-01" and view.display_date("TAL-25-1") == "TAL-25-1"


def test_table_heading_keeps_the_real_name_and_dates_sort_by_iso():
    html = view.sortable_table_html(["ExportOrderID", "ShipDate"], [["TAL-25-1", "2026-11-02"], ["TAL-25-2", "2026-11-09"]])
    assert 'data-col="ShipDate"' in html and 'title="Sort by ShipDate">Ship date' in html
    assert '<td data-v="2026-11-02">2 Nov 2026</td>' in html


def test_rows_naming_the_asked_record_are_highlighted():
    html = view.sortable_table_html(["ExportOrderID", "Qty"], [["TAL-25-1493-215", "5"], ["TAL-25-9", "7"]],
                                    mark=["tal-25-1493-215"])
    assert '<tr data-i="0" class="ka-hit">' in html and '<tr data-i="1">' in html
    assert 'class="ka-total ka-hit"' in view.sortable_table_html(["A"], [["Total"]], mark=["total"])


def test_new_html_escapes_its_text():
    for html in (view.sortable_table_html([EVIL], [[EVIL]], mark=[EVIL]),
                 view.sortable_table_html(["D"], [[EVIL]]),
                 view.source_card_html(_data_ref(transform=EVIL, source_rows=9)),
                 view.source_card_html(_data_ref(transform="page " + EVIL))):
        assert "<script>" not in html and "<img src=x" not in html and "&lt;script&gt;" in html


def test_done_label_for_refined_paged_social_and_understood():
    refined = _data_ref(row_count=5, transform="where Factory is TAL, first 5", source_rows=143)
    assert view.done_label([refined], 0.1) == "Refined the previous result · 5 of 143 rows · no new query · 0.1 s"
    paged = _data_ref(row_count=200, transform="page 2")
    assert view.done_label([paged]) == "Read the next page of rag.vw_OrderShipment · 200 rows"
    assert view.done_label([], 1.2, kind="social") == "No database or document needed · 1.2 s"
    assert view.done_label([], kind="") == "No source found"
    assert view.done_label([_data_ref()], understood="how many PPM meetings at TAL this week?").startswith(
        "Understood as “how many PPM meetings at TAL this week?” · Queried")


def test_source_card_for_derived_rows_and_pages():
    card = view.source_card_html(_data_ref(row_count=5, transform="where Factory is TAL, first 5", source_rows=143))
    assert "Derived from the previous answer · BitopiSplint" in card and "5 of 143" in card
    assert "where Factory is TAL, first 5" in card and "no new query" in card and "SQL Server" not in card
    page = view.source_card_html(_data_ref(row_count=200, transform="page 2"))
    assert "Live data · BitopiSplint" in page and "<dt>Page</dt><dd class=\"\">2</dd>" in page


def test_export_and_caption_say_what_was_derived():
    ref = _data_ref(row_count=5, transform="where Factory is TAL, first 5", source_rows=143)
    md = view.export_markdown([{"role": "user", "text": "only TAL"}, {"role": "assistant", "text": "…", "refs": [ref]}],
                              "t", "s", datetime(2026, 10, 30, 10))
    assert "derived from the previous answer: where Factory is TAL, first 5" in md
    r = QueryResult(database="X", engine="sqlserver", views=["rag.v"], sql="", columns=["a"], rows=[[1]],
                    transform="first 5", as_of=datetime(2026, 10, 30, 9))
    assert present.caption(r) == "1 row(s) · first 5 · as of 30 Oct 2026 09:00 · rag.v"


def test_dangling_question():
    assert view.dangling_question([{"role": "user", "text": "q1"}]) == "q1"
    assert view.dangling_question([{"role": "user", "text": "q1"}, {"role": "assistant", "text": "a"}]) is None
    assert view.dangling_question([]) is None


# ---------------------------------------------------------------- the summary line
def _orders(n=34, capped=False):
    rows = [[f"{['TAL', 'RHL', 'BGL'][i % 3]}-25-{i}", ["TAL", "RHL", "BGL"][i % 3], Decimal(100 + i),
             date(2026, 11, 2 + i % 6)] for i in range(n)]
    return QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], tool="t",
                       sql=f"SELECT TOP ({n if capped else 200}) * FROM rag.vw_ExportOrder",
                       columns=["ExportOrderID", "Factory", "OrderQty", "ShipDate"], rows=rows,
                       as_of=datetime(2026, 10, 30, 9))


def test_summary_states_only_what_the_rows_hold():
    line, params = summarize(_orders())
    total = sum(Decimal(100 + i) for i in range(34))
    assert line == (f"Across these 34 rows: 3 factories (BGL, RHL, TAL); ShipDate from 2 Nov 2026 to 7 Nov 2026; "
                    f"total OrderQty {total} [D1].")
    r = _orders().model_copy(update={"params": params})
    assert verify(line, [], [r], "System doesn't have the data.")[0]
    assert not verify(line, [], [_orders()], "System doesn't have the data.")[0]       # licensed only by its params


def test_no_total_for_a_cut_list_and_nothing_for_short_ones():
    line, _ = summarize(_orders(40, capped=True))
    assert "total" not in line
    assert summarize(_orders(5)) is None
    cut = _orders().model_copy(update={"params": {"shown_label": "first 10"}})
    assert "total" not in summarize(cut)[0]
    assert summarize(_orders().model_copy(update={"transform": "first 5"})) is None


def test_the_templated_answer_carries_the_summary_and_verifies(monkeypatch):
    import ragbot.data.tools as tools
    monkeypatch.setattr(tools, "load_fixed_tools", lambda: [{"name": "t", "answer_list": "{n} order(s) ship next week"}])
    out = Answer(text="", question="q")
    assert orch._templated_answer(out, [_orders()], "System doesn't have the data.")
    head, summary, table = out.text.split("\n\n", 2)
    assert head == "34 order(s) ship next week [D1]:" and summary.startswith("Across these 34 rows: 3 factories")
    assert table.startswith("| ExportOrderID |")
    assert "summary line dropped" not in " ".join(out.warnings)
