"""Phase T3: follow-ups that reshape the previous answer's rows in Python (agent/refine.py)."""
import time
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent import refine
from ragbot.agent.citations import verify
from ragbot.agent.events import Final, Stage
from ragbot.agent.state import ConversationState
from ragbot.agent.templated import try_template
from ragbot.auth.models import Scope
from ragbot.models import QueryResult

NF = "System doesn't have the data."
FACTORIES = ["TAL", "RHL", "BGL"]
BUYERS = ["Zara", "H&M", "Next", "Primark"]


def _rows(n=143):
    rows = []
    for i in range(n):
        rows.append([f"{FACTORIES[i % 3]}-25-{1000 + i}-{i % 7}", FACTORIES[i % 3], BUYERS[i % 4], f"L-{2100 + i % 5}",
                     Decimal(100 + (i * 37) % 900) if i % 11 else None,
                     date(2026, 11, 1) + timedelta(days=i % 20), Decimal("2.5") + Decimal(i % 4)])
    return rows


COLS = ["ExportOrderID", "Factory", "Buyer", "Line", "OrderQty", "ShipDate", "DHU"]


def _prior(n=143, sql="SELECT TOP (200) * FROM rag.vw_ExportOrder", tool="orders_by_factory_ship_window", **kw):
    return QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], sql=sql, tool=tool,
                       columns=COLS, rows=_rows(n), key_columns=["ExportOrderID"], params={"window": "next week"},
                       as_of=datetime(2026, 10, 30, 9, 15), **kw)


P = _prior()


def plan(msg, r=P):
    return refine.parse(msg, r)


# ---------------------------------------------------------------- reading the follow-up
def test_filter_on_a_value_the_rows_hold():
    p = plan("only TAL")
    assert p.filters[0].column == "Factory" and p.filters[0].op == "eq" and p.filters[0].value == "TAL"
    assert plan("TAL only").filters[0].value == "TAL"
    assert plan("RHL").filters[0].value == "RHL"                       # a reply that is only a value
    both = plan("only TAL and RHL")
    assert both.filters[0].op == "in" and both.filters[0].value == ["TAL", "RHL"]
    assert plan("only zara").filters[0].value == "Zara"               # as recorded, whatever the case


def test_filters_with_an_operator():
    assert plan("OrderQty above 500").filters[0].model_dump() == {"column": "OrderQty", "op": "gt",
                                                                 "value": Decimal(500), "value2": None}
    f = plan("ship date between 2026-11-03 and 2026-11-05").filters[0]
    assert (f.op, f.value, f.value2) == ("between", date(2026, 11, 3), date(2026, 11, 5))
    assert plan("where buyer is next").filters[0].value == "Next"
    assert plan("buyer contains ar").filters[0].op == "contains"
    assert plan("buyer above 5") is None                               # a text column has no "above"


def test_group_count_total_distinct():
    assert plan("line wise").group_by == ["Line"]
    assert plan("factory and buyer wise").group_by == ["Factory", "Buyer"]
    assert plan("break it down by buyer").group_by == ["Buyer"]
    assert plan("how many?").scalar == "count" and plan("how many lines").of_column == "Line"
    t = plan("total quantity")
    assert (t.scalar, t.of_column) == ("total", "OrderQty")
    assert plan("their names").distinct == "Buyer"
    assert plan("which buyers").distinct == "Buyer"


def test_sort_limit_and_columns():
    p = plan("sort by ship date latest first, top 10")
    assert p.sort == ("ShipDate", True) and p.limit == 10
    assert plan("sort by quantity").sort == ("OrderQty", True)       # numbers: largest first unless asked
    assert plan("last 5").limit == 5 and plan("last 5").from_end
    assert plan("hide DHU").keep == ["ExportOrderID", "Factory", "Buyer", "Line", "OrderQty", "ShipDate"]
    assert plan("only show buyer and line").keep == ["Buyer", "Line"]
    assert plan("only TAL, sorted by quantity").filters[0].value == "TAL"


def test_what_the_rows_cannot_answer_is_marked_for_a_requery():
    assert plan("style wise").unresolved == ["style"]                 # no Style column: the SQL must regroup
    assert plan("show the style beside each line").add == ["style"]
    assert plan("total DHU").unresolved == ["DHU"]                    # a rate is never added up in Python
    assert plan("sort by colour").unresolved == ["colour"]
    assert plan("next").page == 1 and plan("show the next 200 rows").page == 1
    assert plan("last week?").window == "last week"
    assert plan("what about yesterday").window == "yesterday"
    for msg in ("style wise", "next", "last week?", "show the style beside each line"):
        assert refine.needs_requery(plan(msg), P, 200) is not None


