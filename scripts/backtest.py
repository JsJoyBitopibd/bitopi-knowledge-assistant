"""Stage-2 backtest (gate M✓): compare the stored daily risk levels with what actually happened and write
logs/backtest_<dimension>_<date>.md (internal content: file numbers appear, and logs/ is git-ignored).
Read-only over data/index/signals.db; no database query, no model call.
Usage: python scripts/backtest.py [--dimension quality] [--until YYYY-MM-DD] [--store PATH]
Exit code 0 when the evidence meets every minimum, 3 when it does not yet, 1 when there is no store or no run.
"""
import argparse, _path  # noqa: F401
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from ragbot.config import log_dir
from ragbot.domain_agents import backtest
from ragbot.domain_agents.signals import db_path


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Stage-2 backtest of the stored risk levels (read-only).")
    ap.add_argument("--dimension", default="quality", help="risk dimension (default: quality)")
    ap.add_argument("--until", type=date.fromisoformat, metavar="YYYY-MM-DD",
                    help="ignore runs and outcomes after this date (reproduce an earlier baseline)")
    ap.add_argument("--store", type=Path, metavar="PATH", help="signals.db to read (default: data/index/signals.db)")
    a = ap.parse_args(argv)

    store = a.store or db_path()
    if not store.exists():
        print(f"no store at {store}")
        return 1
    runs, rows = backtest.load(store, a.dimension, a.until)
    if not runs:
        print(f"no {a.dimension} risk run in {store}" + (f" up to {a.until}" if a.until else ""))
        return 1

    result = backtest.compute(runs, rows, a.dimension, rule=backtest.dhu_rule(a.dimension))
    result.store, result.until = str(store), a.until
    v = backtest.verdict(result)
    now = datetime.now()
    out = log_dir() / f"backtest_{a.dimension}_{now:%Y%m%d}.md"
    out.write_text(backtest.render_markdown(result, v, now), encoding="utf-8")
    print("\n".join(backtest.verdict_lines(v)))
    print(f"report: {out}")
    return 0 if v.sufficient else 3


if __name__ == "__main__":
    raise SystemExit(main())
