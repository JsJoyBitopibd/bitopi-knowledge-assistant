"""Phase U3: the home page's suggestions and the "your factory" line (ui/suggest.py, ui/view.py)."""
from datetime import datetime

from ragbot.ui import view
from ragbot.ui.suggest import home_suggestions, localize

from test_ui_view import EVIL

FALLBACK = ["Show me the QC summary today", "Which lines are above the DHU target today?", "What is the next PCD?"]


def test_own_questions_first_then_examples_then_the_defaults():
    got = home_suggestions(own_recent=["Which TAL orders ship next week?", "which tal orders ship next week?",
                                       "third one"],
                           examples=["What are the next PCDs at RHL?", "Which LCs expire next month?", "x", "y"],
                           factory="TAL", fallback=FALLBACK, limit=6)
    # two of the user's own (the second is the first again, case aside, so one), two examples (RHL made TAL), defaults
    assert got == ["Which TAL orders ship next week?", "What are the next PCDs at TAL?", "Which LCs expire next month?",
                   "Show me the QC summary today", "Which lines are above the DHU target today?",
                   "What is the next PCD?"]


def test_without_history_or_examples_the_defaults_fill_in():
    assert home_suggestions(fallback=FALLBACK, limit=6) == FALLBACK


def test_a_factory_is_swapped_only_when_the_question_still_reaches_its_tool():
    assert localize("What are the next PCDs at RHL?", "TAL", lambda q: True) == "What are the next PCDs at TAL?"
    assert localize("What are the next PCDs at RHL?", "TAL", lambda q: False) == "What are the next PCDs at RHL?"
    assert localize("Which LCs expire next month?", "TAL", lambda q: True) == "Which LCs expire next month?"
    assert localize("What are the next PCDs at RHL?", None, lambda q: True) == "What are the next PCDs at RHL?"


def test_unit_line_and_ago():
    assert view.unit_line_html("TAL", "1", "TARASIMA APPARELS LTD.") == \
        '<div class="ka-unit-line">Your factory: TAL · Unit 1 · TARASIMA APPARELS LTD.</div>'
    evil = view.unit_line_html(EVIL, EVIL, EVIL)
    assert "<script>" not in evil and "&lt;script&gt;" in evil
    now = datetime(2026, 10, 30, 15, 0)
    assert view.ago("2026-10-30T14:59:30", now) == "just now"
    assert view.ago("2026-10-30T14:35:00", now) == "25 minutes ago"
    assert view.ago("2026-10-30T12:00:00", now) == "3 hours ago"
    assert view.ago("2026-10-29T18:00:00", now) == "yesterday"
    assert view.ago("2026-10-12T09:00:00", now) == "12 Oct 2026"
    assert view.ago("not a time", now) == ""
