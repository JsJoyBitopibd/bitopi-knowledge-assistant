"""Semantic layer: load config/catalog/*.yaml and render them for the SQL-generation prompt.

A view MAY carry a `definition:` field — a T-SQL SELECT over the real tables that the model NEVER sees.
When present, tools.rewrite_virtual() wraps the guarded SQL as `WITH rag_<name> AS (<definition>) …`,
so the same SQL runs whether the DBA has created the real rag.* views yet or not (docs/DATA_ACCESS.md).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ..config import settings


@dataclass
class View:
    name: str
    grain: str
    description: str
    key_columns: list[str]
    columns: dict[str, str]
    definition: str | None = None  # T-SQL SELECT; excluded from render(); wrapped as CTE at execution


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

    @property
    def engine(self) -> str:
        return "sqlserver" if self.dialect == "tsql" else "mysql"

    @property
    def view_names(self) -> set[str]:
        return {v.name.lower() for v in self.views}

    def view(self, name: str) -> View | None:
        return next((v for v in self.views if v.name.lower() == name.lower()), None)

    def render(self) -> str:
        """Business-language text placed in the SQL prompt. Never emits `definition:`."""
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
    """{database name: Catalog}. Only *.yaml (not *.example.yaml) are loaded. Definitions are validated."""
    from .virtual import validate_definition  # lazy: avoids a cycle at import time
    folder = folder or settings().path("catalog_dir")
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
            views.append(View(v["name"], v.get("grain", ""), v.get("description", ""), v.get("key_columns", []),
                              v.get("columns", {}), definition=defn))
        cats[d["database"]] = Catalog(d["database"], d["dialect"], d["connection_env"], d.get("rules", []),
                                      views, d.get("examples", []), d.get("description", ""), d.get("keywords", []))
    return cats
