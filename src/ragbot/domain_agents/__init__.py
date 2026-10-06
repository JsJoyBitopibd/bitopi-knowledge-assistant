"""Domain agents (Phase K, docs/AGENTS_DESIGN.md): configuration plus tools per business domain, not
free-running models. An agent is picked for a data question without a model call; it adds its charter
to the SQL and answer prompts and is logged with the answer."""
from .registry import Agent, charter_text, load_agents, pick_agent, validate_agents

__all__ = ["Agent", "charter_text", "load_agents", "pick_agent", "validate_agents"]