def test_anything_else_is_not_a_refine():
    for msg in ("What is the PCD for export order TAL-25-1493-215?", "Which RHL orders have a ship date next week?",
                "show TAL orders with a PCD next week", "hi", "who approves a PCD change?", "b+ blood group",
                "SELECT * FROM rag.vw_ExportOrder", "", "sort it", "only foo"):
        assert plan(msg) is None, msg


def test_a_cut_result_is_never_filtered_or_sorted_in_python():
    cut = _prior(200)
    assert refine.capped(cut, 200) and refine.needs_requery(plan("only TAL", cut), cut, 200) == "capped"
    assert refine.needs_requery(plan("top 5", cut), cut, 200) is None        # the first 5 of what is shown is fine
    small_top = _prior(20, sql="SELECT TOP (20) * FROM rag.vw_ExportOrder")
    assert refine.capped(small_top, 200)                                     # the tool's own TOP (20) was reached
    assert not refine.capped(_prior(19, sql="SELECT TOP (20) * FROM x"), 200)


# ---------------------------------------------------------------- applying it
def _apply(msg, r=P):
    return refine.apply(plan(msg, r), r, parent="req1")


def test_apply_filters_sorts_and_cuts_a_copy():
    d = _apply("only TAL, sort by quantity, top 5")
    assert len(d.rows) == 5 and all(x[1] == "TAL" for x in d.rows)
    qty = [x[4] for x in d.rows]
    assert qty == sorted(qty, reverse=True) and None not in qty
    assert len(P.rows) == 143 and P.transform == ""                         # the earlier result is untouched
    assert d.as_of == P.as_of and d.sql == P.sql and d.database == P.database and d.views == P.views
    assert d.transform == "where Factory is TAL, sorted by OrderQty (largest first), first 5"
    assert d.derived_from == "req1" and d.source_rows == 143 and d.tool == "orders_by_factory_ship_window:refined"


def test_empty_cells_sort_last_in_both_directions():
    for msg in ("sort by quantity", "sort by quantity ascending"):
        qty = [x[4] for x in _apply(msg).rows]
        assert qty[-1] is None and None not in qty[:-13]


def test_group_counts_and_sums_but_never_a_rate():
    d = _apply("line wise")
    assert d.columns == ["Line", "Count", "OrderQty"]                        # DHU is a rate: not re-added
    assert sum(x[1] for x in d.rows) == 143
    assert sum(x[2] for x in d.rows) == sum(x[4] for x in P.rows if x[4] is not None)


def test_count_total_distinct_values():
    assert _apply("how many").rows == [[143]]
    assert _apply("how many lines").rows == [[5]]
    assert _apply("only TAL, how many").rows == [[48]]
    assert _apply("total quantity").rows[0][0] == sum(x[4] for x in P.rows if x[4] is not None)
    assert [x[0] for x in _apply("their names").rows] == sorted(BUYERS, key=str.casefold)


# ---------------------------------------------------------------- the answer verifies, and costs no model call
@pytest.mark.parametrize("msg", ["only TAL", "line wise", "how many", "total quantity", "their names",
                                 "sort by ship date latest first, top 10", "OrderQty above 500", "only foo bar",
                                 "only TAL and RHL", "hide DHU", "last 5"])
def test_every_derived_answer_verifies(msg):
    p = plan(msg)
    if p is None:
        return
    d = refine.apply(p, P, parent="r")
    text = try_template(d, None)
    ok, problems = verify(text, [], [d], NF)
    assert ok, (msg, text[:200], problems)
    assert "143" in text and "[D1]" in text


def test_an_empty_filter_says_so_and_verifies():
    d = refine.apply(refine.RefinePlan(filters=[refine.Filter(column="OrderQty", op="gt", value=Decimal(99999))]), P)
    text = try_template(d, None)
    assert text == "None of the earlier answer's 143 rows match: where OrderQty is above 99999 [D1]."
    assert verify(text, [], [d], NF)[0]


def test_parse_and_apply_are_fast():
    big = _prior(199)
    t0 = time.perf_counter()
    for msg in ("only TAL, sort by quantity, top 10", "line wise", "how many lines"):
        refine.apply(refine.parse(msg, big), big)
    assert time.perf_counter() - t0 < 0.3


