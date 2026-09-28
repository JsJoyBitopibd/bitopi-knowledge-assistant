"""Refresh the pre-computed aggregates (Phase C6) from config/aggregates.yaml into data/index/aggregates.db.

Each aggregate's SQL is checked by guard.assert_read_only, its rag.* views are expanded like any other
query, and it is read with one streaming, read-only, rolled-back scan. The local table is swapped in
atomically, so the app keeps answering from the previous copy while this runs.

Schedule it nightly, off-hours (the PCD history scan reads 4.2M rows):
  Windows Task Scheduler:  python scripts/refresh_aggregates.py        (daily, e.g. 02:00)
  or leave it running:     python scripts/refresh_aggregates.py --every 24

Usage: python scripts/refresh_aggregates.py [name ...] [--every HOURS] [--timeout SECONDS]
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
                                                     tool=f"aggregate:{spec['name']}"),
                                    spec.get("indexes", []), spec.get("views", []))
            print(f"{datetime.now():%Y-%m-%d %H:%M} {spec['name']}: {rows:,} rows in {time.perf_counter() - t0:.0f} s")
        except Exception as e:   # keep the previous copy; report and continue with the next aggregate
            failed += 1
            print(f"{datetime.now():%Y-%m-%d %H:%M} {spec['name']}: FAILED after {time.perf_counter() - t0:.0f} s — "
                  f"{e.__class__.__name__}: {e}")
    return failed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("names", nargs="*", help="aggregates to refresh (default: all)")
    ap.add_argument("--every", type=float, help="keep running, refreshing every N hours")
    ap.add_argument("--timeout", type=int, default=900, help="server-side timeout per aggregate (seconds)")
    a = ap.parse_args()
    while True:
        failed = refresh(a.names, a.timeout)
        if not a.every:
            raise SystemExit(1 if failed else 0)
        time.sleep(a.every * 3600)


if __name__ == "__main__":
    main()
