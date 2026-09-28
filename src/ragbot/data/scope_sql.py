"""Row scope in SQL (PRD FR-4.4): a rag view returns only the rows of the factories a user may see.

Each catalog view declares `scope_column:` (the column holding the factory short name: TAL, RHL, ...).
The server-side form below is what the DBA's real rag.* views contain (scripts/gen_rag_views.py): the
view reads the caller's factories from SESSION_CONTEXT, which the connector sets on every connection.

    EXEC sp_set_session_context @key = N'rag_factories', @value = N'TAL,RHL';   -- or N'*' for all

No session value means no rows (fail closed). CHARINDEX over a comma list is used instead of OPENJSON or
STRING_SPLIT because those need database compatibility level 130+, which older ERP databases often lack.
"""
from __future__ import annotations

SESSION_KEY = "rag_factories"


def session_filtered(definition: str, column: str) -> str:
    """The body of a server-side rag view: `definition`'s rows whose `column` is one of the factories in
    the session context, or all rows when the session value is '*'."""
    ctx = f"CAST(SESSION_CONTEXT(N'{SESSION_KEY}') AS nvarchar(4000))"
    return (f"SELECT * FROM (\n{definition.strip().rstrip(';')}\n) AS v\n"
            f"WHERE {ctx} = N'*'\n"
            f"   OR CHARINDEX(N',' + v.[{column}] + N',', N',' + {ctx} + N',') > 0")
