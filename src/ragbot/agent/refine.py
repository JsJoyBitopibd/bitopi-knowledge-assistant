"""Reshape the rows of the previous answer, with no model call and no database (Phase T3).

Real follow-ups in logs/chat.csv — "line wise?", "only TAL", "sort by date", "their names", "top 5", "how many?" —
used to become brand-new questions: rewritten by a model, routed again, and often answered by the same fixed tool
with the same table (PROGRESS V6). They are reshapes of rows the app already holds. Here a follow-up is parsed
against the previous result's own columns; when the WHOLE message is refine clauses, the rows are filtered, grouped,
counted, sorted, cut or narrowed in Python and become a new, derived `QueryResult`:

- the database, views, SQL and `as_of` stay the earlier query's — no new read happened, and the reference says so;
- `transform` says what was done ("where Factory is TAL, sorted by ShipDate (latest first), first 10");
- every figure Python computes (a count, a sum) is a cell of the derived rows, so citations.verify() checks it
  exactly as it checks a database row (CLAUDE.md non-negotiable 2: "SQL or Python computes").

What the rows cannot answer is not guessed at. A result cut at its row limit is never filtered, grouped, counted or
sorted here (the missing rows would make it wrong); a column the result does not have, a regrouping of a rate (DHU
cannot be re-added from line rows), a page or a period is a re-query (`needs_requery`, Phase T4).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel, Field

from ..data.modifiers import direction_words, resolve_column
from ..data.modifiers import strip as strip_modifiers
from ..models import QueryResult

Op = Literal["eq", "neq", "in", "contains", "gt", "gte", "lt", "lte", "between"]


class Filter(BaseModel):
    column: str
    op: Op
    value: Any = None
    value2: Any = None


class RefinePlan(BaseModel):
    filters: list[Filter] = Field(default_factory=list)
    sort: Optional[tuple[str, bool]] = None      # (column, descending)
    sort_kind: str = ""                          # number | date | text: how the direction is worded
    limit: Optional[int] = None
    from_end: bool = False
    keep: list[str] = Field(default_factory=list)          # columns to show, in the result's order ([] = all)
    add: list[str] = Field(default_factory=list)           # columns asked for that the result does not have
    group_by: list[str] = Field(default_factory=list)
    scalar: Optional[Literal["count", "total"]] = None
    of_column: str = ""                          # what a count counts distinctly / what a total adds up
    distinct: str = ""                           # "their names": the distinct values of this column
    page: Optional[int] = None                   # "next", "more": the next page of the query (T4)
    window: str = ""                             # "last week?": the same query for another period (T4)
    unresolved: list[str] = Field(default_factory=list)    # words that named a column the result does not have
    regroup: list[str] = Field(default_factory=list)       # … of them, the grouping words ("style wise"): a tool may regroup
    resort: str = ""                             # … the sort word ("sort by colour"): a tool may sort by it
    # "only zara" when no column of the rows holds "zara": the rows on screen cannot say (they may be a cut, or the
    # value spelled otherwise), so the earlier query is run again with it as a filter (T4)
    unknown_filter: str = ""

    def empty(self) -> bool:
        return not (self.filters or self.sort or self.limit or self.keep or self.add or self.group_by or self.scalar
                    or self.distinct or self.page is not None or self.window or self.unresolved or self.unknown_filter)


# ---------------------------------------------------------------- columns: kinds and words
_RATE = re.compile(r"(?i)%|pct|percent|rate|dhu|avg|average|efficien|smv|sam\b|ratio|price|cost\b")
_ADDITIVE = re.compile(r"(?i)qty|quantity|pcs|pieces|output|checked|inspected|defect|reject|rectif|amount|value|"
                       r"target|manpower|count|total|short|balance|received|issued|hours")
_NAME_LIKE = re.compile(r"(?i)(name|buyer|style|supplier|item|description|merchandiser)$")
_DIMENSIONS = {          # the words staff use for a grouping, and the column names that hold it
    "factory": ("Factory", "FactoryID", "FactoryName", "Company"),
    "unit": ("Unit", "UnitName", "UnitID", "Block"),
    "line": ("Line", "LineNumber", "LineNo", "LineName"),
    "buyer": ("Buyer", "BuyerName", "Customer"),
    "style": ("Style", "StyleName", "BuyerStyleNo"),
    "colour": ("Color", "ColorName", "Colour"),
    "color": ("Color", "ColorName", "Colour"),
    "file": ("FileNo", "FileRefNo", "FileRefID", "File"),
    "po": ("ExportPONo", "PONumber", "PONo", "PO"),
    "eo": ("ExportOrderID",),
    "order": ("ExportOrderID",),
    "supplier": ("Supplier", "SupplierName"),
    "store": ("Store", "StoreName"),
    "status": ("Status", "ExportOrderStatus", "ShipmentStatus"),
    "defect": ("Defect", "DefectName", "DefectType"),
    "stage": ("Stage",),
    "merchandiser": ("Merchandiser",),
    "season": ("Season",),
}


def _is_date(v: Any) -> bool:
    return isinstance(v, (date, datetime)) or (isinstance(v, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}( .*)?", v)))


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float, Decimal)) and not isinstance(v, bool)


def kind_of(r: QueryResult, col: str) -> str:
    """number | date | text, from the column's own non-empty cells."""
    i = r.columns.index(col)
    cells = [row[i] for row in r.rows if row[i] is not None and str(row[i]).strip() != ""]
    if cells and all(_is_number(v) for v in cells):
        return "number"
    if cells and all(_is_date(v) for v in cells):
        return "date"
    return "text"


