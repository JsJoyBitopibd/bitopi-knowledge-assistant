"""Phase K1/K3: domain-agent registry, agent picking without a model, charters in the prompts, and the
fixed-tool engine changes the Stage-1 tools need (single-day windows, default windows, @today)."""
from datetime import date, timedelta

import pytest

import ragbot.agent.orchestrator as orch
import ragbot.data.tools as tools
import ragbot.index_version as iv
import ragbot.llm.base as base
from ragbot.agent.events import Final
from ragbot.auth.models import Scope
from ragbot.data.catalog import load_catalogs
from ragbot.domain_agents import Agent, charter_text, load_agents, pick_agent, validate_agents
from ragbot.llm.base import ChatModel, ChatReply
from ragbot.models import QueryResult


# ---------------------------------------------------------------- registry
def test_real_agents_load_and_validate():
    agents = load_agents()
    assert {"order", "finance_lc", "sourcing", "production"} <= set(agents)
    problems = validate_agents(agents, load_catalogs(), tools.load_fixed_tools())
    assert problems == []


def test_every_fixed_tool_names_an_agent_that_owns_its_database():
    agents = load_agents()
    for t in tools.load_fixed_tools():
        assert t.get("agent") in agents, t["name"]
        assert t["database"] in agents[t["agent"]].catalogs, t["name"]


def test_validate_reports_unknown_agent_catalog_and_charter(tmp_path):
    a = Agent(name="x", title="X", catalogs=("NoSuchDb",), keywords=("x",), charter="agents/missing")
    problems = validate_agents({"x": a}, {}, [{"name": "t1", "agent": "y", "database": "D"}], prompts_dir=tmp_path)
    assert any("NoSuchDb" in p for p in problems)
    assert any("charter" in p for p in problems)
    assert any("unknown agent 'y'" in p for p in problems)


# ---------------------------------------------------------------- picking
_AGENTS = {
    "fin": Agent(name="fin", title="Fin", keywords=("lc", "back-to-back", "expire")),
    "src": Agent(name="src", title="Src", keywords=("fabric", "supplier")),
}


def test_tool_tag_decides_the_agent():
    assert pick_agent("anything at all", {"agent": "src"}, _AGENTS).name == "src"


def test_most_keywords_win_and_phrases_match_with_spaces_or_hyphens():
    assert pick_agent("Which back to back LCs expire next month?", agents=_AGENTS).name == "fin"
    assert pick_agent("Which supplier ships fabric late?", agents=_AGENTS).name == "src"


def test_tie_or_no_keyword_means_no_agent():
    assert pick_agent("Which supplier LC?", agents=_AGENTS) is None          # 1 : 1
    assert pick_agent("How many employees are there?", agents=_AGENTS) is None
    assert pick_agent("Which lcs?", agents=_AGENTS) is None                  # whole words only


def test_real_agents_pick_the_expected_domain():
    agents = load_agents()
    assert pick_agent("Which back-to-back LCs expire next month?", agents=agents).name == "finance_lc"
    assert pick_agent("Which sewing lines missed their target yesterday?", agents=agents).name == "production"
    assert pick_agent("Which suppliers delivered fabric late?", agents=agents).name == "sourcing"


def test_charter_text_is_a_titled_block_or_empty():
    block = charter_text("finance_lc")
    assert block.startswith("Domain notes (Finance/LC agent):") and "rag.vw_ExportLC" in block
    assert charter_text("") == "" and charter_text("nobody") == ""


# ---------------------------------------------------------------- fixed-tool engine
def test_single_day_windows():
    today = date.today()
    assert tools._date_window("today") == (today, today + timedelta(days=1))
    assert tools._date_window("yesterday") == (today - timedelta(days=1), today)
    assert tools._date_window("tomorrow") == (today + timedelta(days=1), today + timedelta(days=2))


def test_default_window_and_today_param():
    hit = tools.match_fixed_tool("How many orders are behind schedule?", tools.load_fixed_tools())
    assert hit is not None
    tool, params = hit
    assert tool["name"] == "orders_past_ship_not_invoiced_count"
    today = date.today()
    assert params["window"] == "last 30 days" and params["from"] == today - timedelta(days=30)
    assert params["today"] == today


def test_window_phrase_overrides_the_default():
    tool, params = tools.match_fixed_tool("Which TAL orders are behind schedule this month?", tools.load_fixed_tools())
    assert tool["name"] == "orders_past_ship_not_invoiced_by_factory"
    assert params["factory"] == "TAL" and params["window"] == "this month"
    assert params["from"] == date.today().replace(day=1)


