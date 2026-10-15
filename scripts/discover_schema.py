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

Every sampled value is pasted into the SQL prompts that show its table and embedded in the schema index, so
sampling is fenced (src/ragbot/data/discovery.py):
- a people database is refused before anything connects: its catalog declares its own `sensitive_patterns:`
  or `restricted_to:`, or no catalog describes it. --allow-people-samples overrides the refusal, but a catalog
  with `sensitive_patterns:` still drops every sample when it loads (data/catalog.py).
- a column named for a person or a login (CreatedBy, UserName, EmpName, LoginID) is never sampled.
- --count-only prints how many sampling queries a run would make, per table and in total, from the discovery
  file already on disk and the logs. It opens no connection and reads no database.
- --max-queries N (default 150): a run with more candidate columns than N stops before its first sampling
  query and writes nothing.

Usage:
  python scripts/discover_schema.py <ConnEnvVarName> <FriendlyName> [--schemas dbo,PPM]
  python scripts/discover_schema.py SQLSERVER_CONN_BITOPISPLINT BitopiSplint --json [--samples]
  python scripts/discover_schema.py SQLSERVER_CONN_BITOPISPLINT BitopiSplint --count-only --tables-from-logs
  python scripts/discover_schema.py SQLSERVER_CONN_BITOPISPLINT BitopiSplint --json --samples --tables-from-logs
  python scripts/discover_schema.py SQLSERVER_CONN_PRODUCTION HR --database HR --json
      (--database: another database on the same server, through the same read-only login; the catalog
       YAML then says `connection_database: HR` — see docs/DATA_ACCESS.md)