def additive(r: QueryResult, col: str) -> bool:
    """A figure that may be added up across rows: a quantity or a count, never a rate or an average."""
    return kind_of(r, col) == "number" and bool(_ADDITIVE.search(col)) and not _RATE.search(col) \
        and not re.search(r"(?i)(id|no|code|number)$", col)


def find_column(word: str, r: QueryResult) -> Optional[str]:
    """The column of the result the user's word(s) mean: the dimension names first ("line" = LineNumber), then the
    general resolver (data/modifiers.resolve_column); a plural is tried as its singular."""
    w = re.sub(r"\s+", " ", (word or "").strip().lower()).strip(" .?!")
    w = re.sub(r"^(?:the|its|their|each|every)\s+", "", w)
    if not w:
        return None
    for cand in dict.fromkeys((w, re.sub(r"(?:es|s)$", "", w) if len(w) > 3 else w)):
        names = _DIMENSIONS.get(cand)
        if names:
            hit = [c for c in r.columns if c in names]
            if len(hit) == 1:
                return hit[0]
        if cand == "date":
            dates = [c for c in r.columns if kind_of(r, c) == "date"]
            if len(dates) == 1:
                return dates[0]
        hit = resolve_column(cand, r.columns)
        if hit:
            return hit
    return None


# ---------------------------------------------------------------- the follow-up's clauses
_HEAD = re.compile(r"^(?:(?:please|pls|kindly|ok(?:ay)?|now|so|and|also|then|just|can\s+you|could\s+you|would\s+you|"
                   r"i\s+(?:want|need)(?:\s+to\s+see)?|give\s+me|let\s+me\s+see|(?:the\s+)?same(?:\s+(?:but|thing))?)\s+)+",
                   re.I)
_TAIL = re.compile(r"(?:\s+(?:please|pls|now|again|instead|too|as\s+well))+$", re.I)
_NOUNS = r"(?:rows?|records?|results?|entries|ones?|items?|orders?|lines?|data|list|table)"
_PAGE = re.compile(rf"^(?:show\s+)?(?:me\s+)?(?:the\s+)?(?:next(?:\s+page)?(?:\s+\d{{1,3}})?(?:\s+{_NOUNS})?|more"
                   rf"(?:\s+{_NOUNS})?|(?:the\s+)?rest(?:\s+of\s+(?:them|it|the\s+{_NOUNS}))?|remaining(?:\s+{_NOUNS})?|"
                   r"load\s+more|see\s+more)$", re.I)
_COUNT = re.compile(r"^(?:how\s+many|count|number\s+of)(?:\s+(?P<what>[a-z][\w ]*?))?(?:\s+(?:are\s+there|in\s+total|"
                    r"in\s+all|there\s+are|is\s+that|are\s+they))?$|^count\s+them$", re.I)
