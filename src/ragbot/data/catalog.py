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
import re
import time
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from ..config import settings
from .sensitive import PEOPLE_TABLE, is_sensitive

MAX_RENDERED_COLUMNS = 40


# Phase Q. One vocabulary for what a column holds, so a statement can be checked before it runs
# (data/typefix.py). Raw tables get theirs from the discovered SQL type; a curated view declares the ones that
# matter in its YAML (`column_kinds:`) — a column whose values look like numbers but are stored as text.
KINDS = ("text", "int", "decimal", "date", "datetime", "time", "bool", "binary")
_SQL_TYPE_KIND = (
    (("nvarchar", "varchar", "nchar", "char", "text", "ntext", "uniqueidentifier", "xml", "sysname"), "text"),
    (("bigint", "smallint", "tinyint", "int"), "int"),
    (("decimal", "numeric", "float", "real", "smallmoney", "money"), "decimal"),
    (("datetimeoffset", "datetime2", "smalldatetime", "datetime", "timestamp"), "datetime"),
    (("date",), "date"), (("time",), "time"), (("bit",), "bool"),
    (("varbinary", "binary", "image"), "binary"),
)


def kind_of_sql_type(sql_type: str) -> str | None:
    """'nvarchar(255)' -> 'text', 'int' -> 'int', 'datetime2' -> 'datetime'; None for anything unrecognised."""
    t = (sql_type or "").split("(")[0].strip().lower()
    for names, kind in _SQL_TYPE_KIND:
        if t in names:
            return kind
    return None


@dataclass
class View:
    name: str
    grain: str
    description: str
    key_columns: list[str]
    columns: dict[str, str]
    definition: str | None = None  # T-SQL SELECT; excluded from render(); wrapped as CTE at execution
    scope_column: str | None = None  # column with the factory short name; per-user row scope filters on it
    column_kinds: dict[str, str] = field(default_factory=dict)   # lower-case column -> KINDS (Phase Q)


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

    def render(self, max_columns: int | None = None) -> str:
        """Compact prompt form: header, key/FK lines, then at most `max_columns` non-sensitive columns
        (default MAX_RENDERED_COLUMNS). A catalog may raise it (`max_rendered_columns:`, Phase R): once a
        database's personal columns are open, a wide table like the 184-column monthly salary sheet would
        otherwise lose the dimension columns a question needs."""
        cap = max_columns or MAX_RENDERED_COLUMNS
        head = f"- {self.name}" + (f" ({self.rows:,} rows)" if self.rows is not None else f" ({self.kind})")
        if self.description:
            head += f" — {self.description}"
        out = [head]
        if self.primary_key:
            out.append(f"    Key: {', '.join(self.primary_key)}")
        for fk in self.foreign_keys:
            out.append(f"    Joins: ({', '.join(fk['columns'])}) -> {fk['ref_table']}({', '.join(fk['ref_columns'])})")
        shown = [c for c in self.columns if not c.get("sensitive")]
        for c in shown[:cap]:
            line = f"    {c['name']}: {c['type']}"
            if c.get("description"):
                line += f" — {c['description']}"
            if c.get("samples"):
                line += f" (values: {', '.join(c['samples'][:15])})"
            out.append(line)
        if len(shown) > cap:
            out.append(f"    … {len(shown) - cap} more columns not shown")
        return "\n".join(out)


