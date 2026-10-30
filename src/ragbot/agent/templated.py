"""Zero-LLM answers for fixed-tool results (Phase C7, docs/ROADMAP.md).

A fixed tool's SQL is hand-written, so its output shape is known: a scalar (COUNT) or a list of rows.
Writing that as prose costs an LLM call and invites the model to get it wrong — it has answered
"System doesn't have the data." to a count of 0 and to a 200-row list (docs/PROGRESS.md). Instead:
- scalar  -> the tool's `answer:` template from config/fixed_tools.yaml (e.g. "{factory} has {value}
             PPM meeting(s) {window}"), or "<Column>: <value>", then [D1];
- rows    -> the tool's `answer_list:` lead (or "Found N matching rows") + [D1] + a Markdown table of the
             first `answer.template_rows` rows.
The result still goes through citations.verify() like any model answer; if it fails, the caller
falls back to the model. Only the first-row count and values that are in the rows (or the query's
parameters) are ever written, so there is nothing to hallucinate.
"""
from __future__ import annotations

import re
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
        if v == v.to_integral():
            return format(v.normalize(), "f")               # 13.0000000000 -> 13, 0E-10 -> 0
        s = format(v, "f")
        if "." in s and -v.as_tuple().exponent > 4:          # a numeric(26,10) ledger sum: 12.1250000000 -> 12.125
            s = s.rstrip("0").rstrip(".")                    # (money written as 12.50 keeps its two decimals)
        return s
    return str(v).replace("|", "/").replace("\n", " ").strip()


def _fill(template: str, r: QueryResult, **extra: Any) -> Optional[str]:
    vals = {k: fmt(v) for k, v in r.params.items()}
    vals.update({k: fmt(v) if not isinstance(v, str) else v for k, v in extra.items()})
    try:
        out = template.format(**vals)
    except (KeyError, IndexError, ValueError):
        return None   # a template naming a parameter this match did not produce: use the generic text
    # an optional part ("{company_label}" is "" when no company was named) must not leave a double space
    return re.sub(r" {2,}", " ", out).strip()


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


def is_blank_result(columns: list[str], rows: list[list[Any]]) -> bool:
    """True when a result answers nothing: no rows, or one row x one column holding NULL, 0 or an empty
    string (SUM over no rows, COUNT of nothing). Used by the SQL cache (never replay such a statement for a
    day), the orchestrator (a `[[None]]` row is truthy, so `any(x.rows)` would call it an answer) and the
    template below. A fixed tool's 0 is still an answer (C7): callers check `tool == "generated"`."""
    if not rows or not columns:
        return True
    if len(rows) == 1 and len(columns) == 1:
        v = rows[0][0]
        return v is None or v == 0 or str(v).strip() == ""
    return False


Filters = list[tuple[str, list[str]]]


def scalar_filters(r: QueryResult) -> Optional[Filters]:
    """What a model-written one-value answer was filtered on, when its WHERE can be stated exactly
    (data/empty_retry.where_filters); None for a fixed tool, any other shape, a blank value or a WHERE that
    is not plain `col = literal` / `col IN (…)` conditions."""
    if r.tool != "generated" or r.error or len(r.rows) != 1 or len(r.columns) != 1 \
            or is_blank_result(r.columns, r.rows):
        return None
    from ..data.empty_retry import where_filters   # lazy: data/tools.py imports this module
    return where_filters(r.sql or "", "mysql" if r.engine == "mysql" else "tsql")


def filter_params(filters: Filters) -> dict[str, str]:
    """The filters as query parameters, {column: "v1, v2"}: what the [D#] card's Filter line shows and what
    citations.verify() licenses, so a value with digits in it ("CompanyID is 02") may be written."""
    return {col: ", ".join(vals) for col, vals in filters}


def _either(values: list[str]) -> str:
    vals = [fmt(v) for v in values]
    return vals[0] if len(vals) == 1 else ", ".join(vals[:-1]) + " or " + vals[-1]


def describe_filters(filters: Filters) -> str:
    """'BloodGroup is O-, O-ve or O Negative and CompanyID is 02': every value, never "and N more"."""
    return " and ".join(f"{col} is {_either(vals)}" for col, vals in filters)


def _int(v: Any) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


def modifier_note(r: QueryResult) -> str:
    """', sorted by OrderQty (largest first); the first 10 are shown' — what a sort or a limit the question asked for
    did (Phase T2, data/tools.apply_limit), from the result's `*_label` parameters, so every figure verifies."""
    parts = []
    if r.params.get("sort_label"):
        parts.append(f", {r.params['sort_label']}")
    if r.params.get("shown_label"):
        parts.append(f"; the {r.params['shown_label']} are shown")
    return "".join(parts)


def _markdown_table(r: QueryResult, shown: int) -> str:
    table = ["| " + " | ".join(r.columns) + " |", "|" + "---|" * len(r.columns)]
    table += ["| " + " | ".join(fmt(v) for v in row) + " |" for row in r.rows[:shown]]
    return "\n".join(table)