_TOTAL = re.compile(r"^(?:what(?:'s|\s+is)\s+)?(?:the\s+)?(?:total|sum|grand\s+total)(?:\s+(?:of\s+)?(?:the\s+)?"
                    r"(?P<col>[a-z][\w ]*?))?(?:\s+in\s+total)?$", re.I)
_GROUP_BY = re.compile(r"^(?:(?:group(?:ed)?|break(?:\s*-?\s*)?(?:it\s+|them\s+)?down|split(?:\s+it|\s+them)?|"
                       r"summari[sz]e(?:\s+it|\s+them)?|count(?:\s+them)?|show(?:\s+it|\s+them)?)\s+)?(?:by|per|for\s+each|"
                       r"each)\s+(?P<dims>[a-z][\w ,&/]*)$", re.I)
_WISE = re.compile(r"^(?:show\s+)?(?:me\s+)?(?:it\s+)?(?P<dims>[a-z][\w ,&/]*?)\s*-?\s*wise(?:\s+(?:summary|total|"
                   r"totals|breakdown|count|data|list))?$", re.I)
_DISTINCT = re.compile(r"^(?:(?:list|show|give|tell)\s+(?:me\s+)?)?(?:only\s+)?(?:their|the|its|which)\s+"
                       r"(?P<what>names?|buyers?|styles?|lines?|suppliers?|items?|factories|units?|files?|colou?rs?)"
                       r"(?:\s+only)?$|^(?P<ones>which\s+ones|names\s+only|just\s+the\s+names)$", re.I)
_REMOVE = re.compile(r"^(?:remove|hide|drop|without|exclude|no)\s+(?:the\s+)?(?P<cols>.+?)(?:\s+columns?)?$", re.I)
_ADD = re.compile(r"^(?:show|add|include|display|also\s+show|put|with)\s+(?:the\s+|me\s+the\s+)?(?P<cols>.+?)"
                  r"(?:\s+columns?)?(?:\s+(?:beside|besides|next\s+to|alongside|along\s+with|with)\b.*|\s+as\s+well|"
                  r"\s+also|\s+too)?$", re.I)
_ONLY_COLS = re.compile(r"^(?:show\s+)?(?:me\s+)?only\s+(?:the\s+)?(?P<cols>.+?)(?:\s+columns?)?$", re.I)
_OPS = (("between", "between"), ("is not", "neq"), ("isn't", "neq"), ("!=", "neq"), ("<>", "neq"),
        ("contains", "contains"), ("containing", "contains"), ("contain", "contains"), ("like", "contains"),
        (">=", "gte"), ("at least", "gte"), ("<=", "lte"), ("at most", "lte"), ("more than", "gt"),
        ("greater than", "gt"), ("above", "gt"), ("over", "gt"), (">", "gt"), ("less than", "lt"), ("below", "lt"),
        ("under", "lt"), ("<", "lt"), ("after", "gt"), ("since", "gte"), ("before", "lt"), ("==", "eq"), ("=", "eq"),
        ("equals", "eq"), ("equal to", "eq"), ("is", "eq"))
_OP_RE = "|".join(re.escape(o) if not o[0].isalpha() else r"\b" + re.escape(o) + r"\b" for o, _ in _OPS)
_FILTER = re.compile(r"^(?:(?:show|list|give)\s+(?:me\s+)?)?(?:(?:only|just)\s+)?(?:(?:the\s+)?" + _NOUNS + r"\s+)?"
                     r"(?:where|with|whose|having|filter(?:ed)?\s+(?:to|by|on)|for)?\s*(?P<col>[a-z][\w ]*?)\s*"
                     r"(?P<op>" + _OP_RE + r")\s*(?P<val>.+)$", re.I)
_BARE = re.compile(r"^(?:(?:show|list|give)\s+(?:me\s+)?)?(?:only|just)\s+(?:for\s+|the\s+)?(?P<val>.+?)"
                   r"(?:\s+" + _NOUNS + r")?$|^(?:for\s+|of\s+)?(?P<val2>.+?)\s+only$", re.I)


def _split(text: str, r: QueryResult) -> list[str]:
    """Names of the columns a list of words means ("unit and line", "date, buyer"); [] if any one is unknown."""
    out = []
    for part in re.split(r"\s*(?:,|&|/|\band\b)\s*", text.strip()):
        if not part:
            continue
        col = find_column(part, r)
        if col is None:
            return []
        if col not in out:
            out.append(col)
    return out


