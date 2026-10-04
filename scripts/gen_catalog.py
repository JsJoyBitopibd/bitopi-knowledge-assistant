"""Write the catalog YAML for a discovered database (config/catalog/<db>.yaml) — Phase J.

A database with no curated rag.* views is still answerable: schema RAG (Phase C) shows the SQL model the
few discovered tables a question needs, and the guard allows only those. What such a catalog must carry
is what the router and the model cannot read off the schema: a one-line business description, the words
staff use for this data (`keywords:`), the connection, and the table-name patterns never to offer
(dated copies, backups, scratch tables). Run after scripts/discover_schema.py --json:

  python scripts/gen_catalog.py HR --database HR --description "Human resources planning: ..." \
      --keywords "hr,manpower,headcount,employee"

Writes config/catalog/HR.yaml (refuses to overwrite a file unless --force: the YAML is hand-edited
afterwards). `--connection-env` defaults to SQLSERVER_CONN_PRODUCTION: one read-only login covers the
whole instance, and `connection_database:` points it at this database (docs/DATA_ACCESS.md §1).
The auto keywords are the most common words in the names of the tables that hold rows, so a question
that uses a table's own vocabulary ("GRN", "voucher", "recipe") reaches the right database even when the
hand-written list missed it.
"""
import argparse, json, re, _path  # noqa: F401
from collections import Counter
from pathlib import Path

import yaml

from ragbot.data.sensitive import is_sensitive, words

ROOT = Path(__file__).resolve().parents[1]

# fnmatch patterns (case-insensitive, on schema.name) of discovered tables never offered to the SQL model:
# history/backup copies, dated snapshots (…_12_13_2022, …_20260816), spreadsheet imports (…$), scratch,
# deleted-row and test tables. Sensitive tables are excluded automatically (data/sensitive.py).
EXCLUDE = ["*back", "*back_*", "*backup*", "*_bak*", "*bak_*", "*$", "*$'", "*$_*", "*_old*", "*.tmp*", "*.temp_*",
           "*_temp", "*_test*", "*.test_*", "*daytest", "*delete*", "*_mail_on_*", "*_before_*", "*_till*",
           "*_[0-9]_*", "*_[0-9][0-9]_*", "*_[0-9][0-9][0-9][0-9]*", "*.backfill_*", "*.shadow_*", "*.baseline_*",
           "*._linechange_*"]

RULES = [
    "Use TOP (n), never LIMIT. Use half-open date ranges with literal dates (col >= '2026-09-01' AND col < '2026-10-01').",
    "Today is {today}. \"This week\" = Monday of this week to next Monday; \"next week\" = next Monday to the following Monday.",
    "This database has no rag.* views: query the tables listed for the question as schema.name and name the columns you need (never SELECT *).",
    "Do NOT compute totals in prose; return the rows or an aggregate (COUNT/SUM in the SQL).",
    "Never join to tables outside the catalog. If a question needs a column that is not listed here, return \"System doesn't have the data.\"",
]

_STOP = {"tbl", "dbo", "info", "master", "details", "detail", "data", "new", "old", "log", "temp", "tmp", "test",
         "back", "backup", "view", "the", "and", "wise", "child", "list", "all", "per", "for", "sub", "set", "type",
         "code", "status", "update", "entry", "history", "mapping", "company", "day", "daily", "month", "monthly",
         "process", "trans", "report", "user", "users", "menu", "application", "migrations", "aggregate"}


def auto_keywords(doc: dict, limit: int = 12) -> list[str]:
    """Most common words in the names of non-empty, non-excluded, non-sensitive tables."""
    import fnmatch
    counts: Counter = Counter()
    for t in doc["tables"]:
        if t["kind"] != "table" or not (t.get("rows") or 0) or t.get("sensitive"):
            continue
        low = t["name"].lower()
        if any(fnmatch.fnmatchcase(low, p) for p in EXCLUDE):
            continue
        name = t["name"].split(".", 1)[1]
        counts.update({w for w in words(name) if len(w) > 2 and w not in _STOP and not w.isdigit()
                       and not is_sensitive(w)})
    return [w for w, _ in counts.most_common(limit)]


def render(doc: dict, a: argparse.Namespace) -> str:
    given = [k.strip() for k in (a.keywords or "").split(",") if k.strip()]
    auto = [w for w in auto_keywords(doc) if w not in {g.lower() for g in given}]
    n_t = sum(1 for t in doc["tables"] if t["kind"] == "table")
    body = {
        "database": a.database_name, "description": a.description.strip(), "dialect": "tsql",
        "connection_env": a.connection_env, "connection_database": a.database or a.database_name,
        "keywords": given + auto, "exclude_tables": EXCLUDE, "rules": RULES, "views": [],
    }
    head = (f"# {a.database_name}: no curated views yet — answered through schema RAG over the discovered tables\n"
            f"# (config/catalog/discovered/{a.database_name}.json: {n_t} tables, discovered {doc.get('generated_at', '')[:10]}).\n"
            f"# Written by scripts/gen_catalog.py; edit the description and keywords freely. `keywords:` are the\n"
            f"# words staff use for this data — the router scores them to pick the database for a question\n"
            f"# (src/ragbot/data/db_router.py). The last {len(auto)} were derived from table names.\n"
            f"# `connection_database:` opens this database through the login of `connection_env` (docs/DATA_ACCESS.md §1).\n")
    text = yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=110)
    return head + text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("database_name", help="name as discovered (config/catalog/discovered/<name>.json) and shown in [D#] references")
    ap.add_argument("--database", default=None, help="database to open on the server (default: the same name)")
    ap.add_argument("--connection-env", default="SQLSERVER_CONN_PRODUCTION")
    ap.add_argument("--description", required=True, help="one line of business language: what this database holds")
    ap.add_argument("--keywords", default="", help="comma-separated words staff use for this data")
    ap.add_argument("--force", action="store_true", help="overwrite an existing catalog YAML")
    a = ap.parse_args()
    src = ROOT / "config" / "catalog" / "discovered" / f"{a.database_name}.json"
    if not src.exists():
        raise SystemExit(f"no discovery file {src}; run scripts/discover_schema.py --json first")
    dest = ROOT / "config" / "catalog" / f"{a.database_name}.yaml"
    if dest.exists() and not a.force:
        raise SystemExit(f"{dest} exists (hand-edited?); pass --force to overwrite")
    doc = json.loads(src.read_text(encoding="utf-8"))
    dest.write_text(render(doc, a), encoding="utf-8")
    print(f"wrote {dest}")


if __name__ == "__main__":
    main()
