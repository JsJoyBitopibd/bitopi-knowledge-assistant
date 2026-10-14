"""Phase N4: the model gateway — task tiers, and one retry on a second provider.

Why it exists: a free-tier key spent for the day and a five-hour provider outage each stopped this
project outright (29 Sep, 9 Oct, 13 Oct). The gateway retries a quota or timeout failure once on a
second provider, and deliberately does not retry an auth failure, which would fail identically and
hide the real cause.
"""
import pytest

import ragbot.llm as llm
from ragbot.llm.base import ChatModel, ChatReply, LLMError


class Fake(ChatModel):
    """A provider that either answers or raises a chosen kind, and counts its calls."""

    def __init__(self, label: str, fail: str | None = None, deltas=()):
        self.name = label
        self.model = label
        self.fail = fail
        self.deltas = tuple(deltas)
        self.calls = 0

    def _chat(self, messages, system, max_tokens, temperature) -> ChatReply:
        self.calls += 1
        if self.fail:
            raise LLMError(self.fail, f"{self.name} says {self.fail}")
        return ChatReply(text=f"{self.name} answered", model=self.name)

    def _stream(self, messages, system, max_tokens, temperature):
        self.calls += 1
        for d in self.deltas:
            yield d
        if self.fail:
            raise LLMError(self.fail, f"{self.name} says {self.fail}")
        yield ChatReply(text=f"{self.name} answered", model=self.name)


def _wrapped(primary: Fake, fallback: Fake | None):
    return llm.FallbackChat(primary, lambda: fallback)


# ---------------------------------------------------------------- when a second provider is used
@pytest.mark.parametrize("kind", ["quota", "timeout"])
def test_a_quota_or_timeout_failure_is_retried_on_the_second_provider(kind):
    first, second = Fake("first", fail=kind), Fake("second")
    reply = _wrapped(first, second).chat([{"role": "user", "content": "hi"}])
    assert reply.text == "second answered"
    assert (first.calls, second.calls) == (1, 1)


@pytest.mark.parametrize("kind", ["auth", "other"])
def test_an_auth_or_unknown_failure_is_not_retried(kind):
    """A wrong key fails the same way on the second provider; retrying would spend a call and hide it."""
    first, second = Fake("first", fail=kind), Fake("second")
    with pytest.raises(LLMError) as e:
        _wrapped(first, second).chat([{"role": "user", "content": "hi"}])
    assert e.value.kind == kind
    assert (first.calls, second.calls) == (1, 0)


def test_without_a_configured_fallback_the_error_is_raised_as_before():
    """The default deployment configures no fallback, and then nothing about today's behaviour changes."""
    first = Fake("first", fail="quota")
    with pytest.raises(LLMError) as e:
        _wrapped(first, None).chat([{"role": "user", "content": "hi"}])
    assert e.value.kind == "quota"


def test_a_broken_fallback_does_not_replace_the_real_error():
    def explode():
        raise RuntimeError("LLM_FALLBACK_PROVIDER is nonsense")

    first = Fake("first", fail="quota")
    with pytest.raises(LLMError) as e:
        llm.FallbackChat(first, explode).chat([{"role": "user", "content": "hi"}])
    assert e.value.kind == "quota"          # the quota error, not the configuration error


def test_the_second_provider_is_built_once_and_only_when_needed():
    built = []

    def make():
        built.append(1)
        return Fake("second")

    first = Fake("first")
    gw = llm.FallbackChat(first, make)
    gw.chat([{"role": "user", "content": "hi"}])
    assert built == []                      # a healthy primary never constructs it
    first.fail = "quota"
    gw.chat([{"role": "user", "content": "hi"}])
    gw.chat([{"role": "user", "content": "hi"}])
    assert built == [1]                     # and it is constructed once, not per failure


# ---------------------------------------------------------------- streaming
def test_a_stream_that_fails_before_its_first_token_falls_back():
    first, second = Fake("first", fail="quota"), Fake("second", deltas=["he", "llo"])
    out = list(_wrapped(first, second).stream([{"role": "user", "content": "hi"}]))
    assert [d for d in out if isinstance(d, str)] == ["he", "llo"]


def test_a_stream_that_fails_after_tokens_reached_the_screen_does_not_fall_back():
    """The deltas are already rendered in the browser. A retry would write the answer a second time
    underneath the first, so a stream that breaks mid-way raises exactly as it did before."""
    first = Fake("first", fail="timeout", deltas=["half an "])
    second = Fake("second", deltas=["a different answer"])
    gw = _wrapped(first, second)
    seen = []
    with pytest.raises(LLMError):
        for item in gw.stream([{"role": "user", "content": "hi"}]):
            seen.append(item)
    assert seen == ["half an "] and second.calls == 0


# ---------------------------------------------------------------- tiers
def test_tasks_map_to_their_tier_and_an_unknown_task_runs_on_main():
    assert llm.tier_for("rewrite") == "small"
    assert llm.tier_for("route") == "small"
    assert llm.tier_for("answer") == "main"
    assert llm.tier_for("sql") == "main"
    assert llm.tier_for("judge") == "main"
    assert llm.tier_for("recommend") == "executive"
    # the safe end: a purpose nobody mapped must not quietly run on the cheap model
    assert llm.tier_for("something_new") == "main"
    assert llm.tier_for("") == "main"


def test_the_tier_map_comes_from_settings_when_it_is_configured(monkeypatch):
    monkeypatch.setattr(llm.config, "settings",
                        lambda: {"llm.tiers": {"small": ["route"], "executive": ["answer"]}})
    assert llm.tier_for("answer") == "executive"      # moved by config alone
    assert llm.tier_for("route") == "small"
    assert llm.tier_for("sql") == "main"              # unmapped -> main


def test_get_model_and_the_two_wrappers_agree(monkeypatch):
    made = {}

    def fake_tier(tier):
        made[tier] = made.get(tier, 0) + 1
        return f"model-for-{tier}"

    monkeypatch.setattr(llm, "_model_for_tier", fake_tier)
    assert llm.get_model("rewrite") == "model-for-small"
    assert llm.get_model("sql") == "model-for-main"
    assert llm.get_model("recommend") == "model-for-executive"
    assert llm.get_chat() == "model-for-main"          # thin wrappers, as the roadmap requires
    assert llm.get_small_chat() == "model-for-small"


def test_the_wrapper_reports_the_primary_model_name():
    """scripts/eval.py records get_chat().model in every run; it must still name the real model."""
    gw = _wrapped(Fake("gemini-3.5-flash-lite"), None)
    assert gw.model == "gemini-3.5-flash-lite"
    assert gw.name == "gemini-3.5-flash-lite"
