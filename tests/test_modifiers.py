"""Phase T2: sort and limit phrases (data/modifiers.py), the fixed tools' order_by parameter, and the limit cut."""
from datetime import date, datetime

import pytest

from ragbot.agent.citations import verify
from ragbot.agent.templated import modifier_note, try_template
from ragbot.data import tools as T
from ragbot.data.modifiers import Modifiers, order_clause, resolve_column, strip
from ragbot.models import QueryResult


@pytest.mark.parametrize("q,rest,sort,desc,limit,from_end", [
    ("Which TAL orders have a PCD next week sorted by quantity, top 10?",
     "Which TAL orders have a PCD next week?", "quantity", None, 10, False),
    ("TAL orders shipping next week sorted by ship date latest first",
     "TAL orders shipping next week", "ship date", True, None, False),
    ("show the next PCDs ordered by buyer ascending", "show the next PCDs", "buyer", False, None, False),
    ("top 5 lines by DHU this week", "lines this week", "DHU", True, 5, False),
    ("lowest 3 lines by output today", "lines output today", "", None, 3, False),   # see the case below
    ("which orders ship next week, highest quantity first", "which orders ship next week", "quantity", True,
     None, False),
    ("last 5 cancelled orders", "cancelled orders", "", None, 5, True),
    ("cancelled orders in the last 30 days", "cancelled orders in the last 30 days", "", None, None, False),
    ("first 20 orders of TAL", "orders of TAL", "", None, 20, False),
    ("TAL orders limit to 15 rows", "TAL orders", "", None, 15, False),
    ("What is the PCD for export order TAL-25-1493-215?", "What is the PCD for export order TAL-25-1493-215?",
     "", None, None, False),
    ("Which TAL lines have the highest DHU?", "Which TAL lines have the highest DHU?", "", None, None, False),
    ("TAL orders by buyer", "TAL orders by buyer", "", None, None, False),        # "orders by" is not a sort
])
def test_strip(q, rest, sort, desc, limit, from_end):
    got, mods = strip(q)
    if q.startswith("lowest 3 lines by output"):
        # "lowest N <noun> by <col>" is a sorted top-N: ascending on the column, the noun kept
        assert (got, mods.sort_word, mods.descending, mods.limit) == ("lines today", "output", False, 3)
        return
    assert got == rest
    assert (mods.sort_word, mods.descending, mods.limit, mods.from_end) == (sort, desc, limit, from_end)


def test_strip_leaves_a_question_without_modifiers_untouched():
    q = "  How many   PPM meetings does TAL have this week? "
    assert strip(q) == ("How many PPM meetings does TAL have this week?", Modifiers())
    assert not Modifiers() and Modifiers(limit=3)


@pytest.mark.parametrize("word,cols,want", [
    ("ship date", ["PCD", "ShipDate", "OrderQty"], "ShipDate"),
    ("shipdate", ["PCD", "ShipDate"], "ShipDate"),
    ("quantity", ["PCD", "ShipDate", "OrderQty"], "OrderQty"),
    ("qty", ["OrderQty", "ShipDate"], "OrderQty"),
    ("date", ["PCD", "ShipDate"], "ShipDate"),
    ("date", ["ShipDate", "ExfactoryDate"], None),             # two date columns: ambiguous, never a guess
    ("buyer", ["Buyer", "BuyerStyleNo"], "Buyer"),
    ("colour", ["PCD", "OrderQty"], None),
    ("dhu", ["Line", "DHU"], "DHU"),
    ("export po no", ["ExportPONo", "ExportOrderID"], "ExportPONo"),
])
def test_resolve_column(word, cols, want):
    assert resolve_column(word, cols) == want


def test_resolve_column_uses_synonyms():
    assert resolve_column("pieces", ["Line", "EndlineOutput"], {"EndlineOutput": ["pieces", "output"]}) == "EndlineOutput"


def test_order_clause_quotes_by_dialect():
    assert order_clause("OrderQty", True) == "[OrderQty] DESC"
    assert order_clause("ShipDate", False, "mysql") == "`ShipDate` ASC"


# ---------------------------------------------------------------- fixed tools
TOOL = {"name": "t", "database": "BitopiSplint", "match": r"(?i)\b(TAL|RHL)\b.*\borders\b",
        "params": {"factory": {"from_group": 1, "upper": True},
                   "order_by": {"type": "order_by", "default": "PCD, ExportOrderID", "then": "ExportOrderID",
                                "numbers": ["OrderQty"], "dates": ["PCD", "ShipDate"],
                                "columns": {"quantity": "OrderQty", "pcd": "PCD", "ship date": "ShipDate",
                                            "order": "ExportOrderID"}}},
        "sql": "SELECT TOP (200) ExportOrderID, OrderQty, PCD FROM rag.vw_ExportOrder WHERE Factory = @factory "
               "ORDER BY {order_by}"}
PLAIN = {"name": "p", "database": "BitopiSplint", "match": r"(?i)\b(TAL|RHL)\b.*\blines\b",
         "params": {"factory": {"from_group": 1, "upper": True}},
         "sql": "SELECT TOP (200) Line, DHU FROM rag.vw_LineDayQuality ORDER BY DHU DESC"}


def test_no_modifier_keeps_the_tools_own_order_and_params():
    tool, params = T.match_fixed_tool("TAL orders next week", [TOOL])
    assert params == {"factory": "TAL"}
    assert T.tool_sql(tool, params).endswith("ORDER BY PCD, ExportOrderID")


