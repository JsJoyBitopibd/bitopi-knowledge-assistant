"""Phase N2, the part left on 2026-10-16: the program ranks a file's ready options and the executive-tier model
explains that order — every fact cited, every number from a cited source, one stricter retry, then nothing.

Run on test playbooks and rates only (tests/test_decisions.py fixtures): the real playbooks and rates are the
departments' to write (N1b), and nothing here invents one.
"""
import sqlite3
from dataclasses import dataclass

from ragbot.auth.models import Scope
from ragbot.config import ROOT
from ragbot.domain_agents import recommend
from ragbot.llm import DEFAULT_TIERS, tier_for
from ragbot.models import Chunk
from test_decisions import TODAY, _assess, _costs, _playbook, _predict

ADMIN = Scope(factories=frozenset(["*"]), is_admin=True)
PDF = "quality_playbook.pdf"


@dataclass
class _Reply:
    text: str
    stop_reason: str = "stop"


class _Model:
    """Answers from a list, one reply per call, and records what it was sent. A (text, stop_reason) pair sets
    the stop reason."""

    def __init__(self, *texts):
        self.texts, self.calls = list(texts), []

    def chat(self, messages, system="", max_tokens=700, temperature=0.0, **kw):
        self.calls.append(dict(content=messages[-1]["content"], system=system, max_tokens=max_tokens, **kw))
        nxt = self.texts.pop(0) if self.texts else ""
        return _Reply(*nxt) if isinstance(nxt, tuple) else _Reply(nxt)


class _Registry:
    def __init__(self, pages=(1, 2)):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE chunk (id TEXT, source TEXT, page INTEGER)")
        self.db.executemany("INSERT INTO chunk VALUES (?, ?, ?)",
                            [(f"quality_playbook#p{p}#c1", PDF, p) for p in pages])


class _Store:
    def __init__(self, factory="ALL"):
        self.factory = factory

    def get(self, ids):
        text = {1: "Inline inspectors check every hour at the lines running the file.",
                2: "Defective pieces are reworked at the line before they move to finishing."}
        return [Chunk(id=i, text="Quality playbook › Actions\n" + text[int(i.split("#p")[1].split("#")[0])],
                      source=PDF, title="quality_playbook", page=int(i.split("#p")[1].split("#")[0]),
                      section="Actions", category="Playbook", factory=self.factory) for i in ids]


def _setup(tmp_path, cost=900):
    db = tmp_path / "signals.db"
    pid = _predict(db, _assess(defects=300))[0]                 # DHU 7.5: red
    pbs = [_playbook(tmp_path)]
    opts = recommend.options(ADMIN, playbooks=pbs, costs=_costs(cost), today=TODAY, path=db)
    return db, pid, pbs, opts


GOOD = ("File FR-26-1985 is red with a DHU of 7.5 against a target of 3.85 [D1].\n"
        "1. Rework the defective pieces: they are reworked at the line before finishing [P2]; it costs "
        "135000 at the rework rate of 900 [D2].\n"
        "2. Add inline inspectors: they check every hour [P1]; a person chooses how many, from 1 to 4 [D2].")


def _explain(tmp_path, model, **kw):
    db, pid, pbs, opts = _setup(tmp_path)
    return pid, recommend.explain(pid, ADMIN, opts=opts, playbooks=pbs, model=model, registry=_Registry(),
                                  store=_Store(), path=db, **kw)


# ---------------------------------------------------------------- the order is the program's
def test_ready_options_with_a_cost_come_first_cheapest_first(tmp_path):
    _db, _pid, _pbs, opts = _setup(tmp_path)
    ranked = recommend.rank(opts)
    assert [o.action for o in ranked] == ["rework_defective_pieces", "add_inline_inspectors"]
    assert ranked[0].cost["value"] == 150 * 900 and ranked[1].cost is None


def test_an_option_that_cannot_be_recommended_is_never_ranked(tmp_path):
    db = tmp_path / "signals.db"
    _predict(db, _assess(defects=300))
    opts = recommend.options(ADMIN, playbooks=[_playbook(tmp_path, "draft")], costs=_costs(), today=TODAY, path=db)
    assert opts and recommend.rank(opts) == []


def test_cheaper_wins_across_playbooks_and_files_stay_apart(tmp_path):
    a = recommend.Option(1, "FR-A", "TAL", "red", "pb2", "t", "approved", "x", "X", 1, cost={"value": 500})
    b = recommend.Option(1, "FR-A", "TAL", "red", "pb1", "t", "approved", "y", "Y", 1, cost={"value": 900})
    c = recommend.Option(2, "FR-B", "TAL", "amber", "pb1", "t", "approved", "y", "Y", 1, cost={"value": 10})
    assert [(o.subject, o.action) for o in recommend.rank([c, b, a])] == [("FR-A", "x"), ("FR-A", "y"), ("FR-B", "y")]


# ---------------------------------------------------------------- the explanation
def test_a_verified_explanation_is_kept_with_its_sources(tmp_path):
    model = _Model(GOOD)
    _pid, ex = _explain(tmp_path, model)
    assert ex.ok and ex.text == GOOD and not ex.problems
    assert [r.marker for r in ex.references] == ["P1", "P2", "D1", "D2"]
    assert len(model.calls) == 1 and model.calls[0]["purpose"] == "recommend"
    sent = model.calls[0]["content"]
    assert "135000" in sent and "in exactly that order" in sent and "Rework the defective pieces" in sent


def test_the_explanation_runs_on_the_executive_tier():
    assert "recommend" in DEFAULT_TIERS["executive"] and tier_for("recommend") == "executive"