def derived_text(r: QueryResult, marker: str = "D1") -> str:
    """The answer for rows reshaped from the earlier answer (Phase T3, agent/refine.py). It says where the rows came
    from and what was done to them; the count of the earlier rows and every value of the transform are in the
    result's params (`of_rows_label`, `transform_label`), and every computed figure is a cell, so it all verifies."""
    src, what = r.source_rows, r.transform
    if not r.rows:
        return f"None of the earlier answer's {src} rows match: {what} [{marker}]."
    if len(r.rows) == 1 and len(r.columns) == 1:
        return f"{r.columns[0]}: {fmt(r.rows[0][0])} — from the earlier answer's {src} rows, {what} [{marker}]."
    n = len(r.rows)
    shown = int(settings().get("answer.template_rows", 25))
    head = f"From the earlier answer's {src} rows, {what}: {n} row{'' if n == 1 else 's'} [{marker}]:"
    tail = f"\n\nOnly the first rows are shown here; there are {n} in total." if n > shown else ""
    return head + "\n\n" + _markdown_table(r, shown) + tail


def try_template(r: QueryResult, tool: Optional[dict[str, Any]], allow_generated: bool = False,
                 marker: str = "D1", describe: bool = True) -> Optional[str]:
    """The answer text for one data result, or None when it should be left to the model.

    Fixed tools always qualify (their shape is known). With `allow_generated` (setting
    answer.templated_generated, Phase J3) a model-written query's rows are written the same way — the
    rows ARE the answer, and the 3-6 s answer-model call that used to restate them is skipped: a
    scalar as "<Column>: <value>", or "<value> — <Column> where <filters>" when its WHERE can be stated
    exactly (Golden case 99 read "EmployeeCount: 29", which did not say what was counted); one row as
    "Col: v · Col: v", more rows as a table under a sentence saying what the query looked for
    (agent/explain.py). The caller adds the filter values to the result's params (filter_params,
    explain.described_params), or a value with digits in it would not verify."""
    if r.transform and not r.error:
        return derived_text(r, marker)
    if r.error or not r.rows or not r.columns:
        return None
    if r.tool == "generated" and not allow_generated:
        return None
    tool = tool or {}
    if len(r.rows) == 1 and len(r.columns) == 1:
        value = r.rows[0][0]
        if r.tool == "generated" and is_blank_result(r.columns, r.rows):
            # A fixed tool's 0 is an answer (C7). A model-written query's 0 or NULL more often means the SQL
            # matched nothing — the router sent a policy question to the database — so the answer model
            # keeps judging it, and its not-found still triggers the documents retry (orchestrator). Since
            # Phase P0 the orchestrator treats such a result as no rows and goes to the documents directly.
            return None
        lead = _fill(tool["answer"], r, value=value) if tool.get("answer") else None
        # a model-written `SELECT COUNT(*) FROM …` without an alias comes back with an empty column name
        label = r.columns[0].strip() or ("Count" if re.search(r"\bcount\s*\(", r.sql or "", re.I) else "Value")
        filters = scalar_filters(r) if lead is None and describe else None
        if filters:
            return f"{fmt(value)} — {label} where {describe_filters(filters)} [{marker}]."
        return f"{lead or f'{label}: {fmt(value)}'} [{marker}]."
    # A model-written list says what its query looked for (agent/explain.py): "Found 100 rows where ShipDate is
    # before today (2026-10-26) and InvoicedQty is empty …" instead of a bare "Found 100 matching rows".
    from . import explain   # lazy: explain reads data/empty_retry, and data/tools.py imports this module
    desc = explain.describe(r) if describe and r.tool == "generated" else None
    if r.tool == "generated" and len(r.rows) == 1 and len(r.columns) <= 8:
        pairs = " · ".join(f"{c}: {fmt(v)}" for c, v in zip(r.columns, r.rows[0]))
        if desc and desc.where:
            return f"One row where {desc.conditions()}: {pairs} [{marker}]."
        return pairs + f" [{marker}]."
    n = len(r.rows)
    shown = int(settings().get("answer.template_rows", 25))
    # Phase T2: after "top 10" the rows are a cut of what the query returned — the lead counts what there was
    total = _int(r.params.get("of_rows_label")) or n
    lead = _fill(tool["answer_list"], r, n=total) if tool.get("answer_list") else None
    found = f"Found {total} matching row" + ("" if total == 1 else "s")
    note = modifier_note(r)
    head = explain.lead(desc, total, marker) if lead is None and desc else f"{lead or found} [{marker}]:"
    for end in (":", ".") if note else ():
        if f" [{marker}]{end}" in head:              # a tool's lead ends "[D1]:", a described list's "[D1]."
            head = head.replace(f" [{marker}]{end}", f"{note} [{marker}]{end}", 1)
            break
    table = ["| " + " | ".join(r.columns) + " |", "|" + "---|" * len(r.columns)]
    table += ["| " + " | ".join(fmt(v) for v in row) + " |" for row in r.rows[:shown]]
    tail = f"\n\nOnly the first rows are shown here; the query returned {n} in total." if n > shown else ""
    return head + "\n\n" + "\n".join(table) + tail
