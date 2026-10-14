"""P3 (ROADMAP K2, finally built): a fixed tool's companion from another database merged onto the main rows in
Python — one table cited [D1][D2], both results kept so the verifier licenses every cell."""
from datetime import datetime

import ragbot.agent.orchestrator as orch
import ragbot.data.tools as tools
from ragbot.agent.citations import verify
from ragbot.auth.models import Scope
from ragbot.models import Answer, QueryResult

NF = "System doesn't have the data."


def _pair():
    main = QueryResult(database="VistaQ_Default_RHL", engine="sqlserver", views=["rag.vw_PlanQualityDay"], sql="SELECT 1",
                       tool="quality_by_group_window", params={"window": "yesterday", "by": "file", "note": ""},
                       columns=["Date", "FileNo", "DHU"], rows=[["2026-10-07", "FR-26-1985", 18.73], ["2026-10-07", "FR-26-2001", 4.5]],
                       as_of=datetime(2026, 10, 8, 9, 0))
    extra = QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], sql="SELECT 2",
                        tool="quality_by_group_window:companion", columns=["FileNo", "Buyer", "ExportOrders", "POs", "FirstEO", "LastEO", "OrderQty"],
                        rows=[["FR-26-1985", "H&M", 17, 15, "TAL-26-1985-1", "TAL-26-1985-17", 120000]],
                        as_of=datetime(2026, 10, 8, 9, 1))
    return main, extra


def test_merge_adds_the_companion_columns_by_key_and_keeps_unmatched_rows():
    main, extra = _pair()
    m = tools.merge_companion(main, extra, {"on": ["FileNo"], "add": ["Buyer", "ExportOrders", "POs"]})
    assert m.columns == ["Date", "FileNo", "DHU", "Buyer", "ExportOrders", "POs"]
    assert m.rows == [["2026-10-07", "FR-26-1985", 18.73, "H&M", 17, 15], ["2026-10-07", "FR-26-2001", 4.5, None, None, None]]
    assert m.database == "VistaQ_Default_RHL + BitopiSplint" and m.views == ["rag.vw_PlanQualityDay", "rag.vw_ExportOrder"]
    assert m.as_of == main.as_of and m.tool.endswith(":merged")
    assert tools.merge_companion(main, extra, {"on": ["Nope"], "add": ["Buyer"]}) is None
    assert tools.merge_companion(main, extra, {"on": ["FileNo"], "add": ["Missing"]}) is None


def test_the_templated_merged_table_cites_both_sources_and_verifies(monkeypatch):
    main, extra = _pair()
    tool = {"name": "quality_by_group_window", "answer_list": "QC by {by} {window} ({n} rows)",
            "companion": {"database": "BitopiSplint", "column": "FileNo", "merge": {"on": ["FileNo"], "add": ["Buyer", "ExportOrders", "POs"]}}}
    monkeypatch.setattr(tools, "load_fixed_tools", lambda: [tool])
    out = Answer(text="", question="PO wise DHU of TAL yesterday")
    assert orch._templated_answer(out, [main, extra], NF)
    assert out.text.startswith("QC by file yesterday (2 rows) [D1][D2]:")
    assert "| H&M | 17 | 15 |" in out.text and "FR-26-2001" in out.text
    assert [r.marker for r in out.references] == ["D1", "D2"] and len(out.results) == 2
    assert verify(out.text, [], out.results, NF)[0]
    assert "Export orders and POs" not in out.text                  # the companion's own table is not repeated


def test_without_a_merge_spec_the_companion_is_still_its_own_table(monkeypatch):
    main, extra = _pair()
    tool = {"name": "quality_by_group_window", "answer_list": "QC by {by} {window} ({n} rows)",
            "companion": {"database": "BitopiSplint", "column": "FileNo", "answer_list": "Export orders of each file"}}
    monkeypatch.setattr(tools, "load_fixed_tools", lambda: [tool])
    out = Answer(text="", question="q")
    assert orch._templated_answer(out, [main, extra], NF)
    assert "[D1]:" in out.text and "Export orders of each file [D2]:" in out.text


def test_the_companion_sees_the_parents_parameters(monkeypatch):
    seen = {}
    monkeypatch.setattr(tools, "run_fixed_tool", lambda tool, params, cats, user="", refresh=False, scope=None:
                        seen.update(params) or QueryResult(database="B", engine="sqlserver", views=[], sql="", columns=[], rows=[]))
    main = QueryResult(database="A", engine="sqlserver", views=[], sql="", tool="t", params={"company": "02", "item": "1178108"},
                       columns=["ItemCode"], rows=[["1178108"]])
    tool = {"name": "t", "agent": "x", "companion": {"database": "B", "column": "ItemCode", "param": "items",
                                                    "sql": "SELECT 1 FROM v WHERE ItemCode IN (@items) AND CompanyID = @company"}}
    tools.run_companion(tool, main, {"B": object()}, scope=Scope.unrestricted())
    assert seen["company"] == "02" and seen["items_0"] == "1178108"
