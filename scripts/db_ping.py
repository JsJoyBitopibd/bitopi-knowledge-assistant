"""Prove a database connection string works through the real connector (read-only, rolls back).
Usage: python scripts/db_ping.py [database ...]   (default: every database in the loaded catalogs)
"""
import argparse, _path  # noqa: F401
from ragbot.data.catalog import load_catalogs
from ragbot.data.connectors import run


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("databases", nargs="*", help="database name(s) as they appear in the catalog YAML")
    a = ap.parse_args()
    cats = load_catalogs()
    names = a.databases or list(cats)
    if not names:
        print("no catalogs loaded under config/catalog/*.yaml (only *.example.yaml present?)")
        return
    for name in names:
        cat = cats.get(name)
        if cat is None:
            print(f"{name}: not a loaded catalog (have: {', '.join(cats)})")
            continue
        sql = "SELECT @@VERSION AS Version, DB_NAME() AS DbName, SUSER_SNAME() AS LoginName" if cat.dialect == "tsql" \
            else "SELECT VERSION() AS Version, DATABASE() AS DbName, CURRENT_USER() AS LoginName"
        try:
            cols, rows = run(cat.engine, sql, {}, conn_env=cat.connection_env, tool="db_ping")
            row = dict(zip(cols, rows[0])) if rows else {}
            print(f"{name} ({cat.connection_env}): OK — {row}")
        except Exception as e:
            print(f"{name} ({cat.connection_env}): FAILED — {e.__class__.__name__}: {e}")


if __name__ == "__main__":
    main()
