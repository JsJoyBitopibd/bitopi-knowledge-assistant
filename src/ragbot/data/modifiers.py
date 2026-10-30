"""Sort and limit phrases in a question (Phase T2): "sorted by ship date", "top 10", "largest 5 by quantity".

Until v1.14.0 a fixed tool ran its hand-written ORDER BY and TOP whatever the question said, so "TAL orders with a
PCD next week sorted by quantity, top 10" came back in PCD order with every row. Here the phrase is cut out of the
question before any tool pattern sees it (the tools were written for the question without it), and what it asked
for becomes a `Modifiers` value:

- a fixed tool that declares an `order_by` parameter sorts in its SQL by a column from its own whitelist — never a
  word of the question — and a tool that cannot honour a requested sort is skipped, so the model writes it;
- a limit always applies to the rows in Python (the tool's own order), and in SQL too when the tool declares a
  `limit` parameter;
- model-written SQL gets the request as explicit lines in its prompt.

Nothing here touches a database or a model.
"""
from __future__ import annotations

import difflib
import re
from typing import Mapping, Optional, Sequence

from pydantic import BaseModel

_DESC_WORDS = r"desc(?:ending)?|(?:highest|largest|biggest|latest|newest|most|max(?:imum)?|high\s+to\s+low)(?:\s+(?:first|on\s+top))?|z\s*-\s*a"
_ASC_WORDS = r"asc(?:ending)?|(?:lowest|smallest|earliest|oldest|least|min(?:imum)?|low\s+to\s+high)(?:\s+(?:first|on\s+top))?|a\s*-\s*z"
_DIRECTION = rf"(?:\s*,?\s*(?:in\s+)?(?P<dir>{_DESC_WORDS}|{_ASC_WORDS})(?:\s+order)?)?"
_COLUMN = r"(?P<col>[A-Za-z][\w%/.-]*(?:\s+[A-Za-z][\w%/.-]*){0,2}?)"
# where a column phrase ends: punctuation, the end, or a word that starts the next clause
_STOP = (r"(?=\s*(?:[,.?;!)]|$|\b(?:and|then|top|first|limit|only|for|with|in|of|at|from|this|next|last|today|"
         r"yesterday|tomorrow|show|please|asc|desc|ascending|descending|highest|lowest|largest|smallest|latest|"
         r"earliest|newest|oldest)\b))")
_PERIODS = r"(?:days?|weeks?|months?|years?|hours?|minutes?)"

_SORT = re.compile(rf"\b(?:sort(?:ed)?|order(?:ed)?|arrang(?:e|ed)|rank(?:ed)?)\s+(?:it\s+|them\s+|this\s+|that\s+|"
                   rf"the\s+(?:list|rows|result|results|data|table)\s+)?by\s+{_COLUMN}{_STOP}{_DIRECTION}", re.I)
_FIRST = re.compile(r"\b(?P<adj>highest|largest|biggest|latest|newest|lowest|smallest|earliest|oldest)\s+"
                    r"(?P<col>[A-Za-z][\w%/.-]*(?:\s+[A-Za-z][\w%/.-]*)?)\s+(?:first|on\s+top)\b", re.I)
_TOP_BY = re.compile(r"\b(?P<adj>top|highest|largest|biggest|bottom|lowest|smallest)\s+(?P<n>\d{1,3})\s+"
                     r"(?P<noun>[A-Za-z]+(?:\s+[A-Za-z]+)?)\s+by\s+" + _COLUMN + _STOP, re.I)
_TOP = re.compile(rf"\b(?:the\s+)?(?P<adj>top|first|largest|biggest|highest)\s+(?P<n>\d{{1,3}})\b(?!\s*{_PERIODS}\b)", re.I)
_LAST = re.compile(rf"\b(?:the\s+)?(?P<adj>last|bottom|lowest|smallest)\s+(?P<n>\d{{1,3}})\b(?!\s*{_PERIODS}\b)", re.I)
_ONLY_N = re.compile(rf"\b(?:only\s+)?(?:limit(?:ed)?\s+(?:it\s+)?to\s+)(?P<n>\d{{1,3}})(?:\s+(?:rows|records|results|entries))?\b"
                     rf"|\b(?P<n2>\d{{1,3}})\s+(?:rows|records|results|entries)\s+only\b", re.I)

_DESC_ADJ = {"top", "highest", "largest", "biggest", "latest", "newest", "most", "first"}


class Modifiers(BaseModel):
    sort_word: str = ""                 # the user's words for the sort column ("quantity", "ship date")
    descending: Optional[bool] = None   # None = not stated
    limit: Optional[int] = None         # top / first / last N
    from_end: bool = False              # "last 5", "bottom 5": the tail of the order

    def __bool__(self) -> bool:
        return bool(self.sort_word or self.limit)

    def lines(self, max_rows: int) -> list[str]:
        """The request as explicit lines for the SQL prompt (Phase T2), never the question's own words as SQL."""
        out = []
        if self.sort_word:
            way = {True: ", largest / latest first (DESC)", False: ", smallest / earliest first (ASC)",
                   None: ""}[self.descending]
            out.append(f'ORDER BY the column that holds "{self.sort_word}"{way}')
        if self.limit:
            n = min(self.limit, max_rows)
            out.append(f"Return at most {n} rows (TOP ({n}) / LIMIT {n})"
                       + (", the last ones in that order" if self.from_end else ""))
        return out


def _direction(word: Optional[str]) -> Optional[bool]:
    if not word:
        return None
    w = word.lower()
    return bool(re.match(_DESC_WORDS, w)) if re.match(rf"(?:{_DESC_WORDS}|{_ASC_WORDS})$", w) else None