@dataclass
class Hint:
    """A code column that holds another table's key where discovery found no foreign key (catalog YAML
    `hints:`), e.g. `Buyer` = `dbo.Contact_Master.ContactID` ('C/09/7' -> ContactName 'H&M'). Listed per
    table, because the same column name means different things in different tables."""
    column: str
    target: str                      # schema.table.column
    tables: list[str]
    note: str = ""

    @property
    def target_table(self) -> str:
        return self.target.rsplit(".", 1)[0]


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
    hints: list[Hint] = field(default_factory=list)            # code columns -> the table that names them
    # The database to open on `connection_env`'s server when it is not the one in the connection string:
    # one read-only login serves the whole instance, so its catalogs share one env var (connectors.dsn).
    connection_database: str | None = None
    # A database that holds one factory's data only (e.g. RHL's own VistaQ server): the router prefers it when a
    # question names that factory and keeps it out when the question names another one (data/db_router.py).
    factory: str | None = None
    label: str = ""                  # the name users know the system by ("VistaQ"), for plain-language replies
    # Phase P1. `synonyms:` {phrase: [phrase, ...]}: words staff use that the catalog's keywords, table and column
    # names do not ("closing balance" -> "closing stock"); appended to the question before the router and the
    # schema index score it. `table_notes:` {schema.table: text}: a one-line description for a discovered table
    # whose JSON description is empty (most of them) — merged into Table.description at load, so the SQL prompt
    # and the schema index both see it. `schema_rag_k:` tables selected per question for this database (default
    # data.schema_rag_k): a catalog with no curated views and no foreign keys needs more candidates.
    synonyms: dict[str, list[str]] = field(default_factory=dict)
    table_notes: dict[str, str] = field(default_factory=dict)
    schema_rag_k: int | None = None
    # Phase Q. `routable: false`: a catalog that is loaded (a pasted statement may read it, db_ping and
    # check_catalog cover it) but never ranked for a question — a prototype or a retired copy whose keywords
    # would otherwise compete with the live system's.
    routable: bool = True
    # Phase R. `sensitive_patterns:` replaces the global list (data/sensitive.py) for THIS database only.
    # The HR databases are answered with `[password, pwd, token, secret]`: the owner opened their business and
    # personal columns on 2026-10-13 and only credentials stay blocked. None = the global default, unchanged.
    sensitive_patterns: tuple[str, ...] | None = None
    max_rendered_columns: int | None = None   # Phase R: raise the 40-column prompt cap for a wide database
    # Phase S. `restricted_to:` the groups whose members may read this database; empty (the default) means
    # open to everyone, which is how every catalog behaved before. The check is auth/models.py
    # Scope.allows_database, and the filter runs before the router, so a user who may not read a database
    # never has its tables put in front of the model, never matches one of its fixed tools, and cannot reach
    # it through a cached statement. Nothing names a group yet: with auth.provider 'none' every visitor is
    # the one open user, so a line here would deny the HR questions to everybody. Turning it on is two
    # changes together — `restricted_to: [KA-HR]` on the three people catalogs and a real sign-in provider.
    restricted_to: tuple[str, ...] = ()

    @property
    def sensitive_pats(self) -> tuple[str, ...]:
        """The patterns that decide what this database hides: its own, or the global default."""
        from .sensitive import patterns
        return self.sensitive_patterns if self.sensitive_patterns is not None else patterns()

    @property
    def display_name(self) -> str:
        return self.label or self.database

    def expand(self, question: str) -> str:
        """The question plus the synonyms of every phrase of `synonyms:` it contains as whole words."""
        if not self.synonyms:
            return question
        padded = " " + re.sub(r"[^a-z0-9&]+", " ", question.lower()).strip() + " "
        extra = [s for phrase, syns in self.synonyms.items()
                 if " " + re.sub(r"[^a-z0-9&]+", " ", phrase.lower()).strip() + " " in padded for s in syns]
        return question + (" " + " ".join(dict.fromkeys(extra)) if extra else "")

    def hints_for(self, table: str) -> list[Hint]:
        low = table.lower()
        return [h for h in self.hints if low in {t.lower() for t in h.tables}]

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
        an empty base table (0 rows can answer nothing; views have no row count and stay).
        Memoised on the tables list: with twenty catalogs (~4,300 tables, ~25 patterns) this is read
        several times per question by the router, the guard and the schema index."""
        stamp = (id(self.tables), len(self.tables), tuple(self.exclude_tables), self.sensitive_patterns)
        memo = self.__dict__.get("_offered")
        if memo is None or memo[0] != stamp:
            offered = [t for t in self.tables if not t.sensitive and not self.excluded(t.name)
                       and not (t.kind == "table" and t.rows == 0)]
            self.__dict__["_offered"] = memo = (stamp, offered)
        return list(memo[1])

    @property
    def allowed_names(self) -> set[str]:
        """Everything a query may reference (lower-case): curated views ∪ offered discovered tables.
        The guard's allow-list from Phase C4 — the full set, not just what one prompt showed."""
        return self.view_names | {t.name.lower() for t in self.offered_tables}

    def table(self, name: str) -> Table | None:
        low = name.lower()
        return next((t for t in self.tables if t.name.lower() == low), None)

    def column_kinds_of(self, obj: str) -> dict[str, str]:
        """What each column of one view or table holds (Phase Q): a view's declared `column_kinds:`, or a
        discovered table's columns mapped from their SQL types. {} when the object is unknown here."""
        v = self.view(obj)
        if v is not None:
            return dict(v.column_kinds)
        t = self.table(obj)
        if t is None:
            return {}
        out = {}
        for c in t.columns:
            kind = kind_of_sql_type(str(c.get("type", "")))
            if kind:
                out[c["name"]] = kind
        return out

    def column_kind(self, obj: str, column: str) -> str | None:
        """The kind of one column of one view or table, or None when nothing records it."""
        if not obj or not column:
            return None
        low = column.lower()
        return next((k for c, k in self.column_kinds_of(obj).items() if c.lower() == low), None)

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
        out += [t.render(self.max_rendered_columns) for t in picked]
        hints = list(join_hints)
        if hints:
            out.append("Likely joins (no declared foreign key):")
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
        out.append("Views:" if self.views else "Views: none — use the tables listed for the question as schema.name.")
        for v in self.views:
            out.append(f"- {v.name} — {v.description} Grain: {v.grain}. Keys: {', '.join(v.key_columns)}")
            out += [f"    {c}: {d}" for c, d in v.columns.items()]
        if self.examples:
            out.append("Examples:")
            for ex in self.examples:
                out.append(f"Q: {ex['question']}\nSQL:\n{ex['sql'].strip()}")
        return "\n".join(out)


