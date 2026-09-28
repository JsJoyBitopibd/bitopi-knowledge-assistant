"""Read-only schema discovery for a SQL Server database. Never part of the app's runtime path.

Two outputs:
- default: docs/schema/<db>.md — a human summary (tables by row count, views, columns of interest),
  used while writing a curated config/catalog/<db>.yaml.
- --json: config/catalog/discovered/<db>.json — the machine-readable catalog of every table and view
  (columns and types, primary keys, foreign keys, row counts, MS_Description) that schema RAG uses
  from Phase C2 on (docs/ROADMAP.md C1). The folder is git-ignored; re-run the script to refresh it.

Reads sys.* catalog views only, except with --samples: then, for short text columns of small base
tables (<= --sample-max-rows rows), it reads up to 30 distinct values so the SQL model knows the
codes a column holds (e.g. ShipmentStatus: TO SHIP / SHIPPED). Sensitive columns and tables
(src/ragbot/data/sensitive.py) are never sampled. The connection is read-only, autocommit off, and
rolled back before closing. A full --samples pass is thousands of queries; --tables-from-logs limits it
to the tables the SQL model has actually needed (data/schema_usage.py). Values sampled in an earlier run
are kept in the new file unless the column is sampled again.

Usage:
  python scripts/discover_schema.py <ConnEnvVarName> <FriendlyName> [--schemas dbo,PPM]
  python scripts/discover_schema.py SQLSERVER_CONN_BITOPISPLINT BitopiSplint --json [--samples]
  python scripts/discover_schema.py SQLSERVER_CONN_BITOPISPLINT BitopiSplint --json --samples --tables-from-logs
"""
import argparse, json, sys, _path  # noqa: F401
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _q(ident: str) -> str:
    """Bracket-quote an identifier that came from sys.* (never from user input)."""
    return "[" + ident.replace("]", "]]") + "]"


def _schema_filter(schemas):
    return "" if not schemas else "AND s.name IN (" + ",".join(f"'{s}'" for s in schemas) + ")"


def write_markdown(cur, a, schemas) -> None:
    sf = _schema_filter(schemas)
    out = [f"# Schema discovery: {a.friendly} ({a.conn_env})\n", "Read-only metadata only — no data rows.\n"]
    out.append("## Tables by row count\n")
    for r in cur.execute(f"""
            SELECT TOP {a.top} s.name, t.name, SUM(p.rows)
            FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
            WHERE 1=1 {sf}
            GROUP BY s.name, t.name ORDER BY SUM(p.rows) DESC""").fetchall():
        out.append(f"- `{r[0]}.{r[1]}` — {r[2]:,} rows")
    out.append("\n## Views\n")
    for r in cur.execute(f"""
            SELECT s.name, v.name FROM sys.views v JOIN sys.schemas s ON s.schema_id = v.schema_id
            WHERE 1=1 {sf} ORDER BY s.name, v.name""").fetchall():
        out.append(f"- `{r[0]}.{r[1]}`")
    out.append("\n## Columns of interest (order/PCD/file/style/buyer/ship)\n")
    for r in cur.execute(f"""
            SELECT s.name, o.name, o.type, c.name, ty.name
            FROM sys.columns c JOIN sys.objects o ON o.object_id = c.object_id
            JOIN sys.schemas s ON s.schema_id = o.schema_id JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            WHERE o.type IN ('U', 'V') {sf} AND (
                c.name LIKE '%PCD%' OR c.name LIKE '%FileRef%' OR c.name LIKE '%Style%' OR c.name LIKE '%Buyer%'
                OR c.name LIKE '%Ship%' OR c.name LIKE '%Order%' OR c.name LIKE '%Factory%' OR c.name LIKE '%Meeting%')
            ORDER BY o.name, c.column_id""").fetchall():
        out.append(f"- `{r[0]}.{r[1]}.{r[3]}` ({r[2].strip()}) : {r[4]}")
    dest = ROOT / "docs" / "schema"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{a.friendly}.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {dest / f'{a.friendly}.md'} ({len(out)} lines)")


