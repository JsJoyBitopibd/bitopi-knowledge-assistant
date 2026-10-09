"""P1: the Inventory fixed tools answer the 8 Oct 2026 questions (and their paraphrases) with no model call,
with the company as a bound parameter from the entity dictionary and every store of the item listed."""
from datetime import datetime

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.data.tools as tools
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent.citations import verify
from ragbot.agent.router import route
from ragbot.auth.models import Scope
from ragbot.data.catalog import load_catalogs
from ragbot.data.tools import load_fixed_tools, match_fixed_tool
from ragbot.domain_agents import load_agents, pick_agent, validate_agents
from ragbot.models import QueryResult

NF = "System doesn't have the data."
Q8OCT = ("please find the closing balance of 1178108 this item code for nonproduction iten and companyid=02 "
         "and store name mantainance store of misami")


class NoModel:
    def chat(self, *a, **k):
        raise AssertionError("no model call expected")


@pytest.mark.parametrize("q, tool, item, company", [
    (Q8OCT, "stock_balance_by_item", "1178108", "02"),
    ("closing stock of 1178108 at misami maintenance store", "stock_balance_by_item", "1178108", "02"),
    ("how much 1178108 is left in the maintenance store of company 02", "stock_balance_by_item", "1178108", "02"),
    ("balance of item 1178108", "stock_balance_by_item", "1178108", ""),
    ("stock in hand of 1178108 at Tarasima central store", "stock_balance_by_item", "1178108", "06"),
    ("can you find grnid and issueid of itemcode=1178108  and companyid=02", "grn_issue_by_item", "1178108", "02"),
    ("goods receipts of item 1178108 at company 02", "grn_by_item", "1178108", "02"),
    ("GRN of item 1178108 at company 02", "grn_by_item", "1178108", "02"),
    ("issues of item 1178108 at company 02", "issues_by_item", "1178108", "02"),
    ("Which stores hold item 1178108?", "stores_holding_item", "1178108", None),
    ("What is item 1178108?", "item_by_code", "1178108", None),
    ("item 1178108", "item_by_code", "1178108", None),
])
def test_the_questions_reach_their_tool_with_the_right_parameters(q, tool, item, company):
    hit = match_fixed_tool(q, load_fixed_tools())
    assert hit and hit[0]["name"] == tool, (q, hit and hit[0]["name"])
    assert hit[1]["item"] == item
    if company is not None:
        assert hit[1]["company"] == company
        assert hit[1]["company_label"] == (f"at company {company}" if company else "")


@pytest.mark.parametrize("q", [
    "What is the trial balance of account 11780001?",           # finance vocabulary: not the store ledger
    "closing balance of LC TAL-25-1493-215",                     # an export order code
    "What is the password policy?",
])
def test_no_false_match(q):
    hit = match_fixed_tool(q, load_fixed_tools())
    assert not hit or not hit[0]["name"].startswith(("stock_", "grn_", "issues_", "stores_", "item_"))


def test_routed_to_data_without_a_model_call(monkeypatch):
    import ragbot.agent.router as router
    monkeypatch.setattr(router, "get_small_chat", lambda: NoModel())
    assert orch._route(Q8OCT, "") == "data"
    assert route("please find the closing balance of 1178108 for company 02") == "data"      # list cue + keyword


def test_the_inventory_agent_is_picked_and_validates():
    agents = load_agents()
    assert "inventory" in agents
    assert pick_agent(Q8OCT).name == "inventory"
    assert pick_agent("goods receipts of item 1178108 at company 02").name == "inventory"    # not sourcing any more
    assert validate_agents(agents, load_catalogs(), load_fixed_tools()) == []


def test_the_views_rewrite_and_scope_filters_them():
    from ragbot.data.virtual import rewrite_virtual
    cat = load_catalogs()["Inventory"]
    assert {v.name for v in cat.views} >= {"rag.vw_StockBalance", "rag.vw_StockMovement", "rag.vw_GoodsReceipt",
                                           "rag.vw_StockIssue", "rag.vw_Item", "rag.vw_Store", "rag.vw_Company"}
    sql = "SELECT TOP (50) StoreName, ClosingBalance FROM rag.vw_StockBalance WHERE ItemCode = @item"
    exec_all, views = rewrite_virtual(sql, cat, scope=Scope.unrestricted())
    assert views == ["rag.vw_StockBalance"] and "dbo.GRNChild" in exec_all and "dbo.IssueChildWithoutGRN" in exec_all
    exec_tal, _ = rewrite_virtual(sql, cat, scope=Scope(factories=["TAL"]))
    assert "[Factory] IN ('TAL')" in exec_tal


def test_templated_answer_verifies_and_lists_every_store(monkeypatch):
    """The answer is written from the rows: the store the user asked for is one of them, with its balance."""
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "get_chat", lambda: NoModel())
    cols = ["CompanyID", "CompanyName", "StoreName", "ItemCode", "ItemName", "NonProductionItem", "BaseUOM",
            "ReceivedQty", "IssuedQty", "ClosingBalance", "LastTransaction"]
    rows = [["02", "MISAMI Garments Ltd.", "Maintenance Store (Misami)", "1178108", "Grease (White Lithium)", "Yes",
             "L1", 13.0, 12.125, 0.875, datetime(2022, 2, 22)]]

    def fake_run(engine, sql, params, **kw):
        assert params["item"] == "1178108" and params["company"] == "02"
        return cols, rows, datetime.now()
    monkeypatch.setattr(tools, "cached_run", fake_run)
    a = orch.answer(Q8OCT, scope=Scope.unrestricted())
    assert not a.not_found and a.usage.calls == 0 and a.route == "data" and a.agent == "inventory"
    assert "Maintenance Store (Misami)" in a.text and "0.875" in a.text and "[D1]" in a.text
    assert verify(a.text, [], a.results, NF)[0]
    assert any("Goods receipts of item 1178108 at company 02" == f for f in a.follow_ups)
