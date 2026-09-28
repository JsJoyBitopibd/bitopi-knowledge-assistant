"""The scope object (PRD section 7): the set of values a user may see, applied as filters everywhere.

Documents carry factory / department / confidentiality / buyer_code; a chunk is visible when all four are
inside the user's scope (auth/filters.py). Database rows are filtered on each view's factory column
(data/virtual.py). `*` means every value.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

LEVELS: tuple[str, ...] = ("public", "internal", "restricted")
ALL = "*"
GROUP_WIDE_FACTORY = "ALL"         # a document for every factory (SOPs, IT policy)
COMMON_DEPARTMENT = "Common"       # a document for every department
_CODE = re.compile(r"^(\*|[A-Z0-9]{1,8})$")


class Scope(BaseModel):
    model_config = ConfigDict(frozen=True)

    factories: frozenset[str]                                   # factory short names; {"*"} = every factory
    departments: frozenset[str] = frozenset({ALL})
    confidentiality_max: Literal["public", "internal", "restricted"] = "internal"
    buyer_codes: Optional[frozenset[str]] = None                # None = every buyer (staff)
    is_admin: bool = False

    @field_validator("factories", mode="before")
    @classmethod
    def _factory_codes(cls, v):
        # Codes end up as literals in SQL (data/virtual.py), so only short upper-case codes pass.
        codes = frozenset(str(x).strip().upper() for x in v)
        bad = sorted(c for c in codes if not _CODE.match(c))
        if bad:
            raise ValueError(f"invalid factory code(s): {bad}")
        return codes

    @classmethod
    def unrestricted(cls) -> "Scope":
        """Everything: scripts, the eval harness and scheduled jobs. Never a signed-in user's default."""
        return cls(factories=frozenset({ALL}), departments=frozenset({ALL}), confidentiality_max="restricted")

    @property
    def all_factories(self) -> bool:
        return ALL in self.factories

    @property
    def all_departments(self) -> bool:
        return ALL in self.departments

    def db_factories(self) -> list[str]:
        """Factory codes for row filters (the group-wide document tag ALL is not a factory in the data)."""
        return sorted(f for f in self.factories if f not in (ALL, GROUP_WIDE_FACTORY))

    def allowed_levels(self) -> list[str]:
        return list(LEVELS[: LEVELS.index(self.confidentiality_max) + 1])

    def allows_factory(self, code: str) -> bool:
        return self.all_factories or str(code).strip().upper() in self.factories

    def key(self) -> str:
        """Canonical, order-independent text: part of every cache key and of every audit row."""
        b = ALL if self.buyer_codes is None else ",".join(sorted(self.buyer_codes))
        return (f"f={','.join(sorted(self.factories))};d={','.join(sorted(self.departments))};"
                f"c={self.confidentiality_max};b={b};a={int(self.is_admin)}")


class User(BaseModel):
    name: str                       # the account name, lower case (sAMAccountName for AD)
    display: str = ""
    groups: list[str] = []
    scope: Scope
