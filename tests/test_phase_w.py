"""Phase W (v1.19.0): ask for the missing key, then answer — required slots on fixed tools, the reply merged with
no model call, the "which goods receipt" rule, and summary answers whose chips drill into their own rows."""
import pytest

import ragbot.agent.orchestrator as orch
import ragbot.data.tools as tools
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent import router
from ragbot.agent.clarify import merge_reply
from ragbot.agent.state import ConversationState
from ragbot.agent.templated import follow_ups
from ragbot.auth.models import Scope
from ragbot.data import values
from ragbot.data.tools import ClarificationNeeded, load_fixed_tools, match_fixed_tool, missing_slot
from ragbot.models import QueryResult

TOOLS = load_fixed_tools()
CHIPS = ["Decor (Target USA)", "Decor (Meijer)", "H&M", "All tests of the last 90 days"]


def _tool(name):
    return next(t for t in TOOLS if t["name"] == name)


# ---------------------------------------------------------------- when a tool asks
@pytest.mark.parametrize("q,asks", [
    ("yarn count?", True),
    ("Show the fabric yarn count tests", True),
    ("yarn count of FR-26-2118", False),             # a file
    ("yarn count last week", False),                  # a period
    ("GSM test results", True),
    ("bowing test of each roll", True),
    ("bowing test of each roll of FR-26-2382", False),
    ("Bowing test details on 29 Oct 2026", False),     # the summary's own chip: a period
    ("Bowing test", False),                           # a summary is an answer: no slot
])
def test_a_tool_asks_only_when_no_key_and_no_period_is_named(q, asks):
    tool, params = match_fixed_tool(q, TOOLS)
    assert (missing_slot(tool, params) is not None) == asks, tool["name"]


def test_a_buyer_fills_the_slot(monkeypatch):
    hit = values.ValueHit("ppc_buyer", "H&M", "C/09/7", "H&M", 1.0, "Production", "")
    monkeypatch.setattr(values, "resolve", lambda q, scope, entity, **k: (hit, []) if entity == "ppc_buyer"
                        else (None, []))
    tool, params = match_fixed_tool("yarn count for H&M", TOOLS)
    assert params["buyer"] == "C/09/7" and missing_slot(tool, params) is None


def test_answer_from_data_raises_with_the_chips_before_anything_runs(monkeypatch):
    ran = []
    monkeypatch.setattr(tools, "run_fixed_tool", lambda *a, **k: ran.append(1))
    monkeypatch.setattr(tools, "slot_choices", lambda tool, spec, params, cats, user, scope: CHIPS)
    with pytest.raises(ClarificationNeeded) as e:
        tools.answer_from_data("yarn count?", scope=Scope.unrestricted())
    assert not ran
    c = e.value.clarify()
    assert e.value.tool == "fabric_yarn_count" and e.value.agent == "fabric_quality"
    assert c == {"slot": "buyer_or_file", "choices": CHIPS, "tool": "fabric_yarn_count", "entity": "ppc_buyer",
                 "all": "All tests of the last 90 days", "all_period": "last 90 days"}
    assert "buyer or file" in e.value.ask


def test_choices_are_names_from_the_tools_own_view_with_the_all_choice_last(monkeypatch):
    tool, params = match_fixed_tool("yarn count?", TOOLS)
    seen = {}

    def fake_run(t, p, cats, user="", refresh=False, *, scope, page=0):
        seen.update(name=t["name"], sql=t["sql"], scope=scope)
        return QueryResult(database="Production", engine="sqlserver", views=[], sql="", columns=["BuyerID"],
                           rows=[["C/19/2168"], ["C/99/1"], ["C/09/7"]])
    monkeypatch.setattr(tools, "run_fixed_tool", fake_run)
    monkeypatch.setattr(values, "names_for", lambda entity, codes, scope: {"C/19/2168": "Decor (Target USA)",
                                                                           "C/09/7": "H&M"})
    tal = Scope(factories=["TAL"])
    got = tools.slot_choices(tool, tool["slot"], params, {}, "u", tal)
    assert got == ["Decor (Target USA)", "H&M", "All tests of the last 90 days"]      # C/99/1 has no name: left out
    assert seen["name"] == "fabric_yarn_count:choices" and "rag.vw_FabricYarnCount" in seen["sql"]
    assert seen["scope"] is tal                                                         # the user's own scope


