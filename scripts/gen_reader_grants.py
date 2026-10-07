"""Write the SQL a sysadmin runs to create the assistant's read-only login (`rag_reader`) with read access to
every database on the instance, plus a DENY on each sensitive table and column that schema discovery found —
and the matching rollback script. Model and reasoning: docs/DATA_ACCESS.md §1, src/ragbot/data/grants.py.

Output goes to private/ (git-ignored: the DENY list names the sensitive tables of internal systems). This
script only WRITES files — it never connects to a database or executes anything. The password is left as the
sqlcmd variable $(RAG_READER_PASSWORD).

Usage: python scripts/gen_reader_grants.py [--login rag_reader] [--out private]
"""
import argparse, _path  # noqa: F401
from pathlib import Path

from ragbot.data.catalog import load_catalogs
from ragbot.data.grants import render_grants, render_rollback, sensitive_counts

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--login", default="rag_reader")
    ap.add_argument("--out", default="private", help="folder for the .sql files (git-ignored by default)")
    a = ap.parse_args()
    cats = {name: cat for name, cat in load_catalogs().items() if cat.dialect == "tsql"}
    if not cats:
        print("no T-SQL catalogs loaded under config/catalog/*.yaml"); raise SystemExit(1)
    out_dir = ROOT / a.out
    out_dir.mkdir(parents=True, exist_ok=True)
    grants, rollback = out_dir / f"{a.login}_grants.sql", out_dir / f"{a.login}_rollback.sql"
    grants.write_text(render_grants(a.login, cats), encoding="utf-8")
    rollback.write_text(render_rollback(a.login, cats), encoding="utf-8")
    for name, cat in cats.items():
        whole, cols = sensitive_counts(cat)
        note = "" if cat.tables else "  — no discovery yet: run scripts/discover_schema.py --json first"
        print(f"{name}: {len(cat.tables)} discovered objects; DENY {whole} whole + {cols} columns{note}")
    print(f"wrote {grants}")
    print(f"wrote {rollback}")


if __name__ == "__main__":
    main()
