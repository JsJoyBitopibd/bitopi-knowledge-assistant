"""AD groups -> scope (PRD FR-4.1), from config/scopes.yaml (copy scopes.example.yaml).

A user's scope is the union over the configured groups they belong to, so joining a group can only add
access: factories are granted explicitly; a group without `departments:` allows every department; the
highest confidentiality wins. A user in none of the configured groups is not enrolled and cannot sign in.
Changing who sees what is a config change only (FR-6.4).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from ..config import settings
from .models import ALL, LEVELS, Scope


def load_scopes(path: Optional[Path] = None) -> dict[str, Any]:
    path = path or settings().path("scopes_file", "config/scopes.yaml")
    if not path.exists():
        return {"groups": {}, "admins": []}
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"groups": d.get("groups") or {}, "admins": d.get("admins") or []}


def scope_for_groups(groups: Iterable[str], cfg: dict[str, Any]) -> Optional[Scope]:
    """The union of the scopes of the configured groups among `groups` (case-insensitive), or None when
    none of them is configured (not enrolled)."""
    have = {g.lower() for g in groups}
    matched = [spec or {} for name, spec in cfg["groups"].items() if name.lower() in have]
    if not matched:
        return None
    factories: set[str] = set()
    departments: set[str] = set()
    level = 0
    buyers: Optional[set[str]] = set()
    for spec in matched:
        factories |= {str(f) for f in spec.get("factories") or []}
        departments |= {str(d) for d in spec.get("departments") or [ALL]}
        level = max(level, LEVELS.index(spec.get("confidentiality", "internal")))
        if buyers is not None:
            codes = spec.get("buyer_codes")
            buyers = None if codes is None else buyers | {str(c) for c in codes}
    admins = {a.lower() for a in cfg.get("admins") or []}
    return Scope(factories=frozenset(factories), departments=frozenset(departments),
                 confidentiality_max=LEVELS[level], buyer_codes=None if buyers is None else frozenset(buyers),
                 is_admin=bool(admins & have))
