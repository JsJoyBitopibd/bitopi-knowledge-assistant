"""A model-written list says what its query looked for (agent/explain.py, 2026-10-26)."""
from datetime import date, datetime

import ragbot.agent.orchestrator as orch
from ragbot.agent.citations import verify
from ragbot.agent.explain import describe, described_params, lead
from ragbot.agent.templated import try_template
from ragbot.auth.models import Scope
from ragbot.models import QueryResult

from test_templated import wired  # noqa: F401  (fixture)

NF = "System doesn't have the data."
TODAY = {"today": date(2026, 10, 26)}
AT_RISK = ("SELECT TOP (100) ExportOrderID, ExportPONo, Buyer, ShipDate, OrderQty, Factory, Merchandiser\n"
           "FROM rag.vw_OrderShipment\nWHERE ShipDate < @today AND InvoicedQty IS NULL\n"
           "ORDER BY ShipDate, ExportOrderID")


def _qr(sql, cols, rows, params=None, tool="generated", engine="sqlserver"):
    return QueryResult(database="BitopiSplint", engine=engine, views=["rag.vw_OrderShipment"], sql=sql, tool=tool,
                       params=dict(TODAY if params is None else params), columns=cols, rows=rows)


def _shipments(n):
    return [[f"TAL-25-733-{i}", "469606-9192", "H&M", datetime(2025, 10, 27), 196, "TAL", "Rahim"] for i in range(n)]


COLS = ["ExportOrderID", "ExportPONo", "Buyer", "ShipDate", "OrderQty", "Factory", "Merchandiser"]


def test_the_at_risk_question_says_what_at_risk_was_taken_to_mean():
    """Live 2026-10-26: "Which Shipments are at risk?" answered "Found 100 matching rows [D1]:" and nothing else."""
    r = _qr(AT_RISK, COLS, _shipments(100))
    text = try_template(r, None, allow_generated=True)
    assert text.startswith("Found 100 rows where ShipDate is before today (2026-10-26) and InvoicedQty is empty, "
                           "sorted by ShipDate (earliest first), then ExportOrderID [D1]. "
                           "The query returns at most 100 rows, so there may be more.\n\n| ExportOrderID |")
    assert verify(text, [], [r], NF)[0]


def test_no_cap_note_below_the_limit():
    text = try_template(_qr(AT_RISK, COLS, _shipments(12)), None, allow_generated=True)
    assert text.startswith("Found 12 rows where ShipDate is before today (2026-10-26)") and "may be more" not in text


def test_literal_values_are_licensed_through_params():
    sql = ("SELECT TOP (200) Buyer, SUM(OrderQty) AS Total FROM rag.vw_OrderShipment WHERE (Factory = 'TAL' OR "
           "Factory = 'RHL') AND OrderQty > 500 GROUP BY Buyer ORDER BY Total DESC")
    r = _qr(sql, ["Buyer", "Total"], [["H&M", 9000], ["Zara", 700]], params={})
    d = describe(r)
    assert lead(d, 2, "D1") == ("Found 2 rows (one per Buyer) where Factory is TAL or RHL and OrderQty is above 500, "
                                "sorted by Total (highest first) [D1].")
    assert described_params(r) == {"Factory": "TAL, RHL", "OrderQty": "500"}
    text = try_template(r, None, allow_generated=True)
    assert not verify(text, [], [r], NF)[0]                       # 500 is in no row …
    licensed = r.model_copy(update={"params": described_params(r)})
    assert verify(text, [], [licensed], NF)[0]                    # … the card's Filter line holds it


def test_each_condition_shape():
    sql = ("SELECT a FROM t WHERE ShipDate BETWEEN @today AND DATEADD(day, 7, @today) AND Style LIKE '%ab%' "
           "AND Status <> 'Closed' AND LineNo NOT IN (1, 2) AND PCD IS NOT NULL")
    d = describe(_qr(sql, ["a"], [[1], [2]]))
    assert d.where == ["ShipDate is from today (2026-10-26) to 2026-11-02", "Style contains ab",
                       "Status is not Closed", "LineNo is not 1 or 2", "PCD is not empty"]
    assert not d.other and d.params["ShipDate"] == "2026-11-02"


def test_dates_numbers_and_parts():
    sql = ("SELECT a FROM t WHERE YEAR(ShipDate) = 2026 AND MONTH(ShipDate) >= 10 AND d >= CAST(GETDATE() AS date) "
           "AND e < GETDATE() AND @today > f")
    d = describe(_qr(sql, ["a"], [[1], [2]]))
    assert d.where == ["the year of ShipDate is 2026", "the month of ShipDate is at least 10", "d is on or after today",
                       "e is before now", "f is before today (2026-10-26)"]


