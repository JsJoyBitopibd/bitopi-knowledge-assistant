"""G3: follow-up question chips under fixed-tool answers, and the per-tool example questions."""
import pytest

from ragbot.agent import orchestrator as orch
from ragbot.agent.templated import follow_ups
from ragbot.auth.models import Scope
from ragbot.data import tools
from ragbot.data.tools import load_fixed_tools, match_fixed_tool
from ragbot.models import QueryResult

TOOLS = load_fixed_tools()


@pytest.mark.parametrize("tool", TOOLS, ids=[t["name"] for t in TOOLS])
def test_every_tool_has_an_example_that_reaches_it(tool):
    hit = match_fixed_tool(tool["example"], TOOLS)
    assert hit is not None and hit[0]["name"] == tool["name"], f"reaches {hit[0]['name'] if hit else None}"


@pytest.mark.parametrize("tool", [t for t in TOOLS if t.get("follow_ups")], ids=lambda t: t["name"])
def test_every_follow_up_reaches_a_fixed_tool(tool):
    """A click on a chip should answer at once, without a SQL-generation call."""
    _, params = match_fixed_tool(tool["example"], TOOLS)
    r = QueryResult(database="DB", engine="sqlserver", views=[], sql="", params=params, tool=tool["name"],
                    columns=["n"], rows=[[1]])
    qs = follow_ups(r, tool, asked=tool["example"])
    assert qs, "no follow-up could be filled from the example's parameters"
    for q in qs:
        assert "{" not in q and "}" not in q
        assert match_fixed_tool(q, TOOLS) is not None, f"{q!r} would need generated SQL"


def _result(**params):
    return QueryResult(database="DB", engine="sqlserver", views=[], sql="", params=params, tool="t",
                       columns=["n"], rows=[[1]])


def test_templates_are_filled_skipped_deduplicated_and_capped():
    tool = {"follow_ups": ["Which {factory} orders ship {window}?", "PPM meetings for {factory} {window}",
                           "Status of {eo}?", "Which {factory} orders ship {window}?", "Next PCDs at {factory}?",
                           "Cancelled {window}?"]}
    got = follow_ups(_result(factory="TAL", window="next week"), tool, asked="PPM meetings for TAL next week")
    assert got == ["Which TAL orders ship next week?", "Next PCDs at TAL?", "Cancelled next week?"]
    assert follow_ups(_result(), None) == [] and follow_ups(_result(), {"follow_ups": ["{eo}"]}) == []


def test_a_templated_answer_carries_its_follow_ups(monkeypatch):
    import ragbot.index_version as iv
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: a)
    tool = next(t for t in TOOLS if t["name"] == "orders_count_by_factory_ship_window")
    _, params = match_fixed_tool(tool["example"], TOOLS)
    result = QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportOrder"], sql=tool["sql"],
                         params=params, tool=tool["name"], columns=["Orders"], rows=[[432]])
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [result])
    a = orch.answer(tool["example"], scope=Scope.unrestricted())
    assert "templated answer (no model call)" in a.warnings
    assert a.follow_ups == ["Which TAL orders have a ship date next week?", "Which TAL orders have a PCD next week?"]
