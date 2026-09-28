"""Scope -> document filter (PRD FR-4.3). The single rule every search path applies: a chunk is visible
when its factory, department, confidentiality and buyer are all inside the user's scope. retrieve()
hands the result to both the vector query and the keyword index, so an out-of-scope chunk is never a
candidate — it cannot influence ranking, reranking or the prompt."""
from __future__ import annotations

from .models import COMMON_DEPARTMENT, GROUP_WIDE_FACTORY, Scope

# Chunk / registry fields the scope filter owns. A caller's `where` may not set them (retriever.py).
SCOPE_FIELDS = ("factory", "department", "confidentiality", "buyer_code")


def scope_where(scope: Scope) -> dict[str, list[str]]:
    """{field: allowed values} for the fields this scope restricts; {} for an unrestricted scope."""
    where: dict[str, list[str]] = {}
    if not scope.all_factories:
        where["factory"] = sorted({*scope.factories, GROUP_WIDE_FACTORY})
    if not scope.all_departments:
        where["department"] = sorted({*scope.departments, COMMON_DEPARTMENT})
    if scope.confidentiality_max != "restricted":
        where["confidentiality"] = scope.allowed_levels()
    if scope.buyer_codes is not None:
        where["buyer_code"] = sorted({"", *scope.buyer_codes})
    return where