_STAMP: dict[Path, tuple[float, tuple]] = {}
_STAMP_SECONDS = 5.0     # an edited or added catalog is seen within this long


def catalog_stamp(folder: Path | None = None) -> tuple:
    """The catalog folder's files and mtimes: the cache key of load_catalogs(), and part of the
    question->SQL cache key (tools.py), so an edited catalog invalidates both. Re-read at most every
    _STAMP_SECONDS: ~36 stats on a Docker bind mount cost tens of milliseconds per question."""
    folder = folder or settings().path("catalog_dir")
    now = time.monotonic()
    hit = _STAMP.get(folder)
    if hit and now - hit[0] < _STAMP_SECONDS:
        return hit[1]
    files = [*folder.glob("*.yaml"), *(folder / "discovered").glob("*.json")]
    stamp = tuple(sorted((str(f.relative_to(folder)), f.stat().st_mtime_ns) for f in files))
    _STAMP[folder] = (now, stamp)
    return stamp


def load_catalogs(folder: Path | None = None) -> dict[str, Catalog]:
    """{database name: Catalog}. Only *.yaml (not *.example.yaml) are loaded. Definitions are validated.

    Cached on the folder's file mtimes: parsing the YAML and re-validating every definition with
    sqlglot used to run on every data question. Editing a catalog file invalidates the cache.
    """
    folder = folder or settings().path("catalog_dir")
    return dict(_load_catalogs(folder, catalog_stamp(folder)))   # copy: callers must not mutate the cached mapping


def denied_databases(scope, cats: dict[str, Catalog] | None = None) -> frozenset[str]:
    """Phase S4: the databases `scope` may not read — every catalog whose `restricted_to:` names no group the
    scope holds. The one place the value index, the fixed tools, the not-found chips and the UI ask, so a
    restriction means the same thing everywhere. `None` (scripts, the eval) is denied nothing.

    Catalogs are read through this module's `load_catalogs` at call time, so a test or `scope_check.py`
    that arms `restricted_to` in memory by patching it reaches every caller at once. A load failure raises:
    each caller decides whether that fails closed."""
    if scope is None:
        return frozenset()
    cats = load_catalogs() if cats is None else cats
    return frozenset(name for name, c in cats.items() if not scope.allows_database(c.restricted_to))


def readable_databases(scope, cats: dict[str, Catalog] | None = None) -> list[str]:
    """The databases `scope` may read, by name — what the sidebar counts (Phase S4: a restricted database is not
    announced to someone it will never answer)."""
    cats = load_catalogs() if cats is None else cats
    return sorted(set(cats) - denied_databases(scope, cats))


