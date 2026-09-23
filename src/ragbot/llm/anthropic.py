"""Anthropic direct API adapter."""
from __future__ import annotations

from anthropic import Anthropic

from ..config import env
from .base import ChatModel, ChatReply


class AnthropicChat(ChatModel):
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None = None, **_: object):  # request knobs are OpenAI-compat only
        self.model = model
        self.client = Anthropic(api_key=api_key or env("LLM_API_KEY"), max_retries=3, timeout=60.0)

    def _chat(self, messages, system, max_tokens, temperature) -> ChatReply:
        r = self.client.messages.create(model=self.model, system=system or "", messages=messages,
                                        max_tokens=max_tokens, temperature=temperature)
        text = "".join(getattr(b, "text", "") for b in r.content)
        return ChatReply(text=text, input_tokens=r.usage.input_tokens, output_tokens=r.usage.output_tokens,
                         model=r.model, stop_reason=r.stop_reason or "", raw=None)