def test_names_for_keeps_only_visible_codes_of_the_entity():
    entries = [values.ValueEntry("ppc_buyer", "H&M", "C/09/7", values.GROUP, "Production"),
               values.ValueEntry("store", "H&M Store", "C/09/7", values.GROUP, "Inventory"),
               values.ValueEntry("ppc_buyer", "Decathlon", "C/18/1621", values.GROUP, "Production")]
    assert values.names_for("ppc_buyer", ["C/09/7", "C/1/1"], Scope.unrestricted(), entries) == {"C/09/7": "H&M"}


# ---------------------------------------------------------------- the reply
PENDING = {"slot": "buyer_or_file", "choices": CHIPS, "tool": "fabric_yarn_count", "entity": "ppc_buyer",
           "all": "All tests of the last 90 days", "all_period": "last 90 days", "question": "yarn count?"}


@pytest.mark.parametrize("reply,merged", [
    ("FR-26-2118", "yarn count for file FR-26-2118?"),
    ("fr-26-2118", "yarn count for file FR-26-2118?"),
    ("Decor (Target USA)", "yarn count for Decor (Target USA)?"),
    ("h&m", "yarn count for H&M?"),
    ("Decor (M", "yarn count for Decor (Meijer)?"),                    # the start of exactly one chip
    ("All tests of the last 90 days", "yarn count last 90 days?"),
    ("all", "yarn count last 90 days?"),
    ("Decor", None),                                                   # two chips start so: not a reply
    ("what about the bowing tests of yesterday please", None),         # a message of its own
])
def test_the_reply_completes_the_question(reply, merged):
    assert merge_reply(PENDING, reply) == merged


@pytest.mark.parametrize("q", ["yarn count for file FR-26-2118?", "yarn count last 90 days?"])
def test_a_completed_question_reaches_the_tool_without_asking_again(q):
    tool, params = match_fixed_tool(q, TOOLS)
    assert tool["name"] == "fabric_yarn_count" and missing_slot(tool, params) is None


def test_ask_then_reply_through_the_orchestrator(monkeypatch):
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: a)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(tools, "slot_choices", lambda tool, spec, params, cats, user, scope: CHIPS)
    monkeypatch.setattr(orch, "standalone_question",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no rewrite for a clarified reply")))
    first = orch.answer("yarn count?", scope=Scope.unrestricted())
    assert (first.route, first.kind, first.agent) == ("clarify", "clarify", "fabric_quality")
    assert first.clarify["choices"] == CHIPS and first.usage.calls == 0 and not first.not_found
    seen = []
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None, **k: seen.append(q) or [
        QueryResult(database="Production", engine="sqlserver", views=["rag.vw_FabricYarnCount"], sql="SELECT 1",
                    tool="fabric_yarn_count", columns=["FileRefID", "ActualEPI"], rows=[["FR-26-2118", 64]],
                    params={"file": "FR-26-2118", "file_label": "for file FR-26-2118", "window": "last 90 days",
                            "buyer_label": "", "factory_label": ""})])
    hist = [{"role": "user", "text": "yarn count?"},
            {"role": "assistant", "text": first.text, "q": "yarn count?", "route": "clarify", "kind": "clarify",
             "rewritten": "yarn count?", "clarify": first.clarify, "request_id": "r1"}]
    second = orch.answer("FR-26-2118", scope=Scope.unrestricted(), history=hist,
                         state=ConversationState.from_history(hist, "c1"))
    assert seen == ["yarn count for file FR-26-2118?"]
    assert second.route == "data" and "FR-26-2118" in second.text and second.usage.calls == 0


def test_the_both_route_answers_from_the_documents_when_the_data_half_would_ask(monkeypatch):
    from test_answer_stream import FakeChat, _chunk
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: a)
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "_route", lambda q, user: "both")
    monkeypatch.setattr(orch, "retrieve", lambda *a, **k: [_chunk()])
    monkeypatch.setattr(tools, "slot_choices", lambda *a, **k: CHIPS)
    chat = FakeChat(["Use form PCD-02 [P1]."])
    monkeypatch.setattr(orch, "get_chat", lambda: chat)
    a = orch.answer("yarn count and which form approves a PCD change?", scope=Scope.unrestricted())
    assert a.text == "Use form PCD-02 [P1]." and a.route == "both"
    assert any(w.startswith("data: would ask for buyer_or_file") for w in a.warnings)


