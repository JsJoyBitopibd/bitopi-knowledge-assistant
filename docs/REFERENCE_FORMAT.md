# Reference format (what the user sees under every answer)

Every factual sentence in an answer ends with one or more markers: `[P1]`, `[P2]` for PDF
sources, `[D1]`, `[D2]` for database sources. Below the answer, a **References** list resolves
each marker. Markers are numbered in the order sources were given to the model, not in the
order they appear in the text.

## PDF reference `[P#]`

```
[P1]  SOP-PPM-014 PCD Approval v3.pdf
      Topic: 3.2 Change after fabric in-house · Page 4 · Category: SOP
      "A change to the planned cut date after fabric in-house requires written approval
       from the Planning Head."
```

Fields (all required unless noted):

| Field | Source | Notes |
|---|---|---|
| File name | chunk metadata `source` | exactly as on disk |
| Topic | chunk metadata `section` (heading prefix) | fall back to the document title if the page had no heading; for tables use the caption |
| Page | chunk metadata `page` | 1-based |
| Category | chunk metadata `category` | the `data/pdfs/<subfolder>` name |
| Quote | first ≤ 300 characters of the chunk text after the heading prefix | trimmed at a sentence boundary |
| Revision status | `superseded` flag | show "(superseded by …)" only when an old version was explicitly requested |

In the Streamlit UI the file name is a link that opens the PDF at that page (`file://` for the
pilot; a viewer URL later).

## Database reference `[D#]`

```
[D1]  Production.PPM (SQL Server) · rag.vw_ExportOrderPCD
      Row key: FileRefID = 4471 (EO 22-0918) · 1 row · as of 22 Sep 2026 09:41
      Tool: pcd_by_eo (fixed)            ▸ show SQL
```

| Field | Source | Notes |
|---|---|---|
| Database + engine | catalog `database`, `dialect` | e.g. `Production.PPM (SQL Server)`, `inventory (MySQL)` |
| Table/view | the view(s) referenced by the SQL | list all when more than one |
| Row key(s) | key columns of the returned rows | up to 5 keys, then "and N more" |
| Row count | len(rows) | |
| As-of | execution timestamp | local time |
| Tool | fixed tool name, or `generated` | |
| SQL | the exact statement executed | collapsed by default; parameters shown separately |

## Not found

When no source answers the question the reply is exactly:

```
System doesn't have the data.
```

optionally followed by one sentence such as *"This may be in the buyer's original PO, which is
not in the document library."* No markers, no references list.

## Rules the verifier enforces (`agent/citations.py`)

1. Every marker in the text maps to a source that was in the prompt.
2. Every number, date, or code (regex for `\d`, ISO dates, `[A-Z]{2,}-?\d+`) in the answer appears
   verbatim in at least one cited source's text or row values (numbers compared after removing
   thousands separators).
3. A factual sentence (contains a number, a date, a code, or a proper noun from the sources) with
   no marker → regenerate with the stricter instruction in `prompts/system_answer_strict.txt`.
4. Two failures → return the not-found reply and log the case to `logs/verify_failures.csv`.

## JSON shape returned by the orchestrator

```json
{
  "text": "The current PCD for EO 22-0918 is 12 Oct 2026 [D1]. …",
  "not_found": false,
  "references": [
    {"marker": "D1", "kind": "data", "database": "Production.PPM", "engine": "sqlserver",
     "views": ["rag.vw_ExportOrderPCD"], "row_keys": ["FileRefID=4471"], "row_count": 1,
     "as_of": "2026-09-22T09:41:00", "tool": "pcd_by_eo", "sql": "SELECT …", "params": {"eo": "22-0918"}},
    {"marker": "P1", "kind": "pdf", "source": "SOP-PPM-014 PCD Approval v3.pdf", "title": "SOP-PPM-014 PCD Approval v3",
     "section": "3.2 Change after fabric in-house", "page": 4, "category": "SOP",
     "quote": "A change to the planned cut date …", "chunk_id": "SOP-PPM-014 PCD Approval v3#p4#c2"}
  ],
  "usage": {"input_tokens": 4120, "output_tokens": 96, "model": "…"}
}
```