def _cut(text: str, span: tuple[int, int], keep: str = "") -> str:
    return text[:span[0]] + (f" {keep} " if keep else " ") + text[span[1]:]


def strip(question: str) -> tuple[str, Modifiers]:
    """(the question without its sort/limit phrases, what they asked for). The rest is whitespace-normalised so a
    fixed tool's pattern sees the question it was written for; "top 5 lines by DHU" keeps "lines"."""
    q = re.sub(r"\s+", " ", question or "").strip()
    mods = Modifiers()
    m = _TOP_BY.search(q)
    if m:
        adj = m["adj"].lower()
        mods.limit, mods.sort_word = int(m["n"]), m["col"].strip()
        mods.descending = adj in _DESC_ADJ
        q = _cut(q, m.span(), keep=m["noun"])
    m = _SORT.search(q)
    if m and not mods.sort_word:
        mods.sort_word, mods.descending = m["col"].strip(), _direction(m["dir"])
        q = _cut(q, m.span())
    m = _FIRST.search(q)
    if m and not mods.sort_word:
        mods.sort_word, mods.descending = m["col"].strip(), m["adj"].lower() in _DESC_ADJ
        q = _cut(q, m.span())
    if mods.limit is None:
        for pat, from_end in ((_TOP, False), (_LAST, True)):
            m = pat.search(q)
            if m:
                mods.limit, mods.from_end = int(m["n"]), from_end
                adj = m["adj"].lower()
                if mods.descending is None and adj in ("largest", "biggest", "highest"):
                    mods.descending = True
                elif mods.descending is None and adj in ("lowest", "smallest"):
                    mods.descending, mods.from_end = False, False
                q = _cut(q, m.span())
                break
    if mods.limit is None:
        m = _ONLY_N.search(q)
        if m:
            mods.limit = int(m["n"] or m["n2"])
            q = _cut(q, m.span())
    if mods.limit is not None and mods.limit <= 0:
        mods.limit = None
    q = re.sub(r"\s+([,.?;!])", r"\1", re.sub(r"\s+", " ", q))
    q = re.sub(r"[,;]+\s*(?=[?.!]|$)", "", q).strip(" ,;")      # "next week, ?" left by a cut → "next week?"
    q = re.sub(r"^(?:and|then)\s+", "", q, flags=re.I)
    return (q if mods else re.sub(r"\s+", " ", question or "").strip()), mods


def _words(name: str) -> str:
    """'ShipDate' -> 'ship date', 'OrderQty' -> 'order qty', 'ExportPONo' -> 'export po no', 'Order_Qty' -> 'order qty'."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    return re.sub(r"[_\s]+", " ", s).strip().lower()


_ALIASES = {"qty": "quantity", "quantity": "qty", "pcs": "qty", "pieces": "qty", "amount": "value",
            "value": "amount", "no": "number", "number": "no"}


def _expand(words) -> set[str]:
    ws = {w for w in words if w}
    return ws | {_ALIASES[w] for w in ws if w in _ALIASES}


def resolve_column(word: str, columns: Sequence[str], synonyms: Optional[Mapping[str, Sequence[str]]] = None,
                   cutoff: float = 0.85) -> Optional[str]:
    """The column of `columns` the user's word(s) mean, or None. Tried in order: the exact name (case-insensitive);
    the name's words ("ship date" = ShipDate); a synonym from the catalog or the tool's whitelist; the only column
    whose words contain the user's ("quantity" = OrderQty via qty); then a close spelling (difflib >= cutoff)."""
    w = re.sub(r"\s+", " ", (word or "").strip().lower())
    if not w or not columns:
        return None
    by_lower = {c.lower(): c for c in columns}
    if w in by_lower:
        return by_lower[w]
    spaced = {_words(c): c for c in columns}
    if w in spaced:
        return spaced[w]
    if w.replace(" ", "") in by_lower:
        return by_lower[w.replace(" ", "")]
    for key, names in (synonyms or {}).items():
        if w == str(key).lower() or w in {str(n).lower() for n in names or ()}:
            hit = by_lower.get(str(key).lower()) or next((by_lower.get(str(n).lower()) for n in names or ()
                                                          if str(n).lower() in by_lower), None)
            if hit:
                return hit
    user = _expand(w.split())
    named = {c: _expand(_words(c).split()) for c in columns}
    for holders in ([c for c, ws in named.items() if user <= ws],          # every word the user said is in the name
                    [c for c, ws in named.items() if user & ws]):          # else: one column shares a word
        if len(holders) == 1:
            return holders[0]
        if len(holders) > 1:
            break                                                          # ambiguous: only a close spelling decides
    close = difflib.get_close_matches(w, list(spaced), n=1, cutoff=cutoff)
    if close:
        return spaced[close[0]]
    close = difflib.get_close_matches(w.replace(" ", ""), list(by_lower), n=1, cutoff=cutoff)
    return by_lower[close[0]] if close else None


def quote(column: str, dialect: str) -> str:
    return f"`{column}`" if dialect == "mysql" else f"[{column}]"


def order_clause(column: str, descending: bool, dialect: str = "tsql") -> str:
    """'[OrderQty] DESC' — only ever a name from a whitelist and a fixed keyword."""
    return f"{quote(column, dialect)} {'DESC' if descending else 'ASC'}"


def direction_words(descending: bool, kind: str = "") -> str:
    """How the lead says the direction: dates read latest/earliest, everything else largest/smallest."""
    if kind == "date":
        return "latest first" if descending else "earliest first"
    if kind == "text":
        return "Z to A" if descending else "A to Z"
    return "largest first" if descending else "smallest first"
