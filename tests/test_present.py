"""C8: result table, CSV and chart decisions."""
from datetime import datetime
from decimal import Decimal

from ragbot.data.present import caption, chart_columns, csv_bytes, csv_name, frame
from ragbot.models import QueryResult


def _qr(cols, rows, tool="generated"):
    return QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], sql="SELECT 1",
                       tool=tool, columns=cols, rows=rows, as_of=datetime(2026, 9, 27, 12, 30))


def test_chart_for_label_and_number():
    r = _qr(["Factory", "Orders"], [["TAL", 1761], ["RHL", 746], ["BGL", Decimal("255")]])
    assert chart_columns(r) == ("Factory", "Orders")
    assert frame(r)["Orders"].tolist() == [1761, 746, 255.0]


def test_no_chart_for_single_value_wide_or_long_results():
    assert chart_columns(_qr(["Orders"], [[12]])) is None
    assert chart_columns(_qr(["a", "b", "c"], [["x", 1, 2], ["y", 3, 4]])) is None
    assert chart_columns(_qr(["F", "N"], [[f"F{i}", i] for i in range(30)])) is None
    assert chart_columns(_qr(["F", "Name"], [["TAL", "x"], ["RHL", "y"]])) is None     # value not numeric
    assert chart_columns(_qr(["N", "M"], [[1, 2], [3, 4]])) is None                    # label numeric too


def test_csv_has_bom_header_and_rows():
    data = csv_bytes(_qr(["Buyer", "Orders"], [["H&M", 3], ["কাপড়", 1]])).decode("utf-8")
    assert data.startswith("﻿Buyer,Orders") and "H&M,3" in data and "কাপড়,1" in data


def test_file_name_and_caption():
    r = _qr(["n"], [[1]], tool="upcoming_pcds")
    assert csv_name(r) == "upcoming_pcds_20260927_1230.csv"
    assert csv_name(_qr(["n"], [[1]])) == "rag.vw_ExportOrder_20260927_1230.csv"
    assert caption(r) == "1 row(s) · as of 27 Sep 2026 12:30 · rag.vw_ExportOrder"


def test_aggregate_results_have_no_blank_row_keys():
    r = _qr(["Factory", "Orders"], [["TAL", 1], ["RHL", 2]])
    r.key_columns = ["ExportOrderID", "ExportPONo"]           # the view's keys, absent from a GROUP BY result
    assert r.row_keys == []


def test_onnx_reranker_without_an_export_says_how_to_make_one(tmp_path, monkeypatch):
    import pytest
    import ragbot.embed as embed
    with pytest.raises(RuntimeError, match="export_reranker_onnx.py"):
        embed.OnnxReranker(tmp_path / "missing")
    monkeypatch.setenv("RERANK_BACKEND", "onnx")
    monkeypatch.setattr(embed, "OnnxReranker", lambda: "onnx-backend")
    embed.get_reranker.cache_clear()
    try:
        assert embed.get_reranker() == "onnx-backend"
    finally:
        embed.get_reranker.cache_clear()
