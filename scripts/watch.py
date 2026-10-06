"""Watcher (Phase L): run every agent's watch rules (config/agents/*.yaml `watch:`) and store the signals in
data/index/signals.db for the morning brief. Read-only, guarded SQL over rag.* views; no model call.
Usage: python scripts/watch.py              # once
       python scripts/watch.py --every 60   # keep running, every 60 minutes (docker-compose service `watch`)
Exit code 1 when a rule failed (the others are still stored).
"""
import argparse, time, _path  # noqa: F401
from datetime import datetime
from ragbot.data.catalog import load_catalogs
from ragbot.data.connectors import run
from ragbot.domain_agents import load_agents
from ragbot.domain_agents.signals import db_path, write_run
from ragbot.domain_agents.watch import run_all


def once(timeout: int) -> int:
    started = datetime.now()
    signals, errors = run_all(load_agents(), load_catalogs(), run, timeout=timeout)
    run_id = write_run(signals, started, errors)
    levels = {lv: sum(s.level == lv for s in signals) for lv in ("red", "amber", "green", "info")}
    print(f"{datetime.now():%Y-%m-%d %H:%M} run {run_id}: {len(signals)} signal(s) "
          f"({', '.join(f'{n} {lv}' for lv, n in levels.items() if n)}) in {(datetime.now() - started).total_seconds():.1f} s"
          f" -> {db_path()}")
    for e in errors:
        print(f"  FAILED {e}")
    return len(errors)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=float, help="keep running, every N minutes")
    ap.add_argument("--timeout", type=int, default=60, help="per-rule query timeout (seconds)")
    a = ap.parse_args()
    while True:
        failed = once(a.timeout)
        if not a.every:
            raise SystemExit(1 if failed else 0)
        time.sleep(a.every * 60)


if __name__ == "__main__":
    main()
