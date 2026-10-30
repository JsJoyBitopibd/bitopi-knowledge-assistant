"""Phase T9: scripted conversations in the golden set (scripts/eval.py converse / conversation_checks)."""
import importlib.util
import json
import sys
from pathlib import Path

from ragbot.models import Answer, QueryResult, Usage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location("eval_script", ROOT / "scripts" / "eval.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def _ans(**kw):
    base = dict(text="From the earlier answer's 143 rows, … [D1]:", route="refine", usage=Usage(calls=0))
    base.update(kw)
    return Answer(**base)


def test_conversation_checks_pass_and_fail():
    case = {"q": "sort by quantity, top 5", "expected_route": ["refine", "requery"], "expect_no_model": True,
            "expected_contains": ["earlier answer"]}
    assert ev.conversation_checks(case, _ans()) == []
    bad = ev.conversation_checks(case, _ans(route="data", usage=Usage(calls=2), text="x"))
    assert bad == ["route data not in ['refine', 'requery']", "2 model call(s)", "missing 'earlier answer'"]
    col = {"q": "q", "expected_column": "Merchandiser"}
    r = QueryResult(database="X", engine="sqlserver", views=[], sql="", columns=["Merchandiser"], rows=[["a"]])
    assert ev.conversation_checks(col, _ans(results=[r])) == [] and ev.conversation_checks(col, _ans()) != []
    assert ev.conversation_checks({"q": "b+ blood group", "expected_not_rewritten": True},
                                  _ans(rewritten_question="Which lines … for b+ blood group")) != []


def test_converse_runs_the_earlier_turns_like_the_app(monkeypatch):
    seen = []
    r = QueryResult(database="X", engine="sqlserver", views=["rag.v"], sql="SELECT TOP (200) a FROM rag.v",
                    columns=["a"], rows=[[1], [2]])

    def fake_answer(q, history=None, where=None, user="", refresh=False, *, scope, state=None):
        seen.append((q, [h["text"] for h in history or []], state.last_question if state else None))
        return Answer(text=f"answer to {q}", route="data", results=[r], request_id=f"r{len(seen)}")
    monkeypatch.setattr(ev, "answer", fake_answer)
    hist, state, ok = ev.converse({"id": 900, "turns": ["first", {"q": "second"}]})
    assert ok and seen[0] == ("first", [], "") and seen[1] == ("second", ["first", "answer to first"], "first")
    assert [h["text"] for h in hist] == ["first", "answer to first", "second", "answer to second"]
    assert state.last_request_id == "r2" and state.last_result().rows == [[1], [2]]


def test_the_golden_set_holds_the_conversation_cases_and_case_62():
    cases = [json.loads(line) for line in (ROOT / "tests" / "golden.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids)) and ids[:100] == list(range(1, 101))        # never delete or renumber a case
    conv = [c for c in cases if c.get("kind") == "conversation"]
    assert len(conv) >= 15 and all("turns" in c and c.get("q") for c in conv)
    assert next(c for c in cases if c["id"] == 62).get("history")              # the original form still works
