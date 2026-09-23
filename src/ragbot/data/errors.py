"""Map raw failure messages to short, user-facing sentences.

The raw error is still logged (logs/sql.csv, logs/errors.log) and kept in Answer.warnings; only what the
end user sees is softened here. Keep these deterministic and specific — no stack traces, no vendor names.
"""
from __future__ import annotations

# LLMError.kind -> message
_LLM = {
    "quota": "The daily AI quota has been reached. Please try again after midnight Pacific time, "
             "or ask IT to enable billing on the AI service.",
    "timeout": "The AI service did not respond in time. Please try again in a moment.",
    "auth": "The AI service rejected our credentials. Please ask IT to check the API key.",
}


def friendly_llm(kind: str) -> str:
    return _LLM.get(kind, "Something went wrong while generating the answer. Please try again.")


def friendly_db(message: str) -> str:
    """A pyodbc / connector error string -> one sentence."""
    m = (message or "").lower()
    if "hyt00" in m or "timeout" in m or "timed out" in m:
        return "This query is too heavy to finish in time. Try narrowing it to one factory or one month."
    if "login" in m or "password" in m or "28000" in m:
        return "The database rejected our login. Please ask IT to check the read-only account."
    if "server" in m and ("not" in m or "unreach" in m or "network" in m):
        return "The database could not be reached. Please try again shortly."
    return "That data could not be retrieved right now. Please try again."
