"""Shared data types. Everything that crosses a module boundary is one of these."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Chunk(BaseModel):
    """One retrievable piece of a PDF (a section, paragraph run, or a table)."""
    id: str                      # "<file stem>#p<page>#c<n>"  or "#t<n>" for tables
    text: str                    # stored text INCLUDING the "<title> › <section>" prefix
    source: str                  # file name on disk, e.g. "SOP-PPM-014 PCD Approval v3.pdf"
    title: str                   # file stem
    page: int                    # 1-based
    section: str = ""            # heading / table caption
    kind: Literal["text", "table"] = "text"
    category: str = "General"    # first-level subfolder under data/pdfs
    doc_hash: str = ""
    superseded: bool = False
    embed_model: str = ""
    ingested_at: str = ""
    score: float = 0.0           # filled by retrieval

    def metadata(self) -> dict[str, Any]:
        return {
            "source": self.source, "title": self.title, "page": self.page, "section": self.section,
            "kind": self.kind, "category": self.category, "doc_hash": self.doc_hash,
            "superseded": self.superseded, "embed_model": self.embed_model, "ingested_at": self.ingested_at,
        }

    @property
    def body(self) -> str:
        """Chunk text without the heading prefix line (used for quotes)."""
        first, _, rest = self.text.partition("\n")
        return rest if "›" in first and rest else self.text


class QueryResult(BaseModel):
    """Rows returned by a data tool, with everything a [D#] reference needs."""
    database: str
    engine: Literal["sqlserver", "mysql"]
    views: list[str]
    sql: str
    params: dict[str, Any] = Field(default_factory=dict)
    tool: str = "generated"      # fixed tool name or "generated"
    sql_executed: str = ""       # what actually ran (virtual views expanded); `sql` is what the user sees
    columns: list[str]
    rows: list[list[Any]]
    key_columns: list[str] = Field(default_factory=list)
    as_of: datetime = Field(default_factory=datetime.now)
    error: Optional[str] = None

    @property
    def row_keys(self) -> list[str]:
        if not self.key_columns:
            return []
        idx = [self.columns.index(k) for k in self.key_columns if k in self.columns]
        if not idx:
            return []   # an aggregate (GROUP BY / COUNT) returns no key columns: nothing to show, not "; ;"
        keys = []
        for r in self.rows[:5]:
            keys.append(", ".join(f"{self.columns[i]}={r[i]}" for i in idx))
        if len(self.rows) > 5:
            keys.append(f"and {len(self.rows) - 5} more")
        return keys


class Reference(BaseModel):
    marker: str                          # "P1" / "D1"
    kind: Literal["pdf", "data"]
    # pdf
    source: Optional[str] = None
    title: Optional[str] = None
    section: Optional[str] = None
    page: Optional[int] = None
    category: Optional[str] = None
    quote: Optional[str] = None
    chunk_id: Optional[str] = None
    superseded: Optional[bool] = None
    # data
    database: Optional[str] = None
    engine: Optional[str] = None
    views: Optional[list[str]] = None
    row_keys: Optional[list[str]] = None
    row_count: Optional[int] = None
    as_of: Optional[datetime] = None
    tool: Optional[str] = None
    sql: Optional[str] = None
    params: Optional[dict[str, Any]] = None


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    calls: int = 0


class Answer(BaseModel):
    text: str
    not_found: bool = False
    references: list[Reference] = Field(default_factory=list)
    route: str = ""
    question: str = ""
    rewritten_question: str = ""
    usage: Usage = Field(default_factory=Usage)
    warnings: list[str] = Field(default_factory=list)
    sources_text: str = ""       # the <sources> block sent to the model (for eval judging; never logged)
    error_kind: str = ""         # "" on success; else quota|timeout|auth|db|other — UI shows a soft message
    results: list[QueryResult] = Field(default_factory=list)   # database results in [D1], [D2] … order (UI table/CSV)