def _load_discovered(folder: Path, database: str, pats: tuple[str, ...] | None = None) -> list[Table]:
    """config/catalog/discovered/<database>.json, or [] when discovery has not been run.

    The JSON records what the GLOBAL rule says, decided once when discovery ran. A catalog that overrides the
    rule (`sensitive_patterns:`, Phase R) has every table and column re-judged here, so the stored flags stay
    truthful about the default while this database follows its own list — and a later discover_schema.py run
    cannot silently re-close it. Everything downstream (offered_tables, Table.render, the schema index, the
    router, the DENY generator) reads these flags, so nothing else has to know about the override.

    Such a catalog also loses every sampled value (G2). Its opened columns are names, gender, designation and
    the rest; Table.render pastes samples into the SQL prompt and the schema index embeds them, so a people
    database's values must never be in a Table — whatever a `discover_schema.py --allow-people-samples` run
    wrote into the file. The file itself is left as written.
    """
    f = folder / "discovered" / f"{database}.json"
    if not f.exists():
        return []
    d = json.loads(f.read_text(encoding="utf-8"))
    out: list[Table] = []
    for t in d.get("tables", []):
        cols, sensitive = t.get("columns", []), bool(t.get("sensitive"))
        if pats is not None:
            name = t["name"].split(".")[-1]
            sensitive = is_sensitive(name, pats)
            cols = [{**{k: v for k, v in c.items() if k != "samples"},
                     "sensitive": is_sensitive(c.get("name", ""), pats)} for c in cols]
        else:
            # every catalog: a person or login column's values, and any value of a people table (an employee
            # master in the ERP), never reach a prompt, even from a file sampled before these rules
            # (discovery.sample_skipped, sensitive.PEOPLE_TABLE)
            from .discovery import sample_skipped, samples_look_personal
            people = bool(PEOPLE_TABLE.search(t["name"].split(".")[-1]))
            cols = [{k: v for k, v in c.items() if k != "samples"}
                    if c.get("samples") and (people or sample_skipped(c.get("name", ""))
                                             or samples_look_personal(c["samples"])) else c for c in cols]
        out.append(Table(t["name"], t.get("kind", "table"), t.get("rows"), t.get("description", ""),
                         t.get("primary_key", []), cols, t.get("foreign_keys", []), sensitive))
    return out


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
            kinds = {str(c): str(k).lower() for c, k in (v.get("column_kinds") or {}).items()}
            for col, kind in kinds.items():
                if col not in v.get("columns", {}):
                    raise RuntimeError(f"{f.name} view {v['name']}: column_kinds names {col!r}, not one of its columns")
                if kind not in KINDS:
                    raise RuntimeError(f"{f.name} view {v['name']}: column_kinds {col}: {kind!r} is not one of {KINDS}")
            views.append(View(v["name"], v.get("grain", ""), v.get("description", ""), v.get("key_columns", []),
                              v.get("columns", {}), definition=defn, scope_column=scope, column_kinds=kinds))
        hints = []
        for h in d.get("hints") or []:
            if len(str(h.get("target", "")).split(".")) != 3 or not h.get("column") or not h.get("tables"):
                raise RuntimeError(f"{f.name}: a hint needs column, target (schema.table.column) and tables: {h}")
            hints.append(Hint(h["column"], h["target"], list(h["tables"]), h.get("note", "")))
        synonyms = d.get("synonyms") or {}
        if not isinstance(synonyms, dict) or any(not isinstance(v, list) or not all(isinstance(s, str) for s in v)
                                                 for v in synonyms.values()):
            raise RuntimeError(f"{f.name}: synonyms must map a phrase to a list of phrases")
        notes = {str(k).lower(): str(v).strip() for k, v in (d.get("table_notes") or {}).items()}
        # Phase R: this database's own sensitive-name list, if it declares one. Read explicitly and checked —
        # an unknown key is silently ignored here, and a typo in this one would open a database by accident.
        pats: tuple[str, ...] | None = None
        if "sensitive_patterns" in d:
            raw = d.get("sensitive_patterns")
            if not isinstance(raw, list) or not raw or not all(isinstance(p, str) and p.strip() for p in raw):
                raise RuntimeError(f"{f.name}: sensitive_patterns must be a non-empty list of words")
            pats = tuple(p.strip().lower() for p in raw)
        # Phase S: the groups that may read this database. Checked the same way and for the same reason — a
        # typo in a key that decides access must fail loudly, not leave the database open without anyone noticing.
        limited: tuple[str, ...] = ()
        if "restricted_to" in d:
            raw_groups = d.get("restricted_to")
            if not isinstance(raw_groups, list) or not raw_groups \
                    or not all(isinstance(g, str) and g.strip() for g in raw_groups):
                raise RuntimeError(f"{f.name}: restricted_to must be a non-empty list of group names")
            limited = tuple(g.strip().lower() for g in raw_groups)
        tables = _load_discovered(folder, d["database"], pats)
        for t in tables:
            if not t.description and t.name.lower() in notes:
                t.description = notes[t.name.lower()]
        cats[d["database"]] = Catalog(d["database"], d["dialect"], d["connection_env"], d.get("rules", []),
                                      views, d.get("examples", []), d.get("description", ""), d.get("keywords", []),
                                      tables=tables,
                                      exclude_tables=d.get("exclude_tables", []), hints=hints,
                                      connection_database=d.get("connection_database") or None,
                                      factory=str(d["factory"]).strip().upper() if d.get("factory") else None,
                                      label=str(d.get("label") or "").strip(),
                                      synonyms={str(k): [str(s) for s in v] for k, v in synonyms.items()},
                                      table_notes=notes,
                                      schema_rag_k=int(d["schema_rag_k"]) if d.get("schema_rag_k") else None,
                                      routable=bool(d.get("routable", True)), sensitive_patterns=pats,
                                      max_rendered_columns=int(d["max_rendered_columns"])
                                      if d.get("max_rendered_columns") else None,
                                      restricted_to=limited)
    return cats