def test_a_sort_becomes_a_whitelisted_order_by():
    tool, params = T.match_fixed_tool("TAL orders sorted by quantity", [TOOL])
    assert params["order_by"] == "[OrderQty] DESC" and params["sort_label"] == "sorted by OrderQty (largest first)"
    assert T.tool_sql(tool, params).endswith("ORDER BY [OrderQty] DESC, ExportOrderID")
    _, p2 = T.match_fixed_tool("TAL orders sorted by pcd latest first", [TOOL])
    assert p2["order_by"] == "[PCD] DESC" and p2["sort_label"] == "sorted by PCD (latest first)"
    _, p3 = T.match_fixed_tool("TAL orders ordered by order", [TOOL])
    assert T.tool_sql(TOOL, p3).endswith("ORDER BY [ExportOrderID] ASC")      # never the column twice (error 169)


def test_a_sort_the_tool_cannot_apply_skips_it():
    assert T.match_fixed_tool("TAL orders sorted by colour", [TOOL]) is None
    assert T.match_fixed_tool("TAL lines sorted by line", [PLAIN]) is None
    assert T.match_fixed_tool("top 5 TAL lines", [PLAIN]) is not None        # a limit alone needs no parameter


def test_tool_sql_never_renders_a_clause_outside_the_whitelist():
    for evil in ("[OrderQty] DESC; DROP TABLE x", "OrderQty DESC", "[Salary] DESC", "[OrderQty] SIDEWAYS", 7):
        assert T.tool_sql(TOOL, {"order_by": evil}).endswith("ORDER BY PCD, ExportOrderID")


def test_no_word_of_the_question_reaches_the_sql():
    tool, params = T.match_fixed_tool("TAL orders sorted by quantity'; drop table x --", [TOOL])
    sql = T.tool_sql(tool, params)
    assert "drop" not in sql.lower() and "'" not in sql


def test_every_real_tool_still_renders_and_matches_its_examples():
    tools = T.load_fixed_tools()
    for t in tools:
        assert "{order_by}" not in T.tool_sql(t)
        if t.get("example"):
            hit = T.match_fixed_tool(t["example"], tools)
            assert hit is not None, t["name"]


def test_real_order_tool_sorts_on_request():
    tools = T.load_fixed_tools()
    tool, params = T.match_fixed_tool("Which TAL orders have a PCD next week sorted by quantity, top 10?", tools)
    assert tool["name"] == "orders_by_factory_pcd_window" and params["factory"] == "TAL"
    assert "ORDER BY [OrderQty] DESC, ExportOrderID" in T.tool_sql(tool, params)


# ---------------------------------------------------------------- the limit, applied in Python
def _res(n, tool="t", **params):
    return QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], sql="SELECT …",
                       tool=tool, columns=["ExportOrderID", "OrderQty"],
                       rows=[[f"TAL-{i}", 100 + i] for i in range(n)], params=dict(params),
                       as_of=datetime(2026, 10, 30, 9))


def test_apply_limit_cuts_a_copy_and_says_so():
    r = _res(34, factory="TAL")
    cut = T.apply_limit(r, "TAL orders top 10")
    assert len(cut.rows) == 10 and cut.rows[0] == ["TAL-0", 100] and len(r.rows) == 34
    assert cut.params["shown_label"] == "first 10" and cut.params["of_rows_label"] == "34"
    tail = T.apply_limit(r, "last 5 TAL orders")
    assert [x[0] for x in tail.rows] == ["TAL-29", "TAL-30", "TAL-31", "TAL-32", "TAL-33"]
    assert T.apply_limit(r, "TAL orders") is r and T.apply_limit(_res(3), "top 10") .rows == _res(3).rows


def test_a_limited_list_leads_with_the_true_count_and_verifies():
    tool = {"answer_list": "{n} {factory} order(s) have a PCD {window}"}
    r = T.apply_limit(_res(34, factory="TAL", window="next week",
                           sort_label="sorted by OrderQty (largest first)"), "top 10")
    assert modifier_note(r) == ", sorted by OrderQty (largest first); the first 10 are shown"
    text = try_template(r, tool)
    assert text.startswith("34 TAL order(s) have a PCD next week, sorted by OrderQty (largest first); "
                           "the first 10 are shown [D1]:")
    assert text.count("| TAL-") == 10
    ok, problems = verify(text, [], [r], "System doesn't have the data.")
    assert ok, problems


def test_modifier_lines_for_the_sql_prompt():
    assert T._modifier_lines("TAL orders", 200) == "(none)"
    lines = T._modifier_lines("TAL orders sorted by ship date latest first, top 500", 200)
    assert 'ORDER BY the column that holds "ship date", largest / latest first (DESC)' in lines
    assert "Return at most 200 rows" in lines                              # never more than data.max_rows


def test_a_built_in_list_at_its_row_limit_says_at_least():
    tool = {"answer_list": "{n} {factory} order(s) have a ship date {window}"}
    r = _res(200, factory="TAL", window="next week").model_copy(
        update={"sql": "SELECT TOP (200) ExportOrderID, OrderQty FROM rag.vw_ExportOrder ORDER BY ShipDate"})
    text = try_template(r, tool)
    assert text.startswith("at least 200 TAL order(s) have a ship date next week [D1]:")
    assert verify(text, [], [r], "System doesn't have the data.")[0]
    small = r.model_copy(update={"rows": r.rows[:30]})
    assert try_template(small, tool).startswith("30 TAL order(s)")
    top20 = r.model_copy(update={"rows": r.rows[:20], "sql": "SELECT TOP (20) ExportOrderID FROM rag.vw_ExportOrder"})
    assert try_template(top20, {"answer_list": "The next {n} PCD(s) from today"}).startswith("The next 20 PCD(s)")