def write_json(cn, cur, a, schemas) -> None:
    from ragbot.data.discovery import assemble, sample_candidates
    sf = _schema_filter(schemas)
    tables = [tuple(r) for r in cur.execute(f"""
        SELECT s.name, o.name, o.type,
               (SELECT SUM(p.rows) FROM sys.partitions p WHERE p.object_id = o.object_id AND p.index_id IN (0, 1))
        FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id
        WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0 {sf}
        ORDER BY s.name, o.name""").fetchall()]
    columns = [tuple(r) for r in cur.execute(f"""
        SELECT s.name, o.name, c.name, ty.name, c.max_length, c.is_nullable, c.column_id
        FROM sys.columns c JOIN sys.objects o ON o.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id = o.schema_id JOIN sys.types ty ON ty.user_type_id = c.user_type_id
        WHERE o.type IN ('U', 'V') AND o.is_ms_shipped = 0 {sf}""").fetchall()]
    pks = [tuple(r) for r in cur.execute(f"""
        SELECT s.name, t.name, c.name, ic.key_ordinal
        FROM sys.indexes i JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        JOIN sys.tables t ON t.object_id = i.object_id JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE i.is_primary_key = 1 {sf}""").fetchall()]
    fks = [tuple(r) for r in cur.execute(f"""
        SELECT fk.name, s.name, t.name, c.name, rs.name, rt.name, rc.name, fkc.constraint_column_id
        FROM sys.foreign_keys fk
        JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id
        JOIN sys.tables t ON t.object_id = fk.parent_object_id JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.columns c ON c.object_id = fkc.parent_object_id AND c.column_id = fkc.parent_column_id
        JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id JOIN sys.schemas rs ON rs.schema_id = rt.schema_id
        JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
        WHERE 1=1 {sf}""").fetchall()]
    descs = [(r[0], r[1], r[2], r[3]) for r in cur.execute(f"""
        SELECT s.name, o.name, CASE WHEN ep.minor_id = 0 THEN NULL ELSE c.name END, CAST(ep.value AS nvarchar(4000))
        FROM sys.extended_properties ep JOIN sys.objects o ON o.object_id = ep.major_id
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        LEFT JOIN sys.columns c ON c.object_id = ep.major_id AND c.column_id = ep.minor_id
        WHERE ep.class = 1 AND ep.name = 'MS_Description' AND o.type IN ('U', 'V') {sf}""").fetchall()]

    dest = ROOT / "config" / "catalog" / "discovered"
    f = dest / f"{a.friendly}.json"
    from ragbot.data.discovery import carried_samples
    previous = carried_samples(json.loads(f.read_text(encoding="utf-8")) if f.exists() else None)
    samples = {}
    if a.samples:
        only = None
        if a.tables:
            only = {t.strip().lower() for t in a.tables.split(",") if t.strip()}
        elif a.tables_from_logs:
            from ragbot.data.catalog import load_catalogs
            from ragbot.data.schema_usage import used_tables
            cat = load_catalogs().get(a.friendly)
            only = {n.lower() for n in used_tables(cat)} if cat else set()
            print(f"tables from the logs ({len(only)}): {', '.join(sorted(only)) or 'none'}")
        cands = sample_candidates(tables, columns, a.sample_max_rows, only=only)
        print(f"sampling {len(cands)} short text columns of tables with <= {a.sample_max_rows:,} rows"
              + (f" in {len(only)} selected table(s)" if only is not None else ""))
        cn.timeout = 10
        for s, t, c in cands:
            try:
                vals = [r[0] for r in cur.execute(
                    f"SELECT DISTINCT TOP 31 {_q(c)} FROM {_q(s)}.{_q(t)} WITH (NOLOCK) WHERE {_q(c)} IS NOT NULL"
                ).fetchall()]
            except Exception as e:
                print(f"  skip {s}.{t}.{c}: {e.__class__.__name__}", file=sys.stderr)
                continue
            if 0 < len(vals) <= 30:
                samples[(s, t, c)] = sorted(str(v).strip() for v in vals)
    kept = {k: v for k, v in previous.items() if k not in samples}
    samples = {**kept, **samples}                 # a column sampled now replaces its old values
    if kept:
        print(f"kept the values of {len(kept)} column(s) sampled in an earlier run")

    doc = assemble(a.friendly, a.conn_env, tables, columns, pks, fks, descs, samples)
    dest.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(doc, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    n_t = sum(1 for t in doc["tables"] if t["kind"] == "table")
    n_fk = sum(len(t["foreign_keys"]) for t in doc["tables"])
    n_s = sum(1 for t in doc["tables"] for c in t["columns"] if c["sensitive"])
    print(f"wrote {f}: {n_t} tables, {len(doc['tables']) - n_t} views, {len(columns)} columns, "
          f"{len(pks)} PK columns, {n_fk} foreign keys, {len(descs)} descriptions, {len(samples)} sampled columns, "
          f"{n_s} sensitive columns")


def main() -> None:
    import pyodbc
    ap = argparse.ArgumentParser()
    ap.add_argument("conn_env", help="env var name holding the ODBC connection string")
    ap.add_argument("friendly", help="short name used for the output file, e.g. BitopiSplint")
    ap.add_argument("--schemas", default=None, help="comma-separated schema names to include (default: all non-system)")
    ap.add_argument("--top", type=int, default=200, help="markdown: max tables listed by row count")
    ap.add_argument("--json", action="store_true", help="write config/catalog/discovered/<db>.json instead")
    ap.add_argument("--samples", action="store_true", help="with --json: read distinct values of short text columns")
    ap.add_argument("--sample-max-rows", type=int, default=200_000, help="only sample tables up to this many rows")
    ap.add_argument("--tables", default=None, help="with --samples: only these tables (comma-separated schema.name)")
    ap.add_argument("--tables-from-logs", action="store_true",
                    help="with --samples: only the raw tables schema selection showed or generated SQL read "
                         "(logs/schema_select.csv, logs/sql.csv)")
    a = ap.parse_args()

    from ragbot.config import env   # loads .env; os.environ alone does not see it
    cn = pyodbc.connect(env(a.conn_env), timeout=30, autocommit=False, readonly=True)
    try:
        cur = cn.cursor()
        schemas = a.schemas.split(",") if a.schemas else None
        if a.json:
            write_json(cn, cur, a, schemas)
        else:
            write_markdown(cur, a, schemas)
    finally:
        cn.rollback()
        cn.close()


if __name__ == "__main__":
    main()
