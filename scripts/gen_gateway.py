"""Write the DBA scripts for a read-only gateway: a linked server on another SQL Server (e.g. the dev server) that
reads the live server, and the rag.* views over it in a database of their own (docs/DATA_ACCESS.md, "Gateway on the
dev server"; src/ragbot/data/gateway.py). Also a speed check: the fixed tools' queries timed through the gateway and
directly on the live server, to compare before the assistant is pointed at the gateway.

This script only WRITES files — it never connects to a database. Output goes to private/gateway/ (git-ignored: it
names the live server and its tables). Passwords stay sqlcmd variables.

Usage: python scripts/gen_gateway.py --live <live host> [--link BITOPI_LIVE] [--gateway-db RAG_LIVE]
                                     [--link-login rag_link] [--reader rag_reader] [--out private/gateway]
"""
import argparse, _path  # noqa: F401
from pathlib import Path

from ragbot.config import settings
from ragbot.data import gateway as gw
from ragbot.data.catalog import load_catalogs
from ragbot.data.tools import load_fixed_tools, match_fixed_tool

ROOT = Path(__file__).resolve().parents[1]


def cases(tools, cats):
    """(tool, the parameters its example question produces) — as scripts/check_catalog.py --live finds them."""
    out = []
    for tool in tools:
        hit = match_fixed_tool(tool.get("example", ""), tools) if tool.get("example") else None
        params = hit[1] if hit and hit[0]["name"] == tool["name"] else tool.get("example_params")
        if params is not None and tool["database"] in cats:
            out.append((tool, params))
    return out


def readme(a, n_views: int, tables: dict, n_tools: int, limits: tuple[int, int]) -> str:
    per_db = "; ".join(f"{db} {len(t)}" for db, t in tables.items())
    return f"""# DBA request: a read-only gateway to the live SQL Server

**Ask:** please set up the gateway server as a read-only window onto the live server ({a.live}), so the knowledge
assistant can read live data with its read-only login (`{a.reader}`) on the gateway, and nothing is created on the
live server except one narrow login. Then run the speed check on both servers and send back the two outputs. We
decide from those whether to point the assistant at the gateway.

## What gets created

| Server | Object | Rights |
|---|---|---|
| Live ({a.live}) | login `{a.link_login}`, users in {", ".join(tables)} | SELECT on the {sum(len(t) for t in tables.values())} tables the views read ({per_db}); sensitive columns denied; nothing else |
| Gateway | linked server `{a.link}` | data access only, no RPC; only `{a.reader}` (and you, for the test) mapped to `{a.link_login}` |
| Gateway | database `{a.gateway_db}`, schema `rag`, {n_views} views | `{a.reader}`: SELECT on schema `rag` only |

The views are the ones the assistant already uses, with the same row filter by factory (session context
`rag_factories`); they read the live tables through `[{a.link}].[database].[schema].[table]`.

## Steps (SQLCMD mode in SSMS; review each script first)

1. **Prerequisites on the gateway:** the Microsoft OLE DB Driver for SQL Server installed (provider `MSOLEDBSQL`;
   for driver version 19 set `LINK_PROVIDER` to `MSOLEDBSQL19` and add `@provstr = N'Encrypt=Optional'` in 01), and
   TCP 1433 open from the gateway to {a.live}.
2. **On the live server:** `00_live_link_login.sql` with `:setvar RAG_LINK_PASSWORD "..."`.
3. **On the gateway:** `01_linked_server.sql` with `RAG_LINK_PASSWORD`, `DBA_LOGIN` (your login) and
   `LINK_PROVIDER`. Its last query prints both servers' collations: if they are equal, run the commented
   `collation compatible` line (filters then run on the live server, which is faster).
4. **On the gateway:** `02_gateway_views.sql`.
5. **Speed check:** `03_speed_check_direct.sql` on the live server, `03_speed_check_gateway.sql` on the gateway
   ({n_tools} queries each). Discard results (Query Options > Results), then send back the Messages tab of both.
   Pass: through the gateway at most 2× the direct time, and within {limits[0]} s ({limits[1]} s for the heavy ones).
6. Remove your own login's mapping when done: `EXEC sp_droplinkedsrvlogin @rmtsrvname = N'{a.link}', @locallogin = N'<your login>';`

**Rollback:** `99_rollback_gateway.sql` on the gateway, `99_rollback_live.sql` on the live server.

## Also, separately

On the live server, `orders_past_ship_not_invoiced_by_factory` (and the watcher's "behind schedule" query) take over
30–60 s; on the dev server, under 1 s. The gateway cannot fix that: the query is slow on the live server itself.
Please run that query from `03_speed_check_direct.sql` on the live server with the actual execution plan and the
missing-index suggestions, and add the index or update the statistics it points to.
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", required=True, help="the live SQL Server the gateway reads (host or host\\instance)")
    ap.add_argument("--link", default="BITOPI_LIVE", help="linked server name on the gateway")
    ap.add_argument("--gateway-db", default="RAG_LIVE", help="database on the gateway that holds the rag views")
    ap.add_argument("--link-login", default="rag_link", help="login on the live server the link signs in with")
    ap.add_argument("--reader", default="rag_reader", help="the assistant's login on the gateway")
    ap.add_argument("--out", default="private/gateway", help="output folder (git-ignored by default)")
    a = ap.parse_args()

    cats = {n: c for n, c in load_catalogs().items() if c.dialect == "tsql"}
    view_cats = {n: c for n, c in cats.items() if any(v.definition for v in c.views)}
    if not view_cats:
        print("no T-SQL catalog with view definitions under config/catalog/"); raise SystemExit(1)
    s = settings()
    limits = (int(s.get("data.timeout_seconds", 10)), int(s.get("data.timeout_seconds_heavy", 30)))
    todo = cases(load_fixed_tools(), cats)
    tables = gw.gateway_tables(view_cats)
    n_views = sum(1 for c in view_cats.values() for v in c.views if v.definition)

    files = {
        "00_live_link_login.sql": gw.render_link_login(a.link_login, view_cats, a.live),
        "01_linked_server.sql": gw.render_linked_server(a.link, a.live, a.link_login, a.reader),
        "02_gateway_views.sql": gw.render_gateway_views(a.link, a.gateway_db, view_cats, a.reader),
        "03_speed_check_direct.sql": gw.render_speed_check(todo, cats, via_gateway=False, gateway_db=a.gateway_db,
                                                           server_label=f"LIVE server ({a.live})", limits=limits),
        "03_speed_check_gateway.sql": gw.render_speed_check(todo, cats, via_gateway=True, gateway_db=a.gateway_db,
                                                            server_label="GATEWAY server", limits=limits),
        "99_rollback_live.sql": gw.render_rollback_live(a.link_login, view_cats),
        "99_rollback_gateway.sql": gw.render_rollback_gateway(a.link, a.gateway_db),
        "README.md": readme(a, n_views, tables, len(todo), limits),
    }
    out = ROOT / a.out
    out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (out / name).write_text(text, encoding="utf-8")
        print(f"wrote {out / name}")
    print(f"{n_views} views; tables per live database: " + ", ".join(f"{db} {len(t)}" for db, t in tables.items())
          + f"; {len(todo)} fixed-tool queries in the speed check")


if __name__ == "__main__":
    main()
