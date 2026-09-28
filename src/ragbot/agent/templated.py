"""Zero-LLM answers for fixed-tool results (Phase C7, docs/ROADMAP.md).

A fixed tool's SQL is hand-written, so its output shape is known: a scalar (COUNT) or a list of rows.
Writing that as prose costs an LLM call and invites the model to get it wrong — it has answered
"System doesn't have the data." to a count of 0 and to a 200-row list (docs/PROGRESS.md). Instead:
- scalar  -> the tool's `answer:` template from config/fixed_tools.yaml (e.g. "{factory} has {value}
             PPM meeting(s) {window}"), or "<Column>: <value>", then [D1];
- rows    -> the tool's `answer_list:` lead (or "N row(s) found") + [D1] + a Markdown table of the
             first `answer.template_rows` rows.
The result still goes through citations.verify() like any model answer; if it fails, the caller
falls back to the model. Only the first-row count and values that are in the rows (or the query's
parameters) are ever written, so there is nothing to hallucinate.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from ..config import settings
from ..models import QueryResult


def fmt(v: Any) -> str:
    """Cell text. Dates without a time print as YYYY-MM-DD (digits the verifier finds in the row)."""
    if v is None:
        return "—"
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d") if (v.hour, v.minute, v.second) == (0, 0, 0) else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        v = v.normalize() if v == v.to_integral() else v
        return format(v, "f")
    return str(v).replace("|", "/").replace("\n", " ").strip()


def _fill(template: str, r: QueryResult, **extra: Any) -> Optional[str]:
    vals = {k: fmt(v) for k, v in r.params.items()}
    vals.update({k: fmt(v) if not isinstance(v, str) else v for k, v in extra.items()})
    try:
        return template.format(**vals)
    except (KeyError, IndexError, ValueError):
        return None   # a template naming a parameter this match did not produce: use the generic text


def follow_ups(r: QueryResult, tool: Optional[dict[str, Any]], asked: str = "", limit: int = 3) -> list[str]:
    """The tool's `follow_ups:` questions (config/fixed_tools.yaml), filled with this answer's parameters —
    a user who asked about TAL next week is offered TAL questions for next week, which stay inside their
    scope. A template naming a parameter this match did not produce is skipped, and so is the question
    just asked. Each one is written to reach a fixed tool, so a click answers without a model call."""
    out: list[str] = []
    for template in (tool or {}).get("follow_ups") or []:
        q = _fill(template, r)
        if q and q.strip().lower() != asked.strip().lower() and q not in out:
            out.append(q)
    return out[:limit]


def try_template(r: QueryResult, tool: Optional[dict[str, Any]]) -> Optional[str]:
    """The answer text for one fixed-tool result, or None when it should be left to the model."""
    if r.error or r.tool == "generated" or not r.rows or not r.columns:
        return None
    tool = tool or {}
    if len(r.rows) == 1 and len(r.columns) == 1:
        value = r.rows[0][0]
        lead = _fill(tool["answer"], r, value=value) if tool.get("answer") else None
        return f"{lead or f'{r.columns[0]}: {fmt(value)}'} [D1]."
    n = len(r.rows)
    shown = int(settings().get("answer.template_rows", 25))
    lead = _fill(tool["answer_list"], r, n=n) if tool.get("answer_list") else None
    head = f"{lead or f'{n} row(s) found'} [D1]:"
    table = ["| " + " | ".join(r.columns) + " |", "|" + "---|" * len(r.columns)]
    table += ["| " + " | ".join(fmt(v) for v in row) + " |" for row in r.rows[:shown]]
    tail = f"\n\nOnly the first rows are shown here; the query returned {n} in total." if n > shown else ""
    return head + "\n\n" + "\n".join(table) + tail
