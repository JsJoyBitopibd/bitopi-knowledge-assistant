"""The scope object (PRD section 7): the set of values a user may see, applied as filters everywhere.

Documents carry factory / department / confidentiality / buyer_code; a chunk is visible when all four are
inside the user's scope (auth/filters.py). Database rows are filtered on each view's factory column
(data/virtual.py). `*` means every value.
"""
from __future__ import annotations

import re
from typing import Iterable, Literal, Optional

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
    # Phase S: the groups the user holds, lower-cased, as the only thing that can open a catalog which names
    # `restricted_to:` (data/catalog.py). It is the user's groups and not a set of database names because a
    # group grants and never takes away — the rule config/scopes.yaml states — and because the roadmap's
    # `databases` cap with intersection could only deny the HR databases to KA-Managers by listing every
    # other database on that group and keeping the list in step with each new one. {"*"} = every group.
    groups: frozenset[str] = frozenset()

    @field_validator("factories", mode="before")
    @classmethod
    def _factory_codes(cls, v):
        # Codes end up as literals in SQL (data/virtual.py), so only short upper-case codes pass.
        codes = frozenset(str(x).strip().upper() for x in v)
        bad = sorted(c for c in codes if not _CODE.match(c))
        if bad:
            raise ValueError(f"invalid factory code(s): {bad}")
        return codes

    @field_validator("groups", mode="before")
    @classmethod
    def _group_names(cls, v):
        return frozenset(str(g).strip().lower() for g in (v or ()) if str(g).strip())

    @classmethod
    def unrestricted(cls) -> "Scope":
        """Everything: scripts, the eval harness and scheduled jobs. Never a signed-in user's default."""
        return cls(factories=frozenset({ALL}), departments=frozenset({ALL}), confidentiality_max="restricted",
                   groups=frozenset({ALL}))

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

    def allows_database(self, restricted_to: Iterable[str] = ()) -> bool:
        """Whether this scope may read a catalog. A catalog that names no group is open to everyone, as every
        catalog was before Phase S; one that names groups is readable only by a user who holds one of them.
        Being an admin does not grant it: the admin flag shows the timings and the audit panel, and making it
        a master key would hand every administrator the payroll in the same change."""
        named = {str(g).strip().lower() for g in (restricted_to or ()) if str(g).strip()}
        return not named or ALL in self.groups or bool(named & self.groups)

    def key(self) -> str:
        """Canonical, order-independent text: part of every cache key and of every audit row. The groups are in
        it so that an answer cached for someone who may read a restricted database is never served to someone
        who may not (Phase S)."""
        b = ALL if self.buyer_codes is None else ",".join(sorted(self.buyer_codes))
        return (f"f={','.join(sorted(self.factories))};d={','.join(sorted(self.departments))};"
                f"c={self.confidentiality_max};b={b};a={int(self.is_admin)};"
                f"g={','.join(sorted(self.groups))}")


class User(BaseModel):
    name: str                       # the account name, lower case (sAMAccountName for AD)
    display: str = ""
    groups: list[str] = []
    scope: Scope