def _value(text: str, kind: str) -> Any:
    t = text.strip().strip("'\"")
    if kind == "number":
        try:
            return Decimal(t.replace(",", ""))
        except InvalidOperation:
            return None
    if kind == "date":
        from ..data.tools import _one_date      # "6 Oct", "2026-10-06", "06-10-2026"
        try:
            return _one_date(t, date.today())
        except Exception:
            return None
    return t


def _bare_filter(text: str, r: QueryResult) -> Optional[Filter]:
    """"only TAL", "TAL and RHL only": values the user named, accepted only when exactly one text column of the
    result holds every one of them exactly (case aside) — never a guess between two columns."""
    vals = [v.strip().strip("'\"") for v in re.split(r"\s*(?:,|\band\b|\bor\b|/)\s*", text.strip()) if v.strip()]
    if not vals or len(vals) > 6:
        return None
    holders = []
    for i, c in enumerate(r.columns):
        seen = {str(row[i]).strip().casefold(): row[i] for row in r.rows if row[i] is not None}
        if all(v.casefold() in seen for v in vals):
            holders.append((c, [seen[v.casefold()] for v in vals]))
    if len(holders) != 1:
        return None
    col, found = holders[0]
    return Filter(column=col, op="eq", value=found[0]) if len(found) == 1 else Filter(column=col, op="in", value=found)


_VALUE = re.compile(r"[\w&'./ -]+")
_CLAUSE_WORDS = re.compile(r"\b(?:and|or|how|many|sort|sorted|top|by|wise|show|total)\b", re.I)


def _one_value(text: str) -> bool:
    """Does the text read as one value ("zara", "Kmart Australia", "C/09/7") rather than more clauses?"""
    t = text.strip()
    return 0 < len(t.split()) <= 4 and bool(_VALUE.fullmatch(t)) and not _CLAUSE_WORDS.search(t)


def _filter(plan: RefinePlan, r: QueryResult, col: str, op_word: str, val: str) -> bool:
    """"OrderQty above 500", "ShipDate between 1 Nov and 15 Nov", "status is cancelled" — on a column of the result."""
    op = dict(_OPS)[op_word]
    kind = kind_of(r, col)
    if op == "between":
        parts = re.split(r"\s+and\s+", val, maxsplit=1)
        lo, hi = (_value(parts[0], kind), _value(parts[1], kind)) if len(parts) == 2 else (None, None)
        if lo is None or hi is None:
            return False
        plan.filters.append(Filter(column=col, op="between", value=lo, value2=hi))
        return True
    if op in ("gt", "gte", "lt", "lte") and kind not in ("number", "date"):
        return False
    if op == "eq" and kind == "text":
        f = _bare_filter(val, r)                 # the value as recorded, when this column holds it
        if f is not None and f.column == col:
            plan.filters.append(f)
            return True
    v = _value(val, "text" if op == "contains" else kind)
    if v is None:
        return False
    plan.filters.append(Filter(column=col, op=op, value=v))
    return True


