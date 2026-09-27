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


def test_stream_error_becomes_llmerror(_no_call_log):
    import openai
    import httpx
    exc = openai.RateLimitError("quota", response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
                                body=None)
    m, _ = _compat(exc=exc)
    with pytest.raises(LLMError) as ei:
        list(m.stream([{"role": "user", "content": "q"}]))
    assert ei.value.kind == "quota"
    assert _no_call_log and _no_call_log[0][1].stop_reason == "error:quota"


def test_chat_error_becomes_llmerror(_no_call_log):
    """Regression (v1.1.0): the error-path log row lacked `text`, so a provider failure raised a
    pydantic ValidationError instead of LLMError and the friendly quota message never showed."""
    import openai
    import httpx
    exc = openai.RateLimitError("quota", response=httpx.Response(429, request=httpx.Request("POST", "http://x")),
                                body=None)
    m, _ = _compat(exc=exc)
    with pytest.raises(LLMError) as ei:
        m.chat([{"role": "user", "content": "q"}])
    assert ei.value.kind == "quota"
