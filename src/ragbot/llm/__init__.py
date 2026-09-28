"""Factory: pick the provider from .env. Callers never import a provider module directly.

Two instances: the main model (answers, SQL) and the small model (rewrite, routing). Provider-specific
request knobs come from .env as JSON (LLM_EXTRA_BODY / LLM_SMALL_EXTRA_BODY) so that no vendor detail
lives in code (non-negotiable #5)."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Optional

from .. import config  # noqa: F401  loads .env: every LLM_* value below is read from os.environ
from .base import ChatModel, ChatReply

__all__ = ["ChatModel", "ChatReply", "get_chat", "get_small_chat"]


def _json_env(name: str, fallback: str | None = None) -> Optional[dict[str, Any]]:
    # `unset` falls back; explicit empty string means "no extra body" (used to shed the main model's config).
    raw = os.getenv(name)
    if raw is None and fallback:
        raw = os.getenv(fallback)
    if not raw or not raw.strip():
        return None
    try:
        val = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{name} must be valid JSON (see .env.example): {e}") from e
    if not isinstance(val, dict):
        raise RuntimeError(f"{name} must be a JSON object")
    return val


def _build(model: str, *, small: bool = False) -> ChatModel:
    provider = os.getenv("LLM_PROVIDER", "openai_compat").lower()
    if small:
        opts: dict[str, Any] = dict(
            extra_body=_json_env("LLM_SMALL_EXTRA_BODY", "LLM_EXTRA_BODY"),
            reasoning_effort=os.getenv("LLM_SMALL_REASONING_EFFORT") or os.getenv("LLM_REASONING_EFFORT") or None)
    else:
        opts = dict(extra_body=_json_env("LLM_EXTRA_BODY"), reasoning_effort=os.getenv("LLM_REASONING_EFFORT") or None)
    opts["tokens_param"] = os.getenv("LLM_TOKENS_PARAM", "max_tokens")
    if provider == "openai_compat":
        from .openai_compat import OpenAICompatChat
        return OpenAICompatChat(model, **opts)
    if provider == "anthropic":
        from .anthropic import AnthropicChat
        return AnthropicChat(model, **opts)
    raise RuntimeError(f"Unknown LLM_PROVIDER={provider!r}; use openai_compat or anthropic")


@lru_cache(maxsize=1)
def get_chat() -> ChatModel:
    """Main model: answering and SQL generation."""
    return _build(os.getenv("LLM_MODEL", ""))


@lru_cache(maxsize=1)
def get_small_chat() -> ChatModel:
    """Cheap model: question rewrite and routing."""
    return _build(os.getenv("LLM_SMALL_MODEL") or os.getenv("LLM_MODEL", ""), small=True)