def _clause(c: str, r: QueryResult, plan: RefinePlan) -> bool:
    """Read one clause into `plan`; False when it is not a refine of these rows."""
    c = _TAIL.sub("", _HEAD.sub("", c.strip())).strip(" ,.?!")
    if not c or re.fullmatch(rf"(?:the\s+)?{_NOUNS}|(?:it|them|that|those|these)", c, re.I):
        return True
    from ..data.tools import PERIOD
    if _PAGE.match(c):
        plan.page = 1
        return True
    m = re.fullmatch(rf"(?:(?:what|how)\s+about|for|in|of|during|on)?\s*(?P<p>{PERIOD})", c, re.I)
    if m:
        plan.window = m["p"]
        return True
    m = _COUNT.match(c)
    if m:
        plan.scalar = "count"
        what = (m.group("what") or "").strip()
        if what:
            col = find_column(what, r)             # "how many lines" counts the distinct lines, not the rows
            if col is not None:
                plan.of_column = col
            elif not re.fullmatch(_NOUNS + r"|of\s+them|them|there", what, re.I):
                return False
        return True
    m = _TOTAL.match(c)
    if m:
        col = find_column(m["col"], r) if m["col"] else None
        if m["col"] and col is None:
            plan.unresolved.append(m["col"])
            return True
        if col is None:
            adds = [x for x in r.columns if additive(r, x)]
            if len(adds) != 1:
                return False
            col = adds[0]
        if not additive(r, col):
            plan.unresolved.append(col)          # a rate or an id: SQL must compute it (T4)
            return True
        plan.scalar, plan.of_column = "total", col
        return True
    for pat in (_WISE, _GROUP_BY):
        m = pat.match(c)
        if m:
            words = m["dims"]
            cols = _split(words, r)
            if not cols:
                if not re.search(r"[a-z]", words, re.I) or len(words.split()) > 5:
                    return False
                plan.unresolved.append(words.strip())    # "line wise" on a by-type table: the SQL must regroup
                plan.regroup.append(words.strip())
                return True
            plan.group_by = cols
            return True
    m = _DISTINCT.match(c)
    if m:
        what = (m.group("what") or "names").lower()
        if what.startswith("name") or m.group("ones"):
            names = [x for x in r.columns if _NAME_LIKE.search(x) and kind_of(r, x) == "text"]
            col = names[0] if names else None
        else:
            col = find_column(what, r)
        if col is None:
            plan.unresolved.append(what)
            return True
        plan.distinct = col
        return True
    m = _REMOVE.match(c)
    if m:
        cols = _split(m["cols"], r)
        if not cols:
            return False
        base = plan.keep or list(r.columns)
        plan.keep = [x for x in base if x not in cols]
        return True
    m = _ONLY_COLS.match(c)
    if m:
        cols = _split(m["cols"], r)
        if cols and len(cols) >= 1 and not _bare_filter(m["cols"], r):
            plan.keep = [x for x in r.columns if x in cols]
            return True
    m = _FILTER.match(c)
    col = find_column(m["col"], r) if m else None
    if m and col is not None:
        return _filter(plan, r, col, m["op"].lower(), m["val"])
    m = _BARE.match(c)
    if m:
        value = (m["val"] or m["val2"]).strip()
        f = _bare_filter(value, r)
        if f is not None:
            plan.filters.append(f)
            return True
        # "only zara" said in so many words, and no column of these rows holds it: a filter on the earlier query
        if re.match(r"^(?:(?:show|list|give)\s+(?:me\s+)?)?(?:only|just)\b|\bonly$", c, re.I) and _one_value(value) \
                and not find_column(value, r):
            plan.unknown_filter = value.strip("'\"")
            return True
    m = _ADD.match(c)
    if m:
        words = m["cols"]
        cols = _split(words, r)
        if cols:
            return True                          # already shown: nothing to change
        # A column the result does not hold, which the SQL must add (T4) — but only when the wording says it is
        # a column ("beside the data", "as well", "add …"). "show TAL orders with a PCD next week" is a new question.
        said = re.search(r"\b(?:beside|besides|next\s+to|alongside|along\s+with|as\s+well|also|too|columns?)\b", c, re.I) \
            or re.match(r"^(?:add|include)\b", c, re.I)
        if said and len(words.split()) <= 4:
            plan.add.append(words.strip())
            return True
        return False
    if len(c.split()) <= 3:
        f = _bare_filter(c, r)                   # a reply that is only a value: "RHL", "TAL and RHL"
        if f is not None:
            plan.filters.append(f)
            return True
    return False


def parse(message: str, prior: QueryResult) -> Optional[RefinePlan]:
    """A plan when the WHOLE message reshapes `prior`'s rows (filler words aside); None when it is anything else —
    a new question, or words this reader does not know. A column word that is not in the result makes
    `unresolved` (or `add`) non-empty: the caller re-queries (T4) instead of guessing."""
    if not prior.rows or not prior.columns or prior.error:
        return None
    text = re.sub(r"\s+", " ", message or "").strip().rstrip("?!. ")
    if not text or len(text) > 160 or re.match(r"^\s*(select|with)\b", text, re.I):
        return None
    plan = RefinePlan()
    rest, mods = strip_modifiers(text)
    if mods.sort_word:
        col = find_column(mods.sort_word, prior)
        if col is None:
            plan.unresolved.append(mods.sort_word)
            plan.resort = mods.sort_word
        else:
            kind = kind_of(prior, col)
            plan.sort = (col, mods.descending if mods.descending is not None else kind == "number")
            plan.sort_kind = kind
    plan.limit, plan.from_end = mods.limit, mods.from_end
    rest = rest.rstrip("?!. ")
    if not _clauses(rest, prior, plan):
        return None
    if plan.empty():
        return None
    return plan


