"""Data-quality report (Phase K4): run the read-only checks in config/data_quality.yaml and write
logs/data_quality_<date>.md (internal content: logs/ is git-ignored).
Usage: python scripts/data_quality.py [--timeout SECONDS] [--check NAME ...]
Exit code 1 when a check could not run (stale data or issues are findings, not failures).
"""
import argparse, _path  # noqa: F401
from datetime import datetime
from ragbot.config import ROOT, log_dir
from ragbot.data.catalog import load_catalogs
from ragbot.data.connectors import run
from ragbot.data.quality import load_checks, render_markdown, run_checks


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=120, help="per-check timeout in seconds")
    ap.add_argument("--check", nargs="*", help="run only these checks")
    a = ap.parse_args()
    checks = load_checks(ROOT / "config" / "data_quality.yaml")
    if a.check:
        checks = [c for c in checks if c["name"] in a.check]
    results = run_checks(checks, load_catalogs(), run, timeout=a.timeout)
    now = datetime.now()
    out = log_dir() / f"data_quality_{now:%Y%m%d}.md"
    out.write_text(render_markdown(results, now), encoding="utf-8")
    for r in results:
        print(f"{r.status:6} {r.name:34} {r.database:13} {r.value}  ({r.seconds}s)")
    print(f"report: {out}")
    raise SystemExit(1 if any(r.status == "error" for r in results) else 0)


if __name__ == "__main__":
    main()
