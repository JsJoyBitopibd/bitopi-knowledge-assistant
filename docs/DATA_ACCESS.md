# Database access rules and the semantic layer

## 1. One read-only login per engine, one schema of views

**SQL Server** (run as a DBA on each database the assistant may read):

```sql
CREATE LOGIN rag_reader WITH PASSWORD = '<strong password>';
USE [Production.PPM];
CREATE USER rag_reader FOR LOGIN rag_reader;
CREATE SCHEMA rag AUTHORIZATION dbo;
GRANT SELECT ON SCHEMA::rag TO rag_reader;
-- no other grants: no dbo, no EXECUTE, no linked servers, no cross-database chaining

CREATE VIEW rag.vw_ExportOrderPCD AS
SELECT  eo.FileRefID,
        eo.ExportOrderNo   AS EONo,
        eo.BuyerName       AS Buyer,
        eo.StyleNo,
        eo.OrderQty,
        eo.PCD             AS PlannedCutDate,
        eo.ShipDate,
        eo.FactoryCode     AS Factory,
        pm.MeetingDate     AS LastPPMMeeting,
        pm.Status          AS PPMStatus
FROM    dbo.ExportOrder eo
LEFT JOIN dbo.PPMMeetings pm ON pm.FileRefID = eo.FileRefID;
```

Column names above are illustrative; map them to the real tables. Views should rename columns
to plain business words and hide anything the assistant must never show (prices, contacts).

**MySQL**:

```sql
CREATE USER 'rag_reader'@'%' IDENTIFIED BY '<strong password>';
CREATE DATABASE rag;
CREATE VIEW rag.vw_fabric_stock AS SELECT … FROM inventory.stock …;
GRANT SELECT ON rag.* TO 'rag_reader'@'%';
```

Connection strings go in `.env` (`SQLSERVER_CONN`, `MYSQL_CONN`). Connections are opened with
a 10-second command timeout and read-uncommitted / read-committed-snapshot isolation.

## 2. The catalog (semantic layer) — `config/catalog/<db>.yaml`

The catalog is the only description of the database the model ever sees. Write it in business
language. One file per database:

```yaml
database: Production.PPM
dialect: tsql            # tsql | mysql
connection_env: SQLSERVER_CONN
rules:
  - Use TOP (n), never LIMIT. Dates as half-open ranges: col >= @from AND col < @to.
  - Today is {today}. "This week" means Monday to next Monday.
  - Never join to objects outside schema rag.
views:
  - name: rag.vw_ExportOrderPCD
    grain: one row per export order; FileRefID is unique
    description: Export orders with planned cut date (PCD) and latest PPM meeting status.
    key_columns: [FileRefID, EONo]
    columns:
      FileRefID: internal order key; joins every other view
      EONo: export order number as buyers quote it, e.g. 22-0918
      Buyer: buyer name
      StyleNo: style number, e.g. TS-4471
      PlannedCutDate: PCD; date cutting is planned to start
      ShipDate: planned shipment date
      Factory: TAL, RHL or BGL
      PPMStatus: Pending / Approved / Rescheduled
examples:
  - question: How many RHL orders have PCD next week?
    sql: |
      SELECT COUNT(*) FROM rag.vw_ExportOrderPCD
      WHERE Factory = 'RHL' AND PlannedCutDate >= @nextMon AND PlannedCutDate < @nextMon2
```

`key_columns` drive the "Row key" line in `[D#]` references.

## 3. Fixed tools — `config/fixed_tools.yaml`

Hand-written, parameterised SQL for frequent questions; no model-generated SQL. Matched by
regex on the (rewritten) question; parameters extracted by regex or asked back.

```yaml
- name: pcd_by_eo
  database: Production.PPM
  match: '\bEO\s*(\d{2}-\d{4})\b.*\b(PCD|cut date)\b|\b(PCD|cut date)\b.*\bEO\s*(\d{2}-\d{4})\b'
  params:
    eo: { type: string, from_group: [1, 4] }
  sql: |
    SELECT TOP (1) FileRefID, EONo, StyleNo, PlannedCutDate, ShipDate, Factory, PPMStatus
    FROM rag.vw_ExportOrderPCD WHERE EONo = @eo
```

## 4. The guard (`src/ragbot/data/guard.py`) — every generated statement must pass

- Exactly one statement; it must be `SELECT` or `WITH … SELECT`.
- Deny list (case-insensitive, word-boundary): INSERT UPDATE DELETE MERGE DROP ALTER CREATE
  TRUNCATE EXEC EXECUTE GRANT REVOKE DENY BACKUP RESTORE OPENROWSET OPENQUERY OPENDATASOURCE
  BULK xp_ sp_ INTO OUTFILE LOAD_FILE LOAD DATA INFORMATION_SCHEMA sys. mysql. performance_schema
  `--` `/*` `;`
- Every table/view referenced must be in the catalog's `views` list (parse with `sqlglot`).
- Add `TOP (200)` / `LIMIT 200` when absent. Reject `SELECT *` on more than one view.
- Timeout 10 s; on error return the message to the model for one retry, then give up.
- Log every statement (user, tool/generated, SQL, params, rows, ms) to `logs/sql.csv`.

## 5. What the assistant tells the user about data

- Which database and engine, which view(s), which row keys, how many rows, when it was read,
  and the SQL on request. Never the connection string, never column lists beyond the catalog.
- If a question needs a table that is not in the catalog: "System doesn't have the data." plus
  "This may be in <system>; ask IT to add a view."

## 6. Virtual views (this build, until a DBA runs the DDL above)

No DDL has been run against `192.168.10.6\MSSQLSERVER2019` — writes to the server are strictly
prohibited for this build. `config/catalog/bitopisplint.yaml` and `config/catalog/production.yaml`
therefore give every view an extra `definition:` field: a plain T-SQL `SELECT` over the real tables
(`dbo.ExportOrder`, `dbo.FileRef`, `PPM.PPMMeetings`, …) that the model never sees — only the
business-language `columns:`/`description:` block is rendered into the SQL-generation prompt.

At execution (`src/ragbot/data/virtual.py`), the guarded SQL is wrapped textually:

```sql
WITH rag_vw_ExportOrder AS (
  <definition from the catalog YAML>
)
SELECT TOP (200) ExportOrderID, PCD FROM rag_vw_ExportOrder WHERE Factory = 'TAL'
```

`sqlglot` is used only to **validate and locate** — never to regenerate — so the SQL a user is shown
(`Reference.sql`) stays byte-identical to what the model or fixed tool wrote; `sql_executed` on the
`QueryResult` carries the expanded statement that actually ran. Every definition is validated at
catalog-load time (`virtual.validate_definition`): no parameters, no `TOP`/`LIMIT` (a view must not
silently truncate), no nested `WITH`, no reference back into `rag.*`, and every table two-part
(`schema.table`) with a schema other than `rag`.

`scripts/gen_rag_views.py` renders the same `definition:` blocks into
`docs/schema/rag_views_<db>.sql` — the `CREATE SCHEMA rag` / `CREATE VIEW` / `CREATE LOGIN rag_reader`
script in §1, ready for a DBA to run. Once that happens, delete the `definition:` field from a view's
YAML entry and the identical SQL runs directly against the real `rag.*` view — no other code changes.

Every connection still opens with `autocommit=False`, and the connector issues an explicit
`ROLLBACK` in a `finally` block before closing, whether the query succeeded or failed
(`src/ragbot/data/connectors.py`) — belt and braces alongside the guard, since the `sa` login used
during this build has no server-side write restriction of its own.
