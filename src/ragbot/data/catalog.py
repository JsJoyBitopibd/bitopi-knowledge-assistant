"""Semantic layer: load config/catalog/*.yaml and render them for the SQL-generation prompt.

A view MAY carry a `definition:` field — a T-SQL SELECT over the real tables that the model NEVER sees.
When present, tools.rewrite_virtual() wraps the guarded SQL as `WITH rag_<name> AS (<definition>) …`,
so the same SQL runs whether the DBA has created the real rag.* views yet or not (docs/DATA_ACCESS.md).

Two tiers (docs/ROADMAP.md C2):
- curated views (the YAML): business descriptions, always rendered into the prompt — the gold tier.
- discovered tables (config/catalog/discovered/<database>.json, from scripts/discover_schema.py --json):
  every table and view of the database. Hundreds of them cannot all go in a prompt, so render()
  includes only the ones a question selected (schema index, Phase C3). Tables listed under
  `exclude_tables:` in the YAML and tables whose name is sensitive are never offered; sensitive
  columns are never rendered.
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from ..config import settings

MAX_RENDERED_COLUMNS = 40


@dataclass
class View:
    name: str
    grain: str
    description: str
    key_columns: list[str]
    columns: dict[str, str]
    definition: str | None = None  # T-SQL SELECT; excluded from render(); wrapped as CTE at execution
    scope_column: str | None = None  # column with the factory short name; per-user row scope filters on it


@dataclass
class Table:
    """A discovered table or view (config/catalog/discovered/<db>.json)."""
    name: str                        # schema.name, e.g. dbo.ExportOrder
    kind: str = "table"              # table | view
    rows: Optional[int] = None
    description: str = ""
    primary_key: list[str] = field(default_factory=list)
    columns: list[dict[str, Any]] = field(default_factory=list)   # name, type, nullable, description, sensitive, samples?
    foreign_keys: list[dict[str, Any]] = field(default_factory=list)  # columns, ref_table, ref_columns
    sensitive: bool = False

    @property
    def sensitive_columns(self) -> set[str]:
        return {c["name"].lower() for c in self.columns if c.get("sensitive")}

    def render(self) -> str:
        """Compact prompt form: header, key/FK lines, then at most MAX_RENDERED_COLUMNS non-sensitive columns."""
        head = f"- {self.name}" + (f" ({self.rows:,} rows)" if self.rows is not None else f" ({self.kind})")
        if self.description:
            head += f" — {self.description}"
        out = [head]
        if self.primary_key:
            out.append(f"    Key: {', '.join(self.primary_key)}")
        for fk in self.foreign_keys:
            out.append(f"    Joins: ({', '.join(fk['columns'])}) -> {fk['ref_table']}({', '.join(fk['ref_columns'])})")
        shown = [c for c in self.columns if not c.get("sensitive")]
        for c in shown[:MAX_RENDERED_COLUMNS]:
            line = f"    {c['name']}: {c['type']}"
            if c.get("description"):
                line += f" — {c['description']}"
            if c.get("samples"):
                line += f" (values: {', '.join(c['samples'][:15])})"
            out.append(line)
        if len(shown) > MAX_RENDERED_COLUMNS:
            out.append(f"    … {len(shown) - MAX_RENDERED_COLUMNS} more columns not shown")
        return "\n".join(out)


@dataclass
class Catalog:
    database: str
    dialect: str                     # "tsql" | "mysql"
    connection_env: str
    rules: list[str]
    views: list[View]
    examples: list[dict[str, str]] = field(default_factory=list)
    description: str = ""            # one-line, shown to the router when multiple catalogs are loaded
    keywords: list[str] = field(default_factory=list)  # words that hint at this database (routing fallback)
    tables: list[Table] = field(default_factory=list)          # discovered tier (may be empty)
    exclude_tables: list[str] = field(default_factory=list)    # fnmatch patterns, e.g. "dbo.*Back", "HR.*"

    @property
    def engine(self) -> str:
        return "sqlserver" if self.dialect == "tsql" else "mysql"

    @property
    def view_names(self) -> set[str]:
        return {v.name.lower() for v in self.views}

    def view(self, name: str) -> View | None:
        return next((v for v in self.views if v.name.lower() == name.lower()), None)

    def excluded(self, name: str) -> bool:
        low = name.lower()
        return any(fnmatch.fnmatchcase(low, p.lower()) for p in self.exclude_tables)

    @property
    def offered_tables(self) -> list[Table]:
        """Discovered tables the SQL model may be shown: not excluded, not sensitive by name, and not
        an empty base table (0 rows can answer nothing; views have no row count and stay)."""
        return [t for t in self.tables if not t.sensitive and not self.excluded(t.name)
                and not (t.kind == "table" and t.rows == 0)]

    @property
    def allowed_names(self) -> set[str]:
        """Everything a query may reference (lower-case): curated views ∪ offered discovered tables.
        The guard's allow-list from Phase C4 — the full set, not just what one prompt showed."""
        return self.view_names | {t.name.lower() for t in self.offered_tables}

    def table(self, name: str) -> Table | None:
        low = name.lower()
        return next((t for t in self.tables if t.name.lower() == low), None)

    def render_selected(self, selected: Iterable[str], join_hints: Iterable[str] = ()) -> str:
        """The per-question part of the SQL prompt: the discovered tables a question selected (offered
        ones only) and likely join conditions. Kept apart from render() so the long static part of the
        prompt stays identical across questions (provider-side prompt caching)."""
        offered = {t.name.lower(): t for t in self.offered_tables}
        picked = [offered[n.lower()] for n in dict.fromkeys(selected) if n.lower() in offered]
        if not picked:
            return ""
        out = [f"=== Other tables in {self.database} (raw tables: reference as schema.name; "
               f"prefer the rag.* views when they cover the question) ==="]
        out += [t.render() for t in picked]
        hints = list(join_hints)
        if hints:
            out.append("Likely joins (same key name, no declared foreign key):")
            out += [f"    {h}" for h in hints]
        return "\n".join(out)

    def render(self) -> str:
        """Business-language text placed in the SQL prompt: rules, curated views, examples. Never
        emits `definition:`. Discovered tables are rendered separately (render_selected)."""
        today = date.today().isoformat()
        out = [f"Database: {self.database} ({self.dialect})"]
        if self.description:
            out.append(f"Purpose: {self.description}")
        out.append("Rules:")
        out += [f"- {r.replace('{today}', today)}" for r in self.rules]
        out.append("Views:")
        for v in self.views:
            out.append(f"- {v.name} — {v.description} Grain: {v.grain}. Keys: {', '.join(v.key_columns)}")
            out += [f"    {c}: {d}" for c, d in v.columns.items()]
        if self.examples:
            out.append("Examples:")
            for ex in self.examples:
                out.append(f"Q: {ex['question']}\nSQL:\n{ex['sql'].strip()}")
        return "\n".join(out)