"""
import argparse, json, sys, _path  # noqa: F401
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ragbot.data.catalog import Catalog

ROOT = Path(__file__).resolve().parents[1]
DISCOVERED = ROOT / "config" / "catalog" / "discovered"
DEFAULT_MAX_QUERIES = 150


def _q(ident: str) -> str:
    """Bracket-quote an identifier that came from sys.* (never from user input)."""
    return "[" + ident.replace("]", "]]") + "]"


def _schema_filter(schemas):
    return "" if not schemas else "AND s.name IN (" + ",".join(f"'{s}'" for s in schemas) + ")"


def _discovered_file(friendly: str) -> Path:
    return DISCOVERED / f"{friendly}.json"


def _catalog(friendly: str) -> Optional["Catalog"]:
    """The loaded catalog whose `database:` is `friendly`, or None (the discovery file is named after it)."""
    from ragbot.data.catalog import load_catalogs
    return load_catalogs().get(friendly)


def _only_tables(a: argparse.Namespace, cat: Optional["Catalog"]) -> Optional[set[str]]:
    """The lower-case schema.name tables a sampling run is limited to, or None for every small table."""
    if a.tables:
        return {t.strip().lower() for t in a.tables.split(",") if t.strip()}
    if a.tables_from_logs:
        from ragbot.data.schema_usage import used_tables
        only = {n.lower() for n in used_tables(cat)} if cat else set()
        print(f"tables from the logs ({len(only)}): {', '.join(sorted(only)) or 'none'}")
        return only
    return None


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


def write_json(cn, cur, a: argparse.Namespace, schemas: Optional[list[str]], cat: Optional["Catalog"] = None) -> None:
    """The discovery file. `cat` is the database's catalog (main() loads it for --samples): the logs are read
    through it for --tables-from-logs. Raises SystemExit(2), before any sampling query and without writing,
    when the candidates exceed --max-queries."""
    from ragbot.data.discovery import assemble, budget_refused, sample_candidates
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

    f = _discovered_file(a.friendly)
    dest = f.parent
    from ragbot.data.discovery import carried_samples
    previous = carried_samples(json.loads(f.read_text(encoding="utf-8")) if f.exists() else None)
    samples = {}
    if a.samples:
        only = _only_tables(a, cat)
        cands = sample_candidates(tables, columns, a.sample_max_rows, only=only)
        print(f"sampling {len(cands)} short text columns of tables with <= {a.sample_max_rows:,} rows"
              + (f" in {len(only)} selected table(s)" if only is not None else ""))
        over = budget_refused(len(cands), a.max_queries)
        if over:
            print(f"refused: {over}. No column was sampled and nothing was written.", file=sys.stderr)
            raise SystemExit(2)
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


def count_only(a: argparse.Namespace) -> int:
    """--count-only: the sampling queries a --samples run with the same options would make, per table and in
    total, worked out from the discovery file already on disk (and the logs for --tables-from-logs) with the
    live candidate rule. Opens no connection. Tables, columns and row counts are those of the file's own run:
    refresh it with --json (sys.* only) first when the schema may have moved since."""
    from ragbot.data.discovery import budget_refused, candidates_from_doc, queries_by_table, samples_refused
    f = _discovered_file(a.friendly)
    if not f.exists():
        print(f"no discovery file {f}: run with --json (sys.* only) first", file=sys.stderr)
        return 1
    doc: dict[str, Any] = json.loads(f.read_text(encoding="utf-8"))
    cat = _catalog(a.friendly)
    only = _only_tables(a, cat)
    cands = candidates_from_doc(doc, only, a.sample_max_rows)
    per = queries_by_table(cands)
    for name, n in per:
        print(f"  {name}: {n}")
    print(f"{a.friendly}: {len(cands)} sampling queries over {len(per)} table(s) "
          f"(from {f.name}, generated {doc.get('generated_at', '?')}; no database was queried)")
    why = samples_refused(cat)
    if why:
        print(f"a --samples run would be refused: {why} (--allow-people-samples overrides)")
    over = budget_refused(len(cands), a.max_queries)
    if over:
        print(f"a --samples run would be refused: {over}")
    elif not why:
        print(f"within --max-queries {a.max_queries}")
    return 0


def _connect(a: argparse.Namespace):
    import pyodbc
    from ragbot.data.connectors import dsn   # loads .env; os.environ alone does not see it
    return pyodbc.connect(dsn(a.conn_env, a.database), timeout=30, autocommit=False, readonly=True)


def _args(argv: Optional[list[str]] = None) -> argparse.Namespace:
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
    ap.add_argument("--count-only", action="store_true",
                    help="print the sampling queries a --samples run would make, from the discovery file on disk "
                         "and the logs; connects to nothing")
    ap.add_argument("--max-queries", type=int, default=DEFAULT_MAX_QUERIES,
                    help="with --samples: refuse to start sampling when more columns than this would be read")
    ap.add_argument("--allow-people-samples", action="store_true",
                    help="with --samples: sample a database whose catalog declares sensitive_patterns or "
                         "restricted_to, or that no catalog describes (refused by default)")
    ap.add_argument("--database", default=None,
                    help="open this database instead of the one named in the connection string (same server, same login)")
    return ap.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    a = _args(argv)
    if a.count_only:
        return count_only(a)
    cat = None
    if a.samples:
        from ragbot.data.discovery import samples_refused
        cat = _catalog(a.friendly)
        why = samples_refused(cat)
        if why and not a.allow_people_samples:
            print(f"refused: {why}. Nothing was read; --allow-people-samples overrides.", file=sys.stderr)
            return 2
        if why:
            print(f"--allow-people-samples: sampling anyway ({why})"
                  + ("; its catalog drops these values when it loads" if cat is not None
                     and cat.sensitive_patterns is not None else ""), file=sys.stderr)
    cn = _connect(a)
    try:
        cur = cn.cursor()
        schemas = a.schemas.split(",") if a.schemas else None
        if a.json:
            write_json(cn, cur, a, schemas, cat)
        else:
            write_markdown(cur, a, schemas)
    finally:
        cn.rollback()
        cn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
