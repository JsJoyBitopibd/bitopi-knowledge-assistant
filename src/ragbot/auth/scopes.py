"""AD groups -> scope (PRD FR-4.1), from config/scopes.yaml (copy scopes.example.yaml).

A user's scope is the union over the configured groups they belong to, so joining a group can only add
access: factories are granted explicitly; a group without `departments:` allows every department; the
highest confidentiality wins. A user in none of the configured groups is not enrolled and cannot sign in.
Changing who sees what is a config change only (FR-6.4).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from ..config import settings
from .models import ALL, LEVELS, Scope

# Table names that hold people data (Phase S4): what an open catalog must not keep offering once the people
# databases are restricted. Broad on purpose — a false hit is a table the owner looks at once and excludes or not.
_PEOPLE_TABLE = re.compile(r"employee|attendance|attendence|punch|payroll|salar|pims|leave(?!r)|staff", re.I)


def load_scopes(path: Optional[Path] = None) -> dict[str, Any]:
    path = path or settings().path("scopes_file", "config/scopes.yaml")
    if not path.exists():
        return {"groups": {}, "admins": []}
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {"groups": d.get("groups") or {}, "admins": d.get("admins") or []}


def scope_for_groups(groups: Iterable[str], cfg: dict[str, Any]) -> Optional[Scope]:
    """The union of the scopes of the configured groups among `groups` (case-insensitive), or None when
    none of them is configured (not enrolled).

    The scope carries the configured groups the user holds (Phase S): they are what opens a catalog that
    names `restricted_to:`. Until 2026-10-15 they were left out, so every signed-in user had no groups and an
    armed restriction would have denied its own members. Only configured groups go in — an AD account
    carries dozens ("Domain Users" …) that would split every cache key per person and decide nothing,
    because config/scopes.yaml stays the one place that says who sees what."""
    have = {g.lower() for g in groups}
    names = [name for name in cfg["groups"] if name.lower() in have]
    matched = [cfg["groups"][name] or {} for name in names]
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
                 is_admin=bool(admins & have), groups=frozenset(names))


def restriction_problems(cats: dict[str, Any], cfg: dict[str, Any], provider: str, open_groups: Iterable[str],
                         users: dict[str, Any], agents: Optional[dict[str, Any]] = None) -> tuple[list[str], list[str]]:
    """Phase S4: (problems, warnings) about the per-database rule as configured — what scripts/check_catalog.py
    prints. A problem is a setting that would deny the wrong people: a `restricted_to:` group that
    config/scopes.yaml does not configure (nobody could read the database), a restriction while sign-in is off
    and no open group holds it (it would deny everyone — the half-armed S3), or local sign-in with no account
    holding the group. A warning is a state worth saying out loud: not armed, or an agent that speaks for both an
    open and a restricted database (its charter may describe the restricted one to everybody)."""
    provider = str(provider or "local").strip().lower()      # as providers.get_provider reads it
    configured = {str(g).lower() for g in cfg.get("groups") or {}}
    opened = {str(g).lower() for g in open_groups}
    held = {str(g).lower() for spec in (users or {}).values() for g in ((spec or {}).get("groups") or [])}
    problems: list[str] = []
    warnings: list[str] = []
    restricted = {name: tuple(str(g).lower() for g in c.restricted_to) for name, c in cats.items() if c.restricted_to}
    for name, groups in sorted(restricted.items()):
        unknown = sorted(set(groups) - configured)
        if unknown:
            problems.append(f"restricted_to {name}: group(s) {', '.join(unknown)} not configured in config/scopes.yaml"
                            " — nobody could read this database")
        if provider == "none" and not set(groups) & opened:
            problems.append(f"restricted_to {name}: auth.provider is none and no open group holds "
                            f"{', '.join(groups)} — this denies everyone; arm it together with sign-in (S3)")
        if provider == "local" and not set(groups) & held:
            problems.append(f"restricted_to {name}: no account in config/users.yaml holds {', '.join(groups)}"
                            " — nobody can read this database")
    if not restricted:
        warnings.append("not armed: no catalog names restricted_to (Phase S3) — everyone who reaches the app can "
                        "read every database, the people databases included")
    # The rule is per database, but people data is not only in the people databases: the ERP, the dashboard and
    # the production databases offer employee masters and attendance of their own (found by the S4 review). Once
    # armed, any of those still offered by an open catalog is a problem — exclude it (`exclude_tables:`) or
    # restrict that catalog too. Unarmed, they are listed so the owner sees the size of the job.
    # Unarmed, the people databases are the ones that opened themselves per catalog (Phase R, sensitive_patterns:)
    # and would be the ones restricted; armed, they are the restricted ones.
    closing = set(restricted) or {n for n, c in cats.items() if getattr(c, "sensitive_patterns", None)}
    open_people = {name: sorted(t.name for t in getattr(c, "offered_tables", []) if _PEOPLE_TABLE.search(t.name))
                   for name, c in cats.items() if name not in closing}
    open_people = {n: ts for n, ts in open_people.items() if ts}
    if restricted:
        for name, ts in sorted(open_people.items()):
            problems.append(f"{name} offers {len(ts)} table(s) that hold people data ({', '.join(ts[:4])}"
                            f"{', …' if len(ts) > 4 else ''}) to users the people databases are closed to — "
                            "exclude them (exclude_tables:) or restrict this catalog too")
    elif open_people:
        by_size = sorted(open_people.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        warnings.append(f"if armed: {len(open_people)} other catalogs still offer "
                        f"{sum(len(ts) for ts in open_people.values())} tables that hold people data ("
                        + ", ".join(f"{n} {len(ts)}" for n, ts in by_size)
                        + ") — each must be excluded or its catalog restricted before S3")
    for a in (agents or {}).values():
        own = [n for n in getattr(a, "catalogs", ()) if n in cats]
        if any(n in restricted for n in own) and any(n not in restricted for n in own):
            warnings.append(f"agent {a.name}: speaks for open and restricted databases alike — its charter reaches "
                            "users who may read only the open ones")
    return problems, warnings
