"""J4: a count/list question in a database's own vocabulary is routed to data without a model call."""
import pytest

import ragbot.agent.router as router
import ragbot.data.catalog as catalog
from ragbot.data.catalog import Catalog


class NoModel:
    def chat(self, *a, **k):
        raise AssertionError("the routing model must not be called")


class SaysData:
    def __init__(self):
        self.calls = 0

    def chat(self, *a, **k):
        self.calls += 1
        from ragbot.llm.base import ChatReply
        return ChatReply(text="data", model="fake", stop_reason="stop")


@pytest.fixture
def cats(monkeypatch):
    cats = {"Inventory": Catalog("Inventory", "tsql", "X", [], [], keywords=["grn", "stock", "file", "fabric roll"]),
            "FM": Catalog("FM", "tsql", "X", [], [], keywords=["voucher", "vouchers"])}
    monkeypatch.setattr(catalog, "load_catalogs", lambda folder=None: cats)
    return cats


def test_count_or_list_in_database_vocabulary_is_data(cats, monkeypatch):
    monkeypatch.setattr(router, "get_small_chat", lambda: NoModel())
    assert router.route("How many GRN were received this week?") == "data"
    assert router.route("List the vouchers posted yesterday") == "data"
    assert router.route("Which fabric roll is in rack 12?") == "data"


def test_document_nouns_still_win(cats, monkeypatch):
    monkeypatch.setattr(router, "get_small_chat", lambda: NoModel())
    assert router.route("Which SOP covers GRN approval?") == "documents"     # 'sop' is a document noun


def test_find_get_fetch_are_list_cues_even_after_please(cats, monkeypatch):
    """P1: 'please find the closing balance …' used to pay a routing call (1.09 s on 8 Oct 2026)."""
    monkeypatch.setattr(router, "get_small_chat", lambda: NoModel())
    assert router.route("please find the GRN of item 1178108") == "data"
    assert router.route("Can you get the stock of fabric roll 12?") == "data"
    assert router.route("fetch the vouchers of September") == "data"


def test_generic_words_and_statements_still_ask_the_model(cats, monkeypatch):
    small = SaysData()
    monkeypatch.setattr(router, "get_small_chat", lambda: small)
    assert router.route("List all file") == "data" and small.calls == 1        # 'file' is too generic to decide
    assert router.route("Tell me about vouchers") == "data" and small.calls == 2   # no count/list cue




def test_a_named_line_or_a_running_question_is_data_without_the_model(cats, monkeypatch):
    """Phase Q: no PDF records which style a sewing line runs today, so these need no routing call."""
    monkeypatch.setattr(router, "get_small_chat", lambda: NoModel())
    for q in ["What style is running on TAL line 2102?", "DHU of line 2322 yesterday",
              "output of unit 03 today", "which style is running on line 2102"]:
        assert router.route(q) == "data", q


def test_a_document_noun_still_wins_over_a_line_number(cats, monkeypatch):
    monkeypatch.setattr(router, "get_small_chat", lambda: NoModel())
    assert router.route("Which SOP covers line 2102 balancing?") == "documents"
    assert router.route("What is the TAL holiday policy?") == "documents"


def test_a_factory_with_database_vocabulary_is_data_but_a_factory_alone_asks_the_model(cats, monkeypatch):
    monkeypatch.setattr(router, "get_small_chat", lambda: NoModel())
    assert router.route("TAL stock of fabric roll") == "data"
    small = SaysData()
    monkeypatch.setattr(router, "get_small_chat", lambda: small)
    assert router.route("Tell me about TAL") == "data" and small.calls == 1
