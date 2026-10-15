"""Map raw failure messages to short, user-facing sentences.

The raw error is still logged (logs/sql.csv, logs/errors.log) and kept in Answer.warnings; only what the
end user sees is softened here. Keep these deterministic and specific — no stack traces, no vendor names.
"""
from __future__ import annotations

# LLMError.kind -> message
_LLM = {
    "quota": "The daily AI quota has been reached. Please try again after midnight Pacific time, "
             "or ask IT to enable billing on the AI service.",
    "rate": "The AI service is busy right now. Please try again in a minute.",
    "timeout": "The AI service did not respond in time. Please try again in a moment.",
    "auth": "The AI service rejected our credentials. Please ask IT to check the API key.",
}


def friendly_llm(kind: str) -> str:
    return _LLM.get(kind, "Something went wrong while generating the answer. Please try again.")


def friendly_db(message: str) -> str:
    """A pyodbc / connector error string -> one sentence."""
    m = (message or "").lower()
    if "hyt00" in m or "timeout" in m or "timed out" in m:
        # Phase Q: the old wording ("narrow it to one factory") sent people the wrong way — a factory-filtered
        # question was the slow one. What a reader can act on is that it can be tried again.
        return "The query took longer than the time allowed."
    if "22018" in m or "8114" in m or "conversion failed" in m or "error converting data type" in m:
        return "The query could not match a number in the question with the codes as they are recorded."
    if "login" in m or "password" in m or "28000" in m:
        return "The database rejected our login. Please ask IT to check the read-only account."
    if "server" in m and ("not" in m or "unreach" in m or "network" in m):
        return "The database could not be reached. Please try again shortly."
    return "That data could not be retrieved right now. Please try again."
