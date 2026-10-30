"""Delete stored conversations untouched for ui.conversations.retention_days and the ones users deleted (Phase U1).

    python scripts/purge_conversations.py [--days N]

The app also does this once per process start; this is for a schedule (the watcher's container, a cron job).
Nothing else is touched: data/index/conversations.db only.
"""
from __future__ import annotations

import argparse

import _path  # noqa: F401

from ragbot import conversations
from ragbot.config import settings


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=None, help="override ui.conversations.retention_days")
    a = ap.parse_args()
    days = a.days if a.days is not None else int(settings().get("ui.conversations.retention_days", 90))
    print(f"removed {conversations.purge(days)} conversation(s) older than {days} day(s) or deleted")


if __name__ == "__main__":
    main()