def test_stage1_questions_reach_their_tools():
    ts = tools.load_fixed_tools()
    expect = {
        "How many back-to-back LCs expire next month?": "bblcs_expiring_count_window",
        "Which BTB LCs expire this week?": "bblcs_expiring_window",
        "Which LCs expire in the next 30 days?": "lcs_expiring_window",
        "Which RHL lines are below target?": "lines_below_target_by_factory",
        "Which lines missed their target?": "lines_below_target",
        "Which orders are past their ship date?": "orders_past_ship_not_invoiced",
    }
    for q, name in expect.items():
        hit = tools.match_fixed_tool(q, ts)
        assert hit is not None and hit[0]["name"] == name, q


def test_ranking_and_grouping_questions_skip_the_stage1_list_tools():
    """Found in the app 2026-10-06: "which suppliers have the most …" was answered with the plain list.
    Ranking / grouping questions go to model-written SQL (with the agent's charter) instead."""
    ts = tools.load_fixed_tools()
    for q in ["Which suppliers have the most back-to-back LCs expiring next month?",
              "How many back-to-back LCs per supplier expire next month?",
              "Which LCs expire next month by buyer?",
              "Which factory has the most lines below target?",
              "Top buyers with orders behind schedule"]:
        hit = tools.match_fixed_tool(q, ts)
        assert hit is None, (q, hit and hit[0]["name"])


def test_agent_catalog_is_added_only_when_the_ranking_missed_it(monkeypatch):
    agent = Agent(name="a", title="A", catalogs=("Own",))
    cats = {"Own": "own", "X": "x", "Y": "y"}
    monkeypatch.setattr(tools, "pick_catalogs", lambda q, c, qvec=None, k=None: {n: c[n] for n in list(c)[:k or 2]})
    assert list(tools._with_agent_catalog("q", {"X": "x", "Y": "y"}, cats, agent, None)) == ["X", "Own"]
    assert list(tools._with_agent_catalog("q", {"Own": "own", "X": "x"}, cats, agent, None)) == ["Own", "X"]


# ---------------------------------------------------------------- charter in the answer prompt
class RecordingChat(ChatModel):
    name = "fake"

    def __init__(self, reply):
        self.reply, self.systems = reply, []

    def _chat(self, messages, system, max_tokens, temperature):
        raise AssertionError("the answer path must stream")

    def _stream(self, messages, system, max_tokens, temperature):
        self.systems.append(system)
        yield self.reply
        yield ChatReply(text=self.reply, input_tokens=1, output_tokens=1, model="fake", stop_reason="stop")


@pytest.fixture
def data_answer(monkeypatch):
    logged = []
    orch._ANSWERS.clear()
    monkeypatch.setattr(base, "_log_call", lambda *a: None)
    monkeypatch.setattr(orch, "_log", lambda a, user, scope=None: logged.append(a) or a)
    monkeypatch.setattr(orch, "_log_verify_failure", lambda *a: None)
    monkeypatch.setattr(iv, "refresh_if_changed", lambda: False)
    monkeypatch.setattr(orch, "_route", lambda q, user: "data")
    monkeypatch.setattr(orch, "_templated_answer", lambda *a, **k: False)    # force the model path
    monkeypatch.setattr(tools, "needs_clarification", lambda q: None)
    result = QueryResult(database="BitopiSplint", engine="sqlserver", views=["rag.vw_ExportLC"],
                         sql="SELECT MasterLCRef, ExpiryDate FROM rag.vw_ExportLC", columns=["MasterLCRef", "ExpiryDate"],
                         rows=[["TAL/SRS/ZARA/667/026", "2026-11-04"]], agent="finance_lc")
    monkeypatch.setattr(tools, "answer_from_data", lambda q, user="", refresh=False, scope=None: [result])
    chat = RecordingChat("LC TAL/SRS/ZARA/667/026 expires on 2026-11-04 [D1].")
    monkeypatch.setattr(orch, "get_chat", lambda: chat)
    return chat, logged


def test_answer_prompt_carries_the_agents_charter_and_the_answer_names_the_agent(data_answer):
    chat, logged = data_answer
    evs = list(orch.answer_stream("When does LC TAL/SRS/ZARA/667/026 expire?", scope=Scope.unrestricted()))
    a = evs[-1].answer
    assert isinstance(evs[-1], Final) and not a.not_found
    assert a.agent == "finance_lc" and logged == [a]
    assert "Domain notes (Finance/LC agent):" in chat.systems[0]


def test_sql_prompt_carries_the_charter_only_with_an_agent():
    from ragbot.config import prompt
    raw = prompt("sql_generate")
    assert "{agent}" in raw
    assert charter_text("") == ""   # no agent: the placeholder becomes an empty line