def load_catalogs(folder: Path | None = None) -> dict[str, Catalog]:
    """{database name: Catalog}. Only *.yaml (not *.example.yaml) are loaded. Definitions are validated.

    Cached on the folder's file mtimes: parsing the YAML and re-validating every definition with
    sqlglot used to run on every data question. Editing a catalog file invalidates the cache.
    """
    folder = folder or settings().path("catalog_dir")
    files = [*folder.glob("*.yaml"), *(folder / "discovered").glob("*.json")]
    stamp = tuple(sorted((str(f.relative_to(folder)), f.stat().st_mtime_ns) for f in files))
    return dict(_load_catalogs(folder, stamp))   # copy: callers must not mutate the cached mapping


def _load_discovered(folder: Path, database: str) -> list[Table]:
    """config/catalog/discovered/<database>.json, or [] when discovery has not been run."""
    f = folder / "discovered" / f"{database}.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text(encoding="utf-8"))
    return [Table(t["name"], t.get("kind", "table"), t.get("rows"), t.get("description", ""), t.get("primary_key", []),
                  t.get("columns", []), t.get("foreign_keys", []), bool(t.get("sensitive"))) for t in d.get("tables", [])]


@lru_cache(maxsize=4)
def _load_catalogs(folder: Path, _stamp: tuple) -> dict[str, Catalog]:
    from .virtual import validate_definition  # lazy: avoids a cycle at import time
    cats: dict[str, Catalog] = {}
    for f in sorted(folder.glob("*.yaml")):
        if f.name.endswith(".example.yaml"):
            continue
        d: dict[str, Any] = yaml.safe_load(f.read_text(encoding="utf-8"))
        views: list[View] = []
        for v in d.get("views", []):
            defn = v.get("definition")
            if defn:
                try:
                    validate_definition(defn, d["dialect"])
                except Exception as e:
                    raise RuntimeError(f"{f.name} view {v['name']} has invalid definition: {e}") from e
            scope = v.get("scope_column")
            if scope and scope not in v.get("columns", {}):
                # a scope filter on a column the view does not have would fail every restricted query
                raise RuntimeError(f"{f.name} view {v['name']}: scope_column {scope!r} is not one of its columns")
            views.append(View(v["name"], v.get("grain", ""), v.get("description", ""), v.get("key_columns", []),
                              v.get("columns", {}), definition=defn, scope_column=scope))
        cats[d["database"]] = Catalog(d["database"], d["dialect"], d["connection_env"], d.get("rules", []),
                                      views, d.get("examples", []), d.get("description", ""), d.get("keywords", []),
                                      tables=_load_discovered(folder, d["database"]),
                                      exclude_tables=d.get("exclude_tables", []))
    return cats
