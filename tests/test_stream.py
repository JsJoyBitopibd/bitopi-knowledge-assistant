"""B1: streaming in the LLM layer (no network: a fake OpenAI client replays stream events)."""
from types import SimpleNamespace as NS

import pytest

import ragbot.llm.base as base
from ragbot.llm.base import ChatModel, ChatReply, LLMError


@pytest.fixture(autouse=True)
def _no_call_log(monkeypatch):
    rows = []
    monkeypatch.setattr(base, "_log_call", lambda *a: rows.append(a))
    return rows


def _event(text=None, finish=None, usage=None, model="m-1"):
    choices = [] if text is None and finish is None else [NS(delta=NS(content=text), finish_reason=finish)]
    return NS(model=model, choices=choices, usage=usage)


class _FakeCompletions:
    def __init__(self, events=None, exc=None):
        self.events, self.exc, self.kwargs = events or [], exc, None

    def create(self, **kw):
        self.kwargs = kw
        if self.exc:
            raise self.exc
        return iter(self.events)


def _compat(events=None, exc=None):
    from ragbot.llm.openai_compat import OpenAICompatChat
    m = OpenAICompatChat("m-1", base_url="http://localhost:1/v1", api_key="test-key")
    fake = _FakeCompletions(events, exc)
    m.client = NS(chat=NS(completions=fake))
    return m, fake


def test_openai_compat_streams_deltas_then_final_reply(_no_call_log):
    events = [_event("Use "), _event("form PCD-02 "), _event("[P1].", finish="stop"),
              _event(usage=NS(prompt_tokens=120, completion_tokens=7))]
    m, fake = _compat(events)
    items = list(m.stream([{"role": "user", "content": "q"}], system="sys", purpose="answer"))
    assert items[:-1] == ["Use ", "form PCD-02 ", "[P1]."]
    final = items[-1]
    assert isinstance(final, ChatReply)
    assert final.text == "Use form PCD-02 [P1]." and final.stop_reason == "stop"
    assert (final.input_tokens, final.output_tokens) == (120, 7)
    assert fake.kwargs["stream"] is True and fake.kwargs["stream_options"] == {"include_usage": True}
    assert len(_no_call_log) == 1          # one calls.csv row per stream, written at the end


def test_default_stream_falls_back_to_chat(_no_call_log):
    class OneShot(ChatModel):
        name = "oneshot"

        def _chat(self, messages, system, max_tokens, temperature):
            return ChatReply(text="whole reply", model="x", stop_reason="stop")

    items = list(OneShot().stream([{"role": "user", "content": "q"}]))
    assert items[0] == "whole reply" and isinstance(items[1], ChatReply) and items[1].text == "whole reply"
    assert len(_no_call_log) == 1


PER_MINUTE = "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
PER_DAY = "GenerateRequestsPerDayPerProjectPerModel-FreeTier"


def _spent(monkeypatch, calls, budget=500):
    """Today's call count as ragbot/quota.py would read it from logs/calls.csv."""
    import ragbot.quota as quota
    monkeypatch.setattr(quota, "calls_today", lambda *a, **k: calls)
    monkeypatch.setattr(quota, "daily_budget", lambda: budget)


def _gemini_body(*ids):
    """The shape of Gemini's 429 body (OpenAI-compatible endpoint): the violated limits as quota ids."""
    return [{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                       "message": "You exceeded your current quota, please check your plan and billing details.",
                       "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                    "violations": [{"quotaMetric": "generativelanguage.googleapis.com/"
                                                                   "generate_content_free_tier_requests",
                                                    "quotaId": i, "quotaValue": "10"} for i in ids]}]}}]


def _429(message="quota", body=None):
    import httpx
    import openai
    return openai.RateLimitError(message, response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
                                 body=body)


def test_stream_error_becomes_llmerror(_no_call_log, monkeypatch):
    _spent(monkeypatch, 500)               # the day's budget is spent and the 429 names no limit: the quota
    m, _ = _compat(exc=_429())
    with pytest.raises(LLMError) as ei:
        list(m.stream([{"role": "user", "content": "q"}]))
    assert ei.value.kind == "quota"
    assert _no_call_log and _no_call_log[0][1].stop_reason == "error:quota"


def test_chat_error_becomes_llmerror(_no_call_log, monkeypatch):
    """Regression (v1.1.0): the error-path log row lacked `text`, so a provider failure raised a
    pydantic ValidationError instead of LLMError and the friendly quota message never showed."""
    _spent(monkeypatch, 500)
    m, _ = _compat(exc=_429())
    with pytest.raises(LLMError) as ei:
        m.chat([{"role": "user", "content": "q"}])
    assert ei.value.kind == "quota"