def test_what_cannot_be_stated_is_pointed_at_not_paraphrased():
    d = describe(_qr("SELECT a FROM t WHERE DATEDIFF(day, ShipDate, @today) > 7 AND Factory = 'TAL'", ["a"],
                     [[1], [2]]))
    assert d.where == ["Factory is TAL"] and d.other
    assert lead(d, 2, "D1") == "Found 2 rows where Factory is TAL and other conditions shown under Show query [D1]."
    # a CTE's own WHERE, a HAVING, a JOIN on a value: the outer WHERE is not the whole story
    for sql in ("WITH x AS (SELECT a FROM t WHERE b = 1) SELECT a FROM x WHERE a = 'Z'",
                "SELECT a, COUNT(*) AS n FROM t WHERE a = 'Z' GROUP BY a HAVING COUNT(*) > 2",
                "SELECT t.a FROM t JOIN u ON t.id = u.id AND u.Status = 'Open' WHERE t.a = 'Z'"):
        assert describe(_qr(sql, ["a"], [[1], [2]])).other, sql
    # a plain key join is how two tables meet, not a filter
    assert not describe(_qr("SELECT t.a FROM t JOIN u ON t.id = u.id WHERE t.a = 'Z'", ["a"], [[1], [2]])).other


def test_too_many_conditions_are_cut_to_a_pointer():
    sql = "SELECT a FROM t WHERE " + " AND ".join(f"c{i} = 'v'" for i in range(7))
    d = describe(_qr(sql, ["a"], [[1], [2]]))
    assert len(d.where) == 5 and d.other


def test_mysql_limit_and_placeholder():
    d = describe(_qr("SELECT a FROM t WHERE ShipDate < :today ORDER BY a DESC LIMIT 2", ["a"], [["x"], ["y"]],
                     engine="mysql"))
    assert d.where == ["ShipDate is before today (2026-10-26)"] and d.order == "sorted by a (Z to A)" and d.capped


def test_not_described():
    assert describe(_qr(AT_RISK, COLS, _shipments(3), tool="orders_by_factory_pcd_window")) is None   # hand-written lead
    bad = _qr(AT_RISK, COLS, _shipments(3)); bad.error = "timeout"
    assert describe(bad) is None
    assert describe(_qr("SELECT a FROM t UNION SELECT a FROM u", ["a"], [[1], [2]])) is None
    assert describe(_qr("SELECT a FROM t", ["a"], [[1], [2]])) is None                  # nothing to say
    plain = try_template(_qr("SELECT a FROM t", ["a"], [[1], [2]]), None, allow_generated=True)
    assert plain.startswith("Found 2 matching rows [D1]:")


def test_one_row_names_its_conditions():
    r = _qr("SELECT TOP (200) Buyer, ShipDate FROM t WHERE ExportOrderID = 'TAL-25-733-187'", ["Buyer", "ShipDate"],
            [["H&M", datetime(2025, 10, 27)]], params={})
    text = try_template(r, None, allow_generated=True)
    assert text == "One row where ExportOrderID is TAL-25-733-187: Buyer: H&M · ShipDate: 2025-10-27 [D1]."
    assert verify(text, [], [r.model_copy(update={"params": described_params(r)})], NF)[0]


def test_orchestrator_states_the_query_and_licenses_its_values(wired):  # noqa: F811
    sql = "SELECT TOP (200) Buyer, OrderQty FROM rag.vw_OrderShipment WHERE OrderQty > 500 ORDER BY OrderQty DESC"
    wired(_qr(sql, ["Buyer", "OrderQty"], [["H&M", 900], ["Zara", 700]], params={}), [])   # any model call fails
    a = orch.answer("which orders are big", scope=Scope.unrestricted())
    assert a.text.startswith("Found 2 rows where OrderQty is above 500, sorted by OrderQty (highest first) [D1].")
    assert a.usage.calls == 0 and not a.not_found
    assert a.results[0].params == {"OrderQty": "500"} and a.references[0].params == {"OrderQty": "500"}


def test_orchestrator_falls_back_to_the_plain_lead_when_the_sentence_does_not_verify(wired):  # noqa: F811
    """A column whose name reads as a code ("SMV10") is not in the rows' pool: the plain template is used."""
    sql = "SELECT TOP (200) Style FROM t WHERE SMV10 IS NULL"
    wired(_qr(sql, ["Style"], [["A"], ["B"]], params={}), [])
    a = orch.answer("styles without smv", scope=Scope.unrestricted())
    assert a.text.startswith("Found 2 matching rows [D1]:") and a.usage.calls == 0