# ---------------------------------------------------------------- "goods receipt": which system?
def test_goods_receipt_with_nothing_to_say_which_is_asked():
    assert router.route("Goods Receipt yesterday") == "data"
    rule = tools.clarification_rule("Goods Receipt yesterday")
    assert rule["name"] == "which_receipt_system" and rule["choices"] == ["Fabric received in PPC", "Store goods receipts"]
    for q in ["goods receipts of item 1178108 at company 02", "fabric received in PPC yesterday",
              "store goods receipts yesterday", "GRN GL/M-09-1026-705", "goods receipts at the central store"]:
        assert tools.clarification_rule(q) is None or tools.clarification_rule(q)["name"] != "which_receipt_system", q


@pytest.mark.parametrize("reply,merged", [
    ("Fabric received in PPC", "fabric received in PPC yesterday"),
    ("ppc", "fabric received in PPC yesterday"),
    ("Store goods receipts", "store goods receipts yesterday"),
    ("store", "store goods receipts yesterday"),
    ("both of them please", None),
])
def test_the_system_reply_swaps_the_phrase(reply, merged):
    pending = {"slot": "source", "choices": ["Fabric received in PPC", "Store goods receipts"],
               "rule": "which_receipt_system", "question": "Goods Receipt yesterday"}
    assert merge_reply(pending, reply) == merged


def test_the_ppc_reply_reaches_the_fabric_receipts_tool():
    tool, params = match_fixed_tool("fabric received in PPC yesterday", TOOLS)
    assert tool["name"] == "fabric_receipts_window" and params["window"] == "yesterday"


def test_the_receipt_question_is_asked_with_no_model_call(monkeypatch):
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: a)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    a = orch.answer("Goods Receipt yesterday", scope=Scope.unrestricted())
    assert a.route == "clarify" and a.clarify["rule"] == "which_receipt_system" and a.usage.calls == 0
    assert a.clarify["choices"] == ["Fabric received in PPC", "Store goods receipts"]


# ---------------------------------------------------------------- a summary's chips drill into its rows
def test_summary_chips_are_its_own_files_and_reach_the_roll_by_roll_tool():
    tool = _tool("fabric_bowing_summary")
    r = QueryResult(database="Production", engine="sqlserver", views=[], sql="", tool=tool["name"],
                    columns=["BuyerID", "FileRefID", "RollsTested"],
                    rows=[["C/18/1621", "FR-26-2382", 14], ["C/09/7", "FR-26-2051", 10], ["C/09/7", "FR-26-2051", 3],
                          ["C/24/3541", "FR-26-2237", 8], ["C/09/7", "FR-26-2194", 5]],
                    params={"window": "on 29 Oct 2026", "window_note": ""})
    chips = follow_ups(r, tool, asked="Bowing test summary on 29 Oct 2026")
    assert chips[:3] == ["Bowing test details for file FR-26-2382 on 29 Oct 2026",
                         "Bowing test details for file FR-26-2051 on 29 Oct 2026",
                         "Bowing test details for file FR-26-2237 on 29 Oct 2026"]
    assert "Shrinkage test summary on 29 Oct 2026" in chips
    for c in chips[:3]:
        t, params = match_fixed_tool(c, TOOLS)
        assert t["name"] == "fabric_bowing_by_roll" and params["file"] and missing_slot(t, params) is None


@pytest.mark.parametrize("name", ["Decor (Target USA)", "Carter’s | OshKosh B’gosh", "H&M"])
def test_buyer_names_in_a_completed_question_survive(name, monkeypatch):
    hit = values.ValueHit("ppc_buyer", name, "C/1/1", name, 1.0, "Production", "")
    monkeypatch.setattr(values, "resolve", lambda q, scope, entity, **k: (hit, []) if entity == "ppc_buyer"
                        else (None, []))
    tool, params = match_fixed_tool(f"yarn count for {name}?", TOOLS)
    assert tool["name"] == "fabric_yarn_count" and params["buyer"] == "C/1/1" and missing_slot(tool, params) is None
