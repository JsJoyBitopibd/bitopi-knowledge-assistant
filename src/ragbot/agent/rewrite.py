"""Turn a follow-up into a standalone question (small model, last N turns)."""
from __future__ import annotations

from ..config import prompt, settings
from ..llm import get_small_chat


def standalone_question(latest: str, history: list[dict], user: str = "") -> str:
    """history: [{"role": "user"|"assistant", "text": str}, ...] oldest first."""
    if not history:
        return latest
    n = int(settings()["answer.history_turns"])
    recent = history[-2 * n:]
    convo = "\n".join(f"{t['role']}: {t['text']}" for t in recent)
    reply = get_small_chat().chat([{"role": "user", "content": f"Conversation:\n{convo}\n\nLatest: {latest}"}],
                                  system=prompt("rewrite"), max_tokens=int(settings()["llm.max_tokens_rewrite"]),
                                  temperature=0, purpose="rewrite", user=user)
    text = reply.text.strip().strip('"')
    return text or latest
