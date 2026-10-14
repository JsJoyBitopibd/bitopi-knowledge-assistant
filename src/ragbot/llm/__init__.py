"""Factory: pick the provider from .env. Callers never import a provider module directly.

Three tiers (Phase N4): small (rewrite, routing), main (answers, SQL) and executive (cross-domain
ranking, recommendations). `get_model(task)` returns the model for a task; `get_chat()` and
`get_small_chat()` are thin wrappers over it, so every existing caller keeps working.

Each model is wrapped in a fallback: when a call fails with a quota or timeout error and a fallback
provider is configured in .env, it is retried once on that provider. A free-tier key spent for the
day, or a provider outage, stopped this project for hours more than once. There is no fallback on an
auth error — a wrong key fails the same way on the second provider, and retrying would only hide it.

Provider-specific request knobs come from .env as JSON (LLM_EXTRA_BODY / LLM_SMALL_EXTRA_BODY) so that
no vendor detail lives in code (non-negotiable #5)."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Iterator, Optional, Union

from .. import config  # noqa: F401  loads .env: every LLM_* value below is read from os.environ
from .base import ChatModel, ChatReply, LLMError

__all__ = ["ChatModel", "ChatReply", "get_chat", "get_small_chat", "get_model", "tier_for"]

# Which tier runs a task. Overridable in settings.yaml (`llm.tiers`), so adding an executive task is
# config, not code. A task nobody mapped runs on `main`: the safe end, never the cheap one.
DEFAULT_TIERS: dict[str, tuple[str, ...]] = {
    "small": ("rewrite", "route", "extract"),
    "main": ("answer", "sql", "judge", "repair"),
    "executive": ("recommend", "prioritise"),
}
# The errors worth a second provider. `auth` is deliberately absent (a bad key fails identically on the
# fallback) and so is `other` (an unknown failure is more likely a bug in the request than the provider).
FALLBACK_KINDS = frozenset({"quota", "timeout"})


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


def _provider(name: str, model: str, **opts: Any) -> ChatModel:
    if name == "openai_compat":
        from .openai_compat import OpenAICompatChat
        return OpenAICompatChat(model, **opts)
    if name == "anthropic":
        from .anthropic import AnthropicChat
        return AnthropicChat(model, **opts)
    raise RuntimeError(f"Unknown LLM provider {name!r}; use openai_compat or anthropic")


def _build(model: str, *, small: bool = False) -> ChatModel:
    provider = os.getenv("LLM_PROVIDER", "openai_compat").lower()
    if small:
        opts: dict[str, Any] = dict(
            extra_body=_json_env("LLM_SMALL_EXTRA_BODY", "LLM_EXTRA_BODY"),
            reasoning_effort=os.getenv("LLM_SMALL_REASONING_EFFORT") or os.getenv("LLM_REASONING_EFFORT") or None)
    else:
        opts = dict(extra_body=_json_env("LLM_EXTRA_BODY"), reasoning_effort=os.getenv("LLM_REASONING_EFFORT") or None)
    opts["tokens_param"] = os.getenv("LLM_TOKENS_PARAM", "max_tokens")
    return _provider(provider, model, **opts)


def _build_fallback(model: str) -> Optional[ChatModel]:
    """The second provider, or None when .env does not configure one (then nothing changes at all).

    It carries its own key and base URL on purpose: the point is to survive the first provider's quota
    and outages, which a second key on the same provider would not."""
    provider = (os.getenv("LLM_FALLBACK_PROVIDER") or "").strip().lower()
    name = (os.getenv("LLM_FALLBACK_MODEL") or "").strip() or model
    if not provider or not name:
        return None
    opts: dict[str, Any] = {"api_key": os.getenv("LLM_FALLBACK_API_KEY") or None}
    if provider == "openai_compat":
        opts["base_url"] = os.getenv("LLM_FALLBACK_BASE_URL") or None
        opts["extra_body"] = _json_env("LLM_FALLBACK_EXTRA_BODY")
        opts["tokens_param"] = os.getenv("LLM_FALLBACK_TOKENS_PARAM", "max_tokens")
    return _provider(provider, name, **opts)


class FallbackChat(ChatModel):
    """A model plus one retry on a second provider for quota and timeout errors.

    Both attempts go through the wrapped models' own `chat()`/`stream()`, so each writes its own row to
    logs/calls.csv: a fallback is visible in the audit as two calls, not one (non-negotiable #4)."""

    def __init__(self, primary: ChatModel, make_fallback):
        self.primary = primary
        self._make_fallback = make_fallback
        self._fallback: Optional[ChatModel] = None
        self._tried = False
        self.name = primary.name

    @property
    def model(self) -> str:
        return getattr(self.primary, "model", "")

    def _chat(self, messages: list[dict], system: str, max_tokens: int, temperature: float) -> ChatReply:
        # Never reached by chat()/stream() below, which delegate to the wrapped models; kept because
        # ChatModel declares it abstract, and honest for anyone who calls it directly.
        return self.primary._chat(messages, system, max_tokens, temperature)

    def _for(self, e: LLMError) -> Optional[ChatModel]:
        """The fallback model when this error deserves one, else None."""
        if getattr(e, "kind", "other") not in FALLBACK_KINDS:
            return None
        if not self._tried:
            self._tried = True
            try:
                self._fallback = self._make_fallback()
            except Exception:      # a misconfigured fallback must not replace the real error
                self._fallback = None
        return self._fallback

    def chat(self, messages: list[dict], system: str = "", max_tokens: int = 700, temperature: float = 0.0,
             **kw: Any) -> ChatReply:
        try:
            return self.primary.chat(messages, system, max_tokens, temperature, **kw)
        except LLMError as e:
            other = self._for(e)
            if other is None:
                raise
        return other.chat(messages, system, max_tokens, temperature, **kw)

    def stream(self, messages: list[dict], system: str = "", max_tokens: int = 700, temperature: float = 0.0,
               **kw: Any) -> Iterator[Union[str, ChatReply]]:
        other, started = None, False
        try:
            for item in self.primary.stream(messages, system, max_tokens, temperature, **kw):
                started = True
                yield item
        except LLMError as e:
            # Once deltas have reached the screen a retry would write the answer twice, so a stream that
            # fails mid-way raises as it did before. Only a stream that failed before its first token
            # is worth a second provider.
            if started:
                raise
            other = self._for(e)
            if other is None:
                raise
        if other is not None:
            yield from other.stream(messages, system, max_tokens, temperature, **kw)


def _tiers() -> dict[str, tuple[str, ...]]:
    try:
        configured = config.settings().get("llm.tiers", None)
    except Exception:
        configured = None
    if not isinstance(configured, dict) or not configured:
        return DEFAULT_TIERS
    return {str(k).strip().lower(): tuple(str(t).strip().lower() for t in (v or []))
            for k, v in configured.items()}


def tier_for(task: str) -> str:
    """small | main | executive for a task name (the `purpose` of the call). Unmapped tasks run on main."""
    want = (task or "").strip().lower()
    for tier, tasks in _tiers().items():
        if want in tasks:
            return tier
    return "main"


@lru_cache(maxsize=4)
def _model_for_tier(tier: str) -> ChatModel:
    if tier == "small":
        name = os.getenv("LLM_SMALL_MODEL") or os.getenv("LLM_MODEL", "")
        primary = _build(name, small=True)
    elif tier == "executive":
        # No executive model configured yet: cross-domain ranking runs on the main model rather than
        # silently on the cheap one.
        name = os.getenv("LLM_EXECUTIVE_MODEL") or os.getenv("LLM_MODEL", "")
        primary = _build(name)
    else:
        name = os.getenv("LLM_MODEL", "")
        primary = _build(name)
    return FallbackChat(primary, lambda: _build_fallback(name))


def get_model(task: str = "answer") -> ChatModel:
    """The model for a task: the tier its name maps to, wrapped in the fallback provider."""
    return _model_for_tier(tier_for(task))


def get_chat() -> ChatModel:
    """Main model: answering and SQL generation."""
    return _model_for_tier("main")


def get_small_chat() -> ChatModel:
    """Cheap model: question rewrite and routing."""
    return _model_for_tier("small")