def test_a_number_the_sources_do_not_hold_is_retried_then_refused(tmp_path):
    bad = GOOD.replace("135000", "120000")
    model = _Model(bad, bad)
    _pid, ex = _explain(tmp_path, model)
    assert not ex.ok and ex.text == "" and len(model.calls) == 2
    assert "120000" in model.calls[1]["content"] and "refused" in model.calls[1]["content"]
    assert any("120000" in p for p in ex.problems)
    assert [o.action for o in ex.ranked] == ["rework_defective_pieces", "add_inline_inspectors"]


def test_the_retry_can_succeed(tmp_path):
    model = _Model(GOOD.replace("[D1]", "[D9]"), GOOD)
    _pid, ex = _explain(tmp_path, model)
    assert ex.ok and len(model.calls) == 2


def test_a_reply_without_markers_or_a_not_found_is_refused(tmp_path):
    _pid, ex = _explain(tmp_path, _Model("Rework the pieces.", "System doesn't have the data."))
    assert not ex.ok


def test_a_reply_cut_off_by_the_length_limit_is_refused_even_when_it_checks_out(tmp_path):
    """Seen live on 3 Nov: a cut-off fragment of the model's own reasoning carried valid markers."""
    model = _Model((GOOD, "length"), (GOOD, "length"))
    _pid, ex = _explain(tmp_path, model)
    assert not ex.ok and any("cut off" in p for p in ex.problems)


def test_the_token_budget_leaves_room_for_the_models_thinking(tmp_path):
    model = _Model(GOOD)
    _explain(tmp_path, model)
    assert model.calls[0]["max_tokens"] >= 2048


def test_one_numbered_item_per_option_in_order(tmp_path):
    one_item = GOOD.split("\n2.")[0]
    swapped = GOOD.replace("1. Rework", "X. Rework").replace("2. Add", "1. Add").replace("X. Rework", "2. Rework")
    for text in (one_item, swapped, GOOD + "\n3. Something else [D2]."):
        _pid, ex = _explain(tmp_path / str(abs(hash(text))), _Model(text, text))
        assert not ex.ok and any("numbered item" in p for p in ex.problems), text


def test_no_model_call_when_nothing_is_ready(tmp_path):
    db = tmp_path / "signals.db"
    pid = _predict(db, _assess(defects=300))[0]
    pbs = [_playbook(tmp_path, "draft")]
    model = _Model(GOOD)
    ex = recommend.explain(pid, ADMIN, playbooks=pbs, opts=recommend.options(ADMIN, playbooks=pbs, costs=_costs(),
                                                                             today=TODAY, path=db),
                           model=model, registry=_Registry(), store=_Store(), path=db)
    assert not ex.ok and model.calls == [] and "no option is ready" in ex.problems[0]


def test_no_model_call_when_no_playbook_page_is_indexed(tmp_path):
    db, pid, pbs, opts = _setup(tmp_path)
    model = _Model(GOOD)
    ex = recommend.explain(pid, ADMIN, opts=opts, playbooks=pbs, model=model, registry=_Registry(pages=()),
                           store=_Store(), path=db)
    assert not ex.ok and model.calls == []
    assert any("not in the index" in p for p in ex.problems) and ex.ranked


def test_a_page_the_viewer_may_not_see_is_not_sent(tmp_path):
    db, pid, pbs, opts = _setup(tmp_path)
    tal = Scope(factories=frozenset(["TAL"]), is_admin=True)
    model = _Model(GOOD)
    ex = recommend.explain(pid, tal, opts=opts, playbooks=pbs, model=model, registry=_Registry(),
                           store=_Store(factory="RHL"), path=db)
    assert model.calls == [] and not ex.ok


def test_another_factorys_file_is_not_explained(tmp_path):
    db, pid, pbs, opts = _setup(tmp_path)
    rhl = Scope(factories=frozenset(["RHL"]), is_admin=True)
    model = _Model(GOOD)
    ex = recommend.explain(pid, rhl, opts=opts, playbooks=pbs, model=model, registry=_Registry(), store=_Store(),
                           path=db)
    assert model.calls == [] and "no open risk level you may see" in ex.problems[0]


def test_a_settled_file_is_not_explained(tmp_path):
    db, pid, pbs, opts = _setup(tmp_path)
    con = sqlite3.connect(db)
    with con:
        con.execute("UPDATE prediction SET outcome_at = '2026-10-20T00:05:00' WHERE id = ?", (pid,))
    con.close()
    model = _Model(GOOD)
    ex = recommend.explain(pid, ADMIN, opts=opts, playbooks=pbs, model=model, registry=_Registry(), store=_Store(),
                           path=db)
    assert model.calls == [] and not ex.ok


def test_what_is_sent_stays_within_the_data_minimisation_limits(tmp_path):
    db, pid, pbs, opts = _setup(tmp_path)
    many = [recommend.Option(pid, "FR-26-1985", "TAL", "red", pbs[0].id, "t", "approved", f"a{i}", f"Action {i}",
                             1 + i % 2, cost={"value": i}) for i in range(20)]
    model = _Model(GOOD)
    ex = recommend.explain(pid, ADMIN, opts=many, playbooks=pbs, model=model, registry=_Registry(), store=_Store(),
                           path=db)
    assert len(ex.ranked) == recommend.MAX_EXPLAINED
    assert len(model.calls[0]["sent_chunk_ids"]) <= 8 and model.calls[0]["sent_rows"] <= 50


def test_the_prompts_live_in_the_prompts_folder_and_hold_no_figure():
    import re
    for name in ("recommend_system", "recommend_explain"):
        text = (ROOT / "prompts" / f"{name}.txt").read_text(encoding="utf-8")
        bare = re.sub(r"\[(?:D|P)(?:\d+|#)\]", "", text)       # source markers name sources, not figures
        assert text.strip() and not any(ch.isdigit() for ch in bare)
