"""The next page of a query (Phase T4): "show the next 200 rows" after a list that stopped at its row limit.

Every statement runs under a row cap (data.max_rows, 200), so a long list used to end with "there may be more" and
no way to see the rest. A page is the same statement with its TOP / LIMIT replaced by OFFSET … FETCH NEXT (T-SQL) or
LIMIT … OFFSET (MySQL), under the statement's own ORDER BY — or, when it has none, its key columns, because a page
without a fixed order could repeat or skip rows. The rewrite is done on sqlglot's tree of the outermost query, never
on the text, so a CTE statement pages its final SELECT.

Only this module asks the guard for an OFFSET (data/guard.py `max_offset`); a model-written statement never may.
"""
from __future__ import annotations

from typing import Sequence

import sqlglot
from sqlglot import exp

from .guard import GuardError


def page_sql(sql: str, dialect: str, page: int, page_size: int, key_columns: Sequence[str] = ()) -> str:
    """`sql` (one SELECT) as page `page` (0 = the first) of `page_size` rows."""
    if page < 0 or page_size <= 0:
        raise GuardError("a page needs a whole page number and a positive size")
    read = "tsql" if dialect == "tsql" else "mysql"
    try:
        tree = sqlglot.parse_one(sql, read=read)
    except Exception as e:
        raise GuardError(f"cannot parse the statement to page it: {e}") from e
    if not isinstance(tree, exp.Query):
        raise GuardError("only a query can be paged")
    if not tree.args.get("order"):
        outputs = {(e.alias_or_name or "").lower() for e in tree.selects} if isinstance(tree, exp.Select) else set()
        keys = [k for k in key_columns if k.lower() in outputs]
        if not keys:
            raise GuardError("this list has no fixed order, so its next page cannot be read reliably")
        tree = tree.order_by(*(exp.column(k, quoted=True) for k in keys), copy=False)
    tree.set("limit", None)
    tree.set("offset", None)
    tree = tree.offset(page * page_size, copy=False).limit(page_size, copy=False)
    return tree.sql(dialect=read)
