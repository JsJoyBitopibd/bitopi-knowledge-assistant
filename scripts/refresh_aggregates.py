"""Refresh the pre-computed aggregates (Phase C6) from config/aggregates.yaml into data/index/aggregates.db.

Each aggregate's SQL is checked by guard.assert_read_only, its rag.* views are expanded like any other
query, and it is read with one streaming, read-only, rolled-back scan. The local table is swapped in
atomically, so the app keeps answering from the previous copy while this runs.

Schedule it nightly, off-hours (the PCD history scan reads 4.2M rows, about 14 minutes):
  Windows Task Scheduler:  python scripts/refresh_aggregates.py              (daily, e.g. 02:00)
  or leave it running:     python scripts/refresh_aggregates.py --at 02:00   (the docker-compose service)

--at HH:MM refreshes every copy at that local time each day. At start it builds only the copies that are
missing, so recreating the container does not move the production read to the time of the recreate:
--every 24 refreshed everything on start and then every 24 hours, so a recreate at 14:58 put the read at
~15:13 every day. --every HOURS is kept for compatibility.

Usage: python scripts/refresh_aggregates.py [name ...] [--at HH:MM | --every HOURS] [--timeout SECONDS]
"""
import argparse, time, _path  # noqa: F401
from datetime import datetime

from ragbot.data import aggregates
from ragbot.data.catalog import load_catalogs
from ragbot.data.connectors import stream_sqlserver
from ragbot.data.guard import assert_read_only
from ragbot.data.virtual import rewrite_virtual
from ragbot.auth.models import Scope


def refresh(names: list[str], timeout: int) -> int:
    cats = load_catalogs()
    failed = 0
    for spec in aggregates.load_specs():
        if names and spec["name"] not in names:
            continue
        cat = cats[spec["database"]]
        t0 = time.perf_counter()
        try:
            assert_read_only(spec["sql"], cat.dialect)
            sql_exec, _ = rewrite_virtual(spec["sql"].strip(), cat, scope=Scope.unrestricted())   # the copy holds every factory; reads are scoped
            rows = aggregates.write(spec["name"],
                                    stream_sqlserver(sql_exec, conn_env=cat.connection_env, timeout=timeout,
                                                     tool=f"aggregate:{spec['name']}", database=cat.connection_database),
                                    spec.get("indexes", []), spec.get("views", []))
            print(f"{datetime.now():%Y-%m-%d %H:%M} {spec['name']}: {rows:,} rows in {time.perf_counter() - t0:.0f} s")
        except Exception as e:   # keep the previous copy; report and continue with the next aggregate
            failed += 1
            print(f"{datetime.now():%Y-%m-%d %H:%M} {spec['name']}: FAILED after {time.perf_counter() - t0:.0f} s — "
                  f"{e.__class__.__name__}: {e}")
    return failed


def daily(names: list[str], at: str, timeout: int) -> None:
    """--at: build the missing copies now, then refresh every selected copy at `at` (local time) each day."""
    specs = [s for s in aggregates.load_specs() if not names or s["name"] in names]
    absent = aggregates.missing(specs)
    if absent:      # never refresh([]) here: an empty list means every aggregate
        print(f"{datetime.now():%Y-%m-%d %H:%M} missing: {', '.join(absent)}; building them now")
        refresh(absent, timeout)
    else:
        print(f"{datetime.now():%Y-%m-%d %H:%M} all {len(specs)} copies present; nothing to build now")
    while True:
        now = datetime.now()
        when = aggregates.next_run(at, now)
        print(f"{now:%Y-%m-%d %H:%M} next refresh at {when:%Y-%m-%d %H:%M} "
              f"(in {aggregates.seconds_until(at, now) / 3600:.1f} h)")
        aggregates.sleep_until(when)            # chunks of at most 300 s against the wall clock
        refresh(names, timeout)


def _hhmm(value: str) -> str:
    """argparse type for --at, so a bad time stops the script at start and not at the first refresh."""
    try:
        aggregates.next_run(value, datetime.now())
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    return value.strip()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="aggregates to refresh (default: all)")
    when = ap.add_mutually_exclusive_group()
    when.add_argument("--at", type=_hhmm, metavar="HH:MM",
                      help="keep running: build missing copies now, then refresh every day at HH:MM (local time)")
    when.add_argument("--every", type=float, help="keep running, refreshing on start and then every N hours")
    ap.add_argument("--timeout", type=int, default=900, help="server-side timeout per aggregate (seconds)")
    a = ap.parse_args()
    if a.at:
        daily(a.names, a.at, a.timeout)         # runs until the process is stopped
        return
    while True:
        failed = refresh(a.names, a.timeout)
        if not a.every:
            raise SystemExit(1 if failed else 0)
        time.sleep(a.every * 3600)


if __name__ == "__main__":
    main()