def _clauses(rest: str, r: QueryResult, plan: RefinePlan) -> bool:
    trial = plan.model_copy(deep=True)
    if _clause(rest, r, trial):
        plan.__dict__.update(trial.__dict__)
        return True
    parts = [p for p in re.split(r"\s*(?:,|;|\band then\b|\bthen\b|\band\b)\s*", rest) if p.strip()]
    if len(parts) < 2:
        return False
    trial = plan.model_copy(deep=True)
    for p in parts:
        if not _clause(p, r, trial):
            return False
    plan.__dict__.update(trial.__dict__)
    return True


# ---------------------------------------------------------------- re-query or not
def row_cap(r: QueryResult) -> Optional[int]:
    """The row limit the query ran under (its TOP / LIMIT), or None when it states none."""
    m = re.search(r"\bTOP\s*\(?\s*(\d+)\s*\)?", r.sql or "", re.I) or re.search(r"\bLIMIT\s+(\d+)\b", r.sql or "", re.I)
    return int(m[1]) if m else None


def capped(r: QueryResult, max_rows: int) -> bool:
    """Did the query stop at its row limit? Then the rows are a cut, not the whole answer. Rows reshaped in Python
    inherit nothing here (they are exactly what the earlier answer showed); a full page of a longer list (T4) is a
    cut like the first page."""
    if r.transform and not r.page:
        return False
    cap = row_cap(r)
    return len(r.rows) >= min(cap or max_rows, max_rows)


def needs_requery(plan: RefinePlan, prior: QueryResult, max_rows: int) -> Optional[str]:
    """Why the stored rows cannot answer this follow-up, or None when they can."""
    if plan.page is not None:
        return "page"
    if plan.window:
        return "window"
    if plan.unresolved:
        return "column not in result: " + ", ".join(plan.unresolved)
    if plan.add:
        return "add column: " + ", ".join(plan.add)
    if plan.unknown_filter:
        return f"filter on a value the rows do not hold: {plan.unknown_filter}"
    if capped(prior, max_rows) and (plan.filters or plan.sort or plan.group_by or plan.scalar or plan.distinct):
        return "capped"
    return None


# ---------------------------------------------------------------- applying the plan
def _cmp(v: Any) -> tuple:
    """A sort key that orders numbers as numbers, dates as dates and text without case; None never compares."""
    if _is_number(v):
        return (0, Decimal(str(v)))
    if isinstance(v, datetime):
        return (1, v)
    if isinstance(v, date):
        return (1, datetime(v.year, v.month, v.day))
    if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return (1, datetime.fromisoformat(v))
    return (2, str(v).casefold())


def _keep(v: Any, f: Filter) -> bool:
    if v is None:
        return f.op == "neq"
    if f.op == "in":
        return str(v).strip().casefold() in {str(x).strip().casefold() for x in f.value}
    if f.op in ("eq", "neq"):
        same = _cmp(v) == _cmp(f.value) if (_is_number(v) or _is_date(v)) and (_is_number(f.value) or _is_date(f.value)) \
            else str(v).strip().casefold() == str(f.value).strip().casefold()
        return same if f.op == "eq" else not same
    if f.op == "contains":
        return str(f.value).casefold() in str(v).casefold()
    try:
        a, b = _cmp(v), _cmp(f.value)
        if a[0] != b[0]:
            return False
        if f.op == "between":
            return b <= a <= _cmp(f.value2)
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[f.op]
    except TypeError:
        return False


def _sum(values: Sequence[Any]) -> Any:
    nums = [v for v in values if _is_number(v)]
    if not nums:
        return None
    if any(isinstance(v, Decimal) for v in nums) or any(isinstance(v, float) for v in nums):
        return sum((Decimal(str(v)) for v in nums), Decimal(0))
    return sum(nums)


