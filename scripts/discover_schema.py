"""Read-only metadata discovery: writes docs/schema/<db>.md with tables, columns, views and row
counts for a database. Never reads data rows — sys.* / information_schema only. Used once per
database while writing its config/catalog/<db>.yaml, not part of the app's runtime path.
Usage: python scripts/discover_schema.py <ConnEnvVarName> <FriendlyName> [--schemas dbo,PPM]
"""
import argparse, os, _path  # noqa: F401
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    import pyodbc
    ap = argparse.ArgumentParser()
    ap.add_argument("conn_env", help="env var name holding the ODBC connection string")
    ap.add_argument("friendly", help="short name used for the output file, e.g. BitopiSplint")
    ap.add_argument("--schemas", default=None, help="comma-separated schema names to include (default: all non-system)")
    ap.add_argument("--top", type=int, default=200, help="max tables listed by row count")
    a = ap.parse_args()

    conn_str = os.environ[a.conn_env]
    cn = pyodbc.connect(conn_str, timeout=30, autocommit=False, readonly=True)
    cur = cn.cursor()
    schemas = a.schemas.split(",") if a.schemas else None
    schema_filter = "" if not schemas else "AND s.name IN (" + ",".join(f"'{s}'" for s in schemas) + ")"

    out = [f"# Schema discovery: {a.friendly} ({a.conn_env})\n", "Read-only metadata only — no data rows.\n"]

    out.append("## Tables by row count\n")
    for r in cur.execute(f"""
            SELECT TOP {a.top} s.name, t.name, SUM(p.rows)
            FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id
            JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
            WHERE 1=1 {schema_filter}
            GROUP BY s.name, t.name ORDER BY SUM(p.rows) DESC"""):
        out.append(f"- `{r[0]}.{r[1]}` — {r[2]:,} rows")

    out.append("\n## Views\n")
    for r in cur.execute(f"""
            SELECT s.name, v.name FROM sys.views v JOIN sys.schemas s ON s.schema_id = v.schema_id
            WHERE 1=1 {schema_filter} ORDER BY s.name, v.name"""):
        out.append(f"- `{r[0]}.{r[1]}`")

    out.append("\n## Columns of interest (order/PCD/file/style/buyer/ship)\n")
    for r in cur.execute(f"""
            SELECT s.name, o.name, o.type, c.name, ty.name
            FROM sys.columns c JOIN sys.objects o ON o.object_id = c.object_id
            JOIN sys.schemas s ON s.schema_id = o.schema_id JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            WHERE o.type IN ('U', 'V') {schema_filter} AND (
                c.name LIKE '%PCD%' OR c.name LIKE '%FileRef%' OR c.name LIKE '%Style%' OR c.name LIKE '%Buyer%'
                OR c.name LIKE '%Ship%' OR c.name LIKE '%Order%' OR c.name LIKE '%Factory%' OR c.name LIKE '%Meeting%')
            ORDER BY o.name, c.column_id"""):
        out.append(f"- `{r[0]}.{r[1]}.{r[3]}` ({r[2].strip()}) : {r[4]}")

    cn.rollback()
    cn.close()
    dest = ROOT / "docs" / "schema"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{a.friendly}.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"wrote {dest / f'{a.friendly}.md'} ({len(out)} lines)")


if __name__ == "__main__":
    main()
