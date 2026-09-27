"""Fixed-tool matching on the real config/fixed_tools.yaml: the right tool and parameters, and no false
matches from words that merely start with a tool keyword."""
import pytest

from ragbot.data.tools import load_fixed_tools, match_fixed_tool


@pytest.mark.parametrize("q, tool, params", [
    ("What is the ship date for TAL-25-1493-215 and how long are backup records retained per the IT policy?",
     "eo_by_id", {"eo": "TAL-25-1493-215"}),        # 'policy' once matched eo_by_po as PO 'licy'
    ("What is the status of PO 594520-9192?", "eo_by_po", {"po": "594520-9192"}),
    ("Which export order is buyer PO number 594520-9192?", "eo_by_po", {"po": "594520-9192"}),
    ("ship date for PO#4500123456", "eo_by_po", {"po": "4500123456"}),
    ("How many PPM meetings does TAL have next week?", "ppm_meetings_count_by_factory_window", {"factory": "TAL"}),
    ("Show the PCD history for TAL-17-382-1", "pcd_history_by_eo", {"eo": "TAL-17-382-1"}),
])
def test_matches(q, tool, params):
    hit = match_fixed_tool(q, load_fixed_tools())
    assert hit and hit[0]["name"] == tool
    assert all(hit[1][k] == v for k, v in params.items())


@pytest.mark.parametrize("q", ["What is the PO policy for orders?", "What is the password policy?",
                               "Which positions approve a purchase order status change?"])
def test_no_false_match(q):
    hit = match_fixed_tool(q, load_fixed_tools())
    assert not hit or hit[0]["name"] != "eo_by_po"


@pytest.mark.parametrize("q, tool", [
    ("What is the next PCD?", "upcoming_pcds"),
    ("what is the upcoming pcd", "upcoming_pcds"),
    ("What is the H&M upcoming PCD?", "upcoming_pcds_by_buyer"),
    ("TAL next PCD", "upcoming_pcds_by_factory"),
    ("give me buyer names", "buyer_list"),
    ("What is the buyer name for TAL-25-1493-215?", "eo_by_id"),     # an order code beats the buyer list
    ("How many export orders were cancelled this month?", "cancelled_orders_count_window"),
    ("Which export orders were cancelled in the last 30 days?", "cancelled_orders_window"),
])
def test_tools_added_from_real_questions(q, tool):
    hit = match_fixed_tool(q, load_fixed_tools())
    assert hit and hit[0]["name"] == tool


@pytest.mark.parametrize("q, asks", [
    ("What is the status of the order?", "Which export order"),
    ("PCD for my order please", "Which export order"),
    ("How many PPM meetings this week?", "For which factory"),
    ("What is the status of TAL-25-1493-215?", None),
    ("status of the order with PO 594520-9192", None),
    ("How many PPM meetings does TAL have this week?", None),
    ("Which orders have a PCD next week?", None),               # answerable by generated SQL: no question back
])
def test_clarification_rules(q, asks):
    from ragbot.data.tools import needs_clarification
    got = needs_clarification(q)
    assert (got is None) if asks is None else (got and got.startswith(asks))
