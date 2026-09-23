"""OpenAI-compatible chat adapter: Google Gemini (OpenAI-compatible endpoint), Azure OpenAI, Azure AI
Foundry (incl. Claude deployments), OpenAI, or any endpoint that speaks /chat/completions (LLM_BASE_URL).

Provider-specific knobs never leak into callers: `extra_body` (JSON merged into the request body, e.g. the
Gemini thinking budget), `reasoning_effort` and the name of the token-limit parameter are constructor
options that the factory in __init__.py fills from .env."""
from __future__ import annotations

import os
from typing import Any, Optional

from openai import OpenAI

from ..config import env
from .base import ChatModel, ChatReply


class OpenAICompatChat(ChatModel):
    name = "openai_compat"

    def __init__(self, model: str, base_url: str | None = None, api_key: str | None = None,
                 extra_body: Optional[dict[str, Any]] = None, reasoning_effort: str | None = None,
                 tokens_param: str = "max_tokens", timeout: float | None = None):
        self.model = model
        self.extra_body = extra_body or None
        self.reasoning_effort = reasoning_effort or None
        self.tokens_param = tokens_param or "max_tokens"
        # max_retries generous: the free tier of some providers (e.g. Gemini) allows as few as
        # 5 requests/minute, and the SDK's own backoff honors a 429's Retry-After when present.
        # Retries/timeout kept modest: on the free tier a 429 carries a long Retry-After, so the old
        # max_retries=8 x 90 s meant a quota failure hung for many minutes before surfacing. Two
        # retries and a 30 s timeout fail fast enough for the UI to show a friendly message.
        self.client = OpenAI(base_url=base_url or os.getenv("LLM_BASE_URL") or None,
                             api_key=api_key or env("LLM_API_KEY"),
                             max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
                             timeout=timeout or float(os.getenv("LLM_TIMEOUT", "30")))

    def _chat(self, messages, system, max_tokens, temperature) -> ChatReply:
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        kwargs: dict[str, Any] = {self.tokens_param: max_tokens, "temperature": temperature}
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        r = self.client.chat.completions.create(model=self.model, messages=msgs, **kwargs)
        c = r.choices[0]
        usage = getattr(r, "usage", None)
        text = (c.message.content or "") if c.message else ""
        return ChatReply(text=text, input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                         output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                         model=r.model or self.model, stop_reason=c.finish_reason or "", raw=None)

    def _classify(self, exc: Exception) -> str:
        import openai
        if isinstance(exc, openai.RateLimitError):
            return "quota"
        if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
            return "timeout"
        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return "auth"
        return "other"