def apply(plan: RefinePlan, prior: QueryResult, *, parent: str = "", max_rows: int = 200) -> QueryResult:
    """The derived result: pure Python over `prior.rows`, which is never changed."""
    cols = list(prior.columns)
    rows = [list(x) for x in prior.rows]
    for f in plan.filters:
        i = cols.index(f.column)
        rows = [x for x in rows if _keep(x[i], f)]
    if plan.distinct:
        i = cols.index(plan.distinct)
        vals = {}
        for x in rows:
            if x[i] is not None and str(x[i]).strip():
                vals.setdefault(str(x[i]).strip().casefold(), x[i])
        cols, rows = [plan.distinct], [[v] for v in sorted(vals.values(), key=_cmp)]
    elif plan.group_by:
        keys = [cols.index(c) for c in plan.group_by]
        sums = [c for c in cols if c not in plan.group_by and additive(prior, c)]
        groups: dict[tuple, list[list[Any]]] = {}
        for x in rows:
            groups.setdefault(tuple(x[k] for k in keys), []).append(x)
        out = []
        for key in sorted(groups, key=lambda k: tuple(_cmp(v) if v is not None else (9, "") for v in k)):
            members = groups[key]
            out.append([*key, len(members), *(_sum([m[cols.index(c)] for m in members]) for c in sums)])
        cols, rows = [*plan.group_by, "Count", *sums], out
    if plan.scalar == "count":
        if plan.of_column:
            i = cols.index(plan.of_column)
            n = len({str(x[i]).strip().casefold() for x in rows if x[i] is not None and str(x[i]).strip()})
            cols, rows = [f"Distinct {plan.of_column}"], [[n]]
        else:
            cols, rows = ["Count"], [[len(rows)]]
    elif plan.scalar == "total":
        i = cols.index(plan.of_column)
        cols, rows = [f"Total {plan.of_column}"], [[_sum([x[i] for x in rows]) or 0]]
    if plan.sort and plan.sort[0] in cols:
        i, desc = cols.index(plan.sort[0]), plan.sort[1]
        present = sorted((x for x in rows if x[i] is not None), key=lambda x: _cmp(x[i]), reverse=desc)
        rows = present + [x for x in rows if x[i] is None]          # empty cells last, whichever the direction
    if plan.keep and not (plan.group_by or plan.distinct or plan.scalar):
        idx = [cols.index(c) for c in plan.keep if c in cols]
        cols, rows = [cols[i] for i in idx], [[x[i] for i in idx] for x in rows]
    if plan.limit:
        rows = rows[-plan.limit:] if plan.from_end else rows[:plan.limit]
    rows = rows[:max_rows]
    transform = describe(plan, prior)
    inherited = {k: v for k, v in prior.params.items() if k not in ("shown_label", "of_rows_label", "sort_label")}
    params = {**inherited, "transform_label": transform, "of_rows_label": str(len(prior.rows))}
    return prior.model_copy(update={
        "columns": cols, "rows": rows, "key_columns": [k for k in prior.key_columns if k in cols],
        "tool": prior.tool if prior.tool.endswith(":refined") else f"{prior.tool}:refined",
        "params": params, "transform": transform, "derived_from": parent, "source_rows": len(prior.rows),
        "lookups": [], "corrections": [], "candidates": [], "sql_cached": False})


def _value_text(v: Any) -> str:
    from .templated import fmt
    return fmt(v)


_OP_WORDS = {"eq": "is", "neq": "is not", "contains": "contains", "gt": "is above", "gte": "is at least",
             "lt": "is below", "lte": "is at most"}