def test_every_suggested_chip_parses_and_needs_no_requery():
    chips = refine.suggestions(P)
    assert chips and len(chips) <= 5
    for c in chips:
        p = refine.parse(c, P)
        assert p is not None and refine.needs_requery(p, P, 200) is None, c
    # a cut result: only the next page (read with the same statement, T4) and a cut are offered
    assert refine.suggestions(_prior(200)) == ["Show the next 200 rows", "Top 10"]
    assert refine.needs_requery(refine.parse("Show the next 200 rows", _prior(200)), _prior(200), 200) == "page"
    no_pages = _prior(200, tool="production_dashboard_window")              # keeps one factory in Python
    assert refine.suggestions(no_pages) == ["Top 10"]
    one = P.model_copy(update={"columns": ["Count"], "rows": [[5]]})
    assert refine.suggestions(one) == []


# ---------------------------------------------------------------- through the orchestrator
@pytest.fixture
def wired(monkeypatch):
    logged = []
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: logged.append(a) or a)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)

    def boom(*a, **k):
        raise AssertionError("a refine must not call a model, route, search or query")
    for name in ("get_chat", "retrieve", "_route", "standalone_question"):
        monkeypatch.setattr(orch, name, boom)
    return logged


def _state(r=P):
    hist = [{"role": "user", "text": "Which TAL orders ship next week?"},
            {"role": "assistant", "text": "…", "q": "Which TAL orders ship next week?", "results": [r],
             "request_id": "req1", "route": "data"}]
    return ConversationState.from_history(hist, "c1")


def test_orchestrator_answers_a_refine_without_any_model_call(wired):
    evs = list(orch.answer_stream("only TAL, top 5", scope=Scope.unrestricted(), state=_state()))
    assert [e.name for e in evs if isinstance(e, Stage)] == ["understanding", "refining"]
    a = evs[-1].answer
    assert isinstance(evs[-1], Final) and a.route == "refine" and a.kind == "refine" and a.usage.calls == 0
    assert a.prior_request_id == "req1" and not a.not_found
    assert a.text.startswith("From the earlier answer's 143 rows, where Factory is TAL, first 5: 5 rows [D1]:")
    ref = a.references[0]
    assert ref.transform == "where Factory is TAL, first 5" and ref.source_rows == 143 and ref.as_of == P.as_of
    assert a.refine_suggestions and all(refine.parse(c, a.results[0]) for c in a.refine_suggestions)
    assert wired == [a]


def test_a_follow_up_the_rows_cannot_answer_goes_on_as_usual(monkeypatch, wired):
    monkeypatch.setattr(orch, "_route", lambda q, user: "refuse")         # the usual path, stopped early
    monkeypatch.setattr(orch, "standalone_question", lambda q, h, u="": q)
    import ragbot.data.requery as rq
    monkeypatch.setattr(rq, "requery", lambda *a, **k: None)              # T4 finds no safe way to re-run it
    a = list(orch.answer_stream("style wise", history=[{"role": "user", "text": "x"}], scope=Scope.unrestricted(),
                                state=_state()))[-1].answer
    assert a.route == "refuse" and a.kind == ""
    cut = list(orch.answer_stream("only TAL", history=[{"role": "user", "text": "x"}], scope=Scope.unrestricted(),
                                  state=_state(_prior(200))))[-1].answer
    assert cut.route == "refuse"


def test_a_full_page_is_a_cut_like_the_first_page():
    page2 = _prior(200).model_copy(update={"transform": "page 2", "page": 1,
                                           "sql": "SELECT a FROM rag.v ORDER BY a OFFSET 200 ROWS FETCH FIRST 200 ROWS ONLY"})
    assert refine.capped(page2, 200) and refine.needs_requery(plan("only TAL", page2), page2, 200) == "capped"
    assert refine.suggestions(page2) == ["Show the next 200 rows", "Top 10"]
    last_page = page2.model_copy(update={"rows": page2.rows[:37]})
    assert not refine.capped(last_page, 200)


def test_chips_keep_acronyms_and_never_add_up_counts():
    assert refine._chip_words("PCD") == "PCD" and refine._chip_words("ShipDate") == "ship date"
    assert refine._chip_words("OrderQty") == "order qty" and refine._chip_words("ExportPONo") == "export PO no"
    grouped = refine.apply(plan("line wise"), P)
    assert not any(c.startswith("Total count") for c in refine.suggestions(grouped))
