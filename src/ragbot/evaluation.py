"""The golden-set eval's scoring rules, kept out of scripts/eval.py so they can be tested.

Three rules the 2026-10-14 morning run showed were missing:

- **A provider outage is not a wrong answer.** Four answers that day were the quota message, scored
  hit 0 / correct 0 as if the assistant had answered badly — and on a not-found case the same message
  (which is marked not-found) would have been scored *correct*. Such a row is unscored and re-run.
- **A run with unscored or outage rows is not a baseline.** `compare()` took the newest run covering
  every case, which that day was the damaged one.
- **Every score is gated, not four of six.** The 5-point rule (CLAUDE.md) is about any score; not-found
  and refuse were never compared, so a 20-point not-found drop printed no BLOCK.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

INFRA_KINDS = {"quota", "timeout"}          # the provider was out; `db`, `auth`, `other` stay scored
_INFRA_WARNING = re.compile(r"^llm (quota|timeout)\b")
RUN_NAME = re.compile(r"\d{8}T\d{4}")       # eval/results/<date>T<time>.json; other reports are skipped
SCORES = ("hit_rate", "faithfulness", "correctness", "citation_validity", "not_found_ok", "refuse_ok")
BLOCK_DROP = 0.05


def infra_failure(answer: Any) -> bool:
    """The answer is the provider's failure, not the assistant's: score nothing and re-run the case."""
    return (getattr(answer, "error_kind", "") or "") in INFRA_KINDS


def outage_row(row: dict[str, Any]) -> bool:
    """A stored row that was lost to an outage — marked unscored, or (runs written before this rule)
    carrying the orchestrator's `llm quota:` / `llm timeout:` warning."""
    return bool(row.get("unscored")) or any(_INFRA_WARNING.match(w or "") for w in row.get("warnings", []))


def complete(rows: list[dict[str, Any]]) -> bool:
    return not any(outage_row(r) for r in rows)


def scores(rows: list[dict[str, Any]]) -> dict[str, Optional[float]]:
    """The six gated scores over these rows. None where no row carries the score (a kind not present)."""
    def mean(xs: list[float]) -> Optional[float]:
        return sum(xs) / len(xs) if xs else None

    col = {"hit_rate": "hit", "faithfulness": "faithful", "correctness": "correct",
           "citation_validity": "citation_valid"}
    out = {k: mean([r[c] for r in rows if r.get(c) is not None]) for k, c in col.items()}
    for k, kind in (("not_found_ok", "not_found"), ("refuse_ok", "refuse")):
        out[k] = mean([r["correct"] for r in rows if r.get("kind") == kind and r.get("correct") is not None])
    return out


def pick_baseline(current: Path, ids: set[Any]) -> Optional[tuple[str, list[dict[str, Any]]]]:
    """The most recent earlier *complete* run that covered every one of `ids`, as (file name, its rows for
    those ids). A run with an unscored or outage row anywhere is skipped, not just on these ids: an outage
    that took four cases also took the judge's view of the day."""
    runs = sorted(f for f in current.parent.glob("*.json") if RUN_NAME.fullmatch(f.stem))
    for f in reversed([f for f in runs if f.name < current.name]):
        rows = json.loads(f.read_text(encoding="utf-8")).get("rows", [])
        old = {r["id"]: r for r in rows}
        if ids <= set(old) and complete(rows):
            return f.name, [old[i] for i in sorted(ids)]
    return None


def baseline_rows(path: Path, ids: set[Any]) -> list[dict[str, Any]]:
    """An explicitly named baseline's rows for `ids` (`eval.py --baseline`); every id must be in it."""
    old = {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8")).get("rows", [])}
    missing = sorted(ids - set(old))
    if missing:
        raise ValueError(f"{path.name} has no case(s) {missing}")
    return [old[i] for i in sorted(ids)]


def kind_changed(old_rows: list[dict[str, Any]], new_rows: list[dict[str, Any]]) -> list[Any]:
    """Cases whose kind differs between the two runs: the golden case was redefined in between (case 44, a
    not-found case until Phase R gave it an answer). Compared as they are, they move a score that is not about
    the same question — one case is 25 points of not-found — so compare() sets them aside and names them."""
    old = {r["id"]: r.get("kind") for r in old_rows}
    return sorted(r["id"] for r in new_rows if r["id"] in old and old[r["id"]] != r.get("kind"))


def blocks(before: dict[str, Optional[float]], after: dict[str, Optional[float]],
           drop: float = BLOCK_DROP) -> list[str]:
    """The scores that dropped by more than `drop` (five points). A score only one side has is not compared."""
    return [k for k in SCORES
            if before.get(k) is not None and after.get(k) is not None and before[k] - after[k] > drop + 1e-9]