def describe(plan: RefinePlan, prior: QueryResult) -> str:
    """'where Factory is TAL, grouped by Line (count, total OrderQty), sorted by OrderQty (largest first), first 10'"""
    parts = []
    for f in plan.filters:
        if f.op == "in":
            vals = [_value_text(v) for v in f.value]
            parts.append(f"where {f.column} is {', '.join(vals[:-1])} or {vals[-1]}")
        elif f.op == "between":
            parts.append(f"where {f.column} is between {_value_text(f.value)} and {_value_text(f.value2)}")
        else:
            parts.append(f"where {f.column} {_OP_WORDS[f.op]} {_value_text(f.value)}")
    if plan.distinct:
        parts.append(f"the distinct {plan.distinct} values")
    elif plan.group_by:
        sums = [c for c in prior.columns if c not in plan.group_by and additive(prior, c)]
        what = ", ".join(["count"] + [f"total {c}" for c in sums])
        parts.append(f"grouped by {' and '.join(plan.group_by)} ({what})")
    if plan.scalar == "count":
        parts.append(f"counting distinct {plan.of_column}" if plan.of_column else "counted")
    elif plan.scalar == "total":
        parts.append(f"total of {plan.of_column}")
    if plan.sort:
        parts.append(f"sorted by {plan.sort[0]} ({direction_words(plan.sort[1], plan.sort_kind)})")
    if plan.keep and not (plan.group_by or plan.distinct or plan.scalar):
        dropped = [c for c in prior.columns if c not in plan.keep]
        parts.append(f"without {', '.join(dropped)}" if len(dropped) <= len(plan.keep)
                     else f"showing {', '.join(plan.keep)}")
    if plan.limit:
        parts.append(f"{'last' if plan.from_end else 'first'} {plan.limit}")
    return ", ".join(parts)


# ---------------------------------------------------------------- chips
def suggestions(r: QueryResult, limit: int = 5, max_rows: int = 200) -> list[str]:
    """Follow-ups that reshape this answer, each one a message parse() reads back into a plan — a click never costs a
    model call. On a list that stopped at its row limit only a cut and the next page are offered (T4: the page is
    read with the same statement, no model). Nothing is offered on a scalar or an empty result."""
    if not r.rows or not r.columns or r.error or len(r.rows) < 3 or (len(r.columns) == 1 and len(r.rows) == 1):
        return []
    out: list[str] = []
    cut = capped(r, max_rows)
    if (cut or (r.page and len(r.rows) >= max_rows)) and pageable(r):
        out.append(f"Show the next {max_rows} rows")
    if not cut:
        dims = [c for c in r.columns if any(c in names for names in _DIMENSIONS.values())
                and c not in ("ExportOrderID", "ExportPONo", "PONumber", "PONo")
                and 1 < len({str(x[r.columns.index(c)]) for x in r.rows}) < len(r.rows)]
        if dims:
            word = next(k for k, names in _DIMENSIONS.items() if dims[0] in names)
            out.append(f"{word.capitalize()} wise")
        dates = [c for c in r.columns if kind_of(r, c) == "date"]
        if dates:
            out.append(f"Sort by {_chip_words(dates[0])} latest first")
        nums = [c for c in r.columns if additive(r, c) and c != "Count"]     # adding up counts says nothing new
        if nums and len(r.rows) > 10:
            out.append(f"Sort by {_chip_words(nums[0])}, top 10")
        elif nums:
            out.append(f"Total {_chip_words(nums[0])}")
        if "Factory" in r.columns:
            counts: dict[str, int] = {}
            for x in r.rows:
                v = x[r.columns.index("Factory")]
                if v:
                    counts[str(v)] = counts.get(str(v), 0) + 1
            if len(counts) > 1:
                out.append(f"Only {max(counts, key=counts.get)}")
    elif len(r.rows) > 10:
        out.append("Top 10")
    return [s for s in out if parse(s, r) is not None][:limit]


def pageable(r: QueryResult) -> bool:
    """Can the next page of this result's query be read? A model-written statement can; a fixed tool can unless it
    keeps one factory in Python (its pages would not line up with what was shown)."""
    base = r.tool.split(":")[0]
    if base == "generated":
        return bool(r.sql)
    try:
        from ..data.tools import load_fixed_tools
        tool = next((t for t in load_fixed_tools() if t.get("name") == base), None)
    except Exception:
        return False
    return tool is not None and not tool.get("filter")


def _chip_words(col: str) -> str:
    """'ShipDate' -> 'ship date', 'PCD' -> 'PCD', 'OrderQty' -> 'order qty': how a chip names a column (the parser
    reads it back either way)."""
    parts = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", col)
    parts = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", parts)
    return " ".join(w if w.isupper() and len(w) > 1 else w.lower() for w in re.split(r"[_\s]+", parts) if w)