# ---------------------------------------------------------------- a per-minute 429 is not the daily quota
def test_a_per_minute_limit_is_a_rate_error_not_the_daily_quota(_no_call_log, monkeypatch):
    """Every 429 used to read as the quota, so a per-minute limit told staff to come back after midnight."""
    _spent(monkeypatch, 500)               # the ids decide, not the count
    m, _ = _compat(exc=_429("Error code: 429", body=_gemini_body(PER_MINUTE)))
    with pytest.raises(LLMError) as ei:
        m.chat([{"role": "user", "content": "q"}])
    assert ei.value.kind == "rate"


def test_a_per_day_limit_is_the_quota_even_under_the_budget(monkeypatch):
    _spent(monkeypatch, 3)
    m, _ = _compat(exc=_429("Error code: 429", body=_gemini_body(PER_DAY)))
    with pytest.raises(LLMError) as ei:
        m.chat([{"role": "user", "content": "q"}])
    assert ei.value.kind == "quota"


def test_a_per_day_limit_listed_beside_a_per_minute_one_is_the_quota(monkeypatch):
    _spent(monkeypatch, 3)
    m, _ = _compat(exc=_429("Error code: 429", body=_gemini_body(PER_MINUTE, PER_DAY)))
    with pytest.raises(LLMError) as ei:
        m.chat([{"role": "user", "content": "q"}])
    assert ei.value.kind == "quota"


def test_a_429_naming_no_limit_under_the_budget_is_a_rate_error(monkeypatch):
    _spent(monkeypatch, 120)
    m, _ = _compat(exc=_429())
    with pytest.raises(LLMError) as ei:
        m.chat([{"role": "user", "content": "q"}])
    assert ei.value.kind == "rate"


def test_a_call_count_that_fails_reads_as_no_calls(monkeypatch):
    import ragbot.quota as quota

    def broken(*a, **k):
        raise OSError("logs/calls.csv is locked")
    monkeypatch.setattr(quota, "calls_today", broken)
    m, _ = _compat(exc=_429())
    with pytest.raises(LLMError) as ei:
        m.chat([{"role": "user", "content": "q"}])
    assert ei.value.kind == "rate"


def test_the_limit_hit_survives_the_300_character_cut(monkeypatch):
    """Gemini's 429 is longer than the cut; the log must still say which limit was hit."""
    _spent(monkeypatch, 3)
    body = _gemini_body(PER_MINUTE)
    m, _ = _compat(exc=_429(f"Error code: 429 - {'x' * 400} {body}", body=body))
    with pytest.raises(LLMError) as ei:
        m.chat([{"role": "user", "content": "q"}])
    msg = str(ei.value)
    assert msg.startswith("Error code: 429 - xxx") and msg.endswith(f" [quota: {PER_MINUTE}]")
    assert len(msg) == 300 + len(f" [quota: {PER_MINUTE}]")       # cut, then the id once


def test_a_streamed_rate_error_is_logged_as_error_rate(_no_call_log, monkeypatch):
    _spent(monkeypatch, 3)
    m, _ = _compat(exc=_429("Error code: 429", body=_gemini_body(PER_MINUTE)))
    with pytest.raises(LLMError) as ei:
        list(m.stream([{"role": "user", "content": "q"}]))
    assert ei.value.kind == "rate"
    assert _no_call_log and _no_call_log[0][1].stop_reason == "error:rate"


@pytest.mark.parametrize("text,calls,budget,kind", [
    (f"quotaId: {PER_MINUTE}", 500, 500, "rate"),
    (f"quotaId: {PER_DAY}", 0, 500, "quota"),
    (f"{PER_MINUTE}, {PER_DAY}", 0, 500, "quota"),
    ("...GenerateContentInputTokensPerModelPerMinute-FreeTier...", 500, 500, "rate"),
    ("Error code: 429", 499, 500, "rate"),
    ("Error code: 429", 500, 500, "quota"),
    ("", 0, 500, "rate"),
    # OpenAI / Azure: the account's allowance is spent — never "try again in a minute", whatever the count
    ("Error code: 429 - {'error': {'code': 'insufficient_quota'}}", 0, 500, "quota"),
])
def test_rate_or_quota(text, calls, budget, kind):
    assert base.rate_or_quota(text, calls, budget) == kind


def test_staff_are_told_to_wait_a_minute_not_until_midnight():
    from ragbot.data.errors import friendly_llm
    assert "minute" in friendly_llm("rate") and "midnight" not in friendly_llm("rate")
    assert "midnight" in friendly_llm("quota")


def test_quota_ids_are_found_once_each_in_order():
    text = f"'quotaId': '{PER_MINUTE}' ... '{PER_DAY}' ... '{PER_MINUTE}'"
    assert base.quota_ids(text) == [PER_MINUTE, PER_DAY]
    assert base.quota_ids("You exceeded your current quota") == []
