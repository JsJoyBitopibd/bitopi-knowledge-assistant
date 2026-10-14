"""Watcher (Phase L): run every agent's watch rules (config/agents/*.yaml `watch:`) and store the signals in
data/index/signals.db for the morning brief. Read-only, guarded SQL over rag.* views; no model call.
Usage: python scripts/watch.py              # once
       python scripts/watch.py --every 60   # keep running, every 60 minutes (docker-compose service `watch`)
       python scripts/watch.py --risk       # Phase M: score and store the risk levels, then settle finished subjects
       python scripts/watch.py --risk-only  # the risk pass without the watch rules
Exit code 1 when a rule failed (the others are still stored).
"""
import argparse, time, _path  # noqa: F401
from datetime import datetime
from ragbot.data.catalog import load_catalogs
from ragbot.data.connectors import run
from ragbot.domain_agents import load_agents
from ragbot.domain_agents.signals import db_path, open_predictions, write_risk_run, write_run
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


# Which catalog each dimension's subjects are read from. One entry today; a second dimension is a line
# here plus its own config/rules/<dimension>.yaml, not new machinery.
RISK_CATALOGS = {"quality": "VistaQ_Default_RHL"}


def risk_once(timeout: int, dry_run: bool = False) -> int:
    """Phase M: score every subject in the window, store the run, then settle the subjects that finished."""
    from ragbot.domain_agents import risk

    failed = 0
    cats = load_catalogs()
    for dimension, rs in sorted(risk.load_rule_sets().items()):
        cat = cats.get(RISK_CATALOGS.get(dimension, ""))
        if cat is None:
            print(f"  SKIPPED {dimension}: no catalog mapped (RISK_CATALOGS)")
            failed += 1
            continue
        started = datetime.now()
        try:
            assessments, errors, source = risk.collect(rs, cat, timeout=timeout)
            newest = risk.newest_day(cat, timeout=timeout)
            running = risk.still_running(cat, timeout=timeout)
        except Exception as e:                      # one dimension failing must not stop the others
            print(f"  FAILED {dimension}: {e.__class__.__name__}: {e}")
            failed += 1
            continue

        running_now = risk.predictable(assessments, running)
        levels = {lv: sum(a.level == lv for a in running_now) for lv in ("red", "amber", "green", "info")}
        shape = (f"{len(running_now)} running subject(s) of {len(assessments)} scored "
                 f"({', '.join(f'{n} {lv}' for lv, n in levels.items() if n)})")
        if dry_run:
            print(f"{datetime.now():%Y-%m-%d %H:%M} [dry run] {dimension}: {shape}; "
                  f"{len(running)} still planned; VistaQ's newest day {newest}; errors={errors}")
            for e in errors:
                print(f"  PROBLEM {e}")
            continue

        # Only subjects still in production get a prediction. A file that has already finished
        # would otherwise be predicted and settled in the same run, and a prediction with no lead
        # time over its outcome is worth nothing to the backtest the Stage-2 gate asks for.

        run_id = write_risk_run(dimension, running_now, started, errors, source=source)
        print(f"{datetime.now():%Y-%m-%d %H:%M} risk run {run_id} [{dimension}]: {shape} in "
              f"{(datetime.now() - started).total_seconds():.1f} s -> {db_path()}")
        for e in errors:
            print(f"  PROBLEM {e}")
            failed += 1

        done = risk.settle(dimension, assessments, running, newest)
        print(f"  settled {done} finished subject(s); {len(open_predictions(dimension))} still open"
              + (f"; VistaQ's newest day is {newest}" if newest else "; VistaQ holds no inspections"))
    return failed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--every", type=float, help="keep running, every N minutes")
    ap.add_argument("--timeout", type=int, default=60, help="per-rule query timeout (seconds)")
    ap.add_argument("--risk", action="store_true", help="also score and store the Stage-2 risk levels")
    ap.add_argument("--risk-only", action="store_true", help="only the risk pass, no watch rules")
    ap.add_argument("--dry-run", action="store_true", help="score but store nothing (risk pass only)")
    a = ap.parse_args()
    while True:
        failed = 0 if a.risk_only else once(a.timeout)
        if a.risk or a.risk_only:
            failed += risk_once(max(a.timeout, 180), dry_run=a.dry_run)
        if not a.every:
            raise SystemExit(1 if failed else 0)
        time.sleep(a.every * 60)


if __name__ == "__main__":
    main()
