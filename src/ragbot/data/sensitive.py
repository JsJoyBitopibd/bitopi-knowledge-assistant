"""Which tables and columns count as sensitive (personal, HR, credential data).

One definition shared by schema discovery (no sample values are ever read from a sensitive column)
and, from Phase C4, the SQL guard (a generated query may not reference one). Override the list with
`data.sensitive_patterns` in config/settings.yaml.

Matching is on the words of the name, so short patterns do not fire inside unrelated identifiers:
`EmpNID`, `NID_No` and `DOB` are sensitive, `ManID` and `TownID` are not. A name is split into
words at camelCase and non-alphanumeric boundaries; a pattern of 5+ letters matches anywhere in
the lower-cased name (catches all-lowercase names such as `empmobile`), a shorter one (nid, dob,
pwd, bank, wage) must be a whole word or its plural, so `Doberman` is not a date of birth.
Deliberately conservative: `IPAddress` and `BankName` are flagged too.
"""
from __future__ import annotations

import re

DEFAULT_PATTERNS = ["salar", "wage", "bank", "nid", "passport", "password", "pwd", "token", "secret",
                    "blood", "religion", "phone", "mobile", "email", "address", "dob", "birth",
                    # added with the HR/payroll databases (Phase J): health records and pay components
                    "medical", "tax", "bonus", "increment", "pay"]


def patterns() -> tuple[str, ...]:
    from ..config import settings
    try:
        got = settings().get("data.sensitive_patterns")
    except Exception:
        got = None
    return tuple(p.lower() for p in (got or DEFAULT_PATTERNS))


def words(name: str) -> list[str]:
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return [w.lower() for w in re.split(r"[^A-Za-z0-9]+", s) if w]


def is_sensitive(name: str, pats: tuple[str, ...] | None = None) -> bool:
    """True if a column/table name matches any sensitive pattern."""
    pats = pats or patterns()
    low = (name or "").lower()
    ws = words(name)
    for p in pats:
        if len(p) >= 5 and p in low:
            return True
        if any(w.startswith(p) if len(p) >= 5 else w in (p, p + "s") for w in ws):
            return True
    return False
