"""ragbot/ui/view.py: the factory pick never widens a scope, titles/export/labels, and every HTML piece escapes
what it is given (answers, quotes, SQL and file names can hold any text)."""
from datetime import datetime

from ragbot.auth.filters import scope_where
from ragbot.auth.models import GROUP_WIDE_FACTORY, Scope
from ragbot.domain_agents.watch import Signal
from ragbot.models import Reference
from ragbot.ui import view

TAL_RHL = Scope(factories=frozenset({"TAL", "RHL"}))
EVERY = Scope(factories=frozenset({"*"}), is_admin=True)


def _sig(rule="behind_schedule", factory="TAL", level="red", value=455.0, kind="ops", **kw):
    base = dict(agent="order", rule=rule, factory=factory, level=level, value=value, title=f"{value:.0f} orders late",
                question=f"Which {factory} orders are behind schedule?", weight=3, metric="Behind schedule",
                database="BitopiSplint", views=["rag.vw_OrderShipment"], sql="SELECT 1", as_of="2026-10-07T03:52:17",
                kind=kind)
    base.update(kw)
    return Signal(**base)


# ── narrow_scope ──────────────────────────────────────────────────────────────────────────────────────────────

def test_narrow_scope_picks_one_factory_and_keeps_the_rest():
    narrowed = view.narrow_scope(TAL_RHL, "TAL")
    assert narrowed.factories == frozenset({"TAL"})
    assert narrowed.model_dump(exclude={"factories"}) == TAL_RHL.model_dump(exclude={"factories"})


def test_narrow_scope_all_or_no_pick_returns_the_scope_unchanged():
    assert view.narrow_scope(TAL_RHL, "All") is TAL_RHL
    assert view.narrow_scope(TAL_RHL, None) is TAL_RHL
    assert view.narrow_scope(TAL_RHL, "") is TAL_RHL


def test_narrow_scope_never_widens():
    # a factory outside the scope (a forged pill value) leaves the user's own scope, not the factory asked for
    assert view.narrow_scope(TAL_RHL, "BGL") is TAL_RHL
    assert not view.narrow_scope(TAL_RHL, "BGL").allows_factory("BGL")
    # from an every-factory scope a pick narrows; the admin flag and the rest stay
    narrowed = view.narrow_scope(EVERY, "RHL")
    assert narrowed.factories == frozenset({"RHL"}) and narrowed.is_admin
    assert not narrowed.allows_factory("TAL")


def test_narrow_scope_keeps_group_wide_documents_visible():
    where = scope_where(view.narrow_scope(EVERY, "TAL"))
    assert set(where["factory"]) == {"TAL", GROUP_WIDE_FACTORY}


def test_narrowed_scope_changes_the_cache_key():
    assert view.narrow_scope(EVERY, "TAL").key() != EVERY.key()


def test_factories_offered_only_in_scope():
    assert view.factories_offered(TAL_RHL, {"TAL", "RHL", "BGL"}, []) == ["RHL", "TAL"]
    assert view.factories_offered(EVERY, set(), ["tal", "BGL", "*"]) == ["BGL", "TAL"]   # no signals: configured codes


# ── labels, titles, export ────────────────────────────────────────────────────────────────────────────────────

def test_join_names():
    assert view.join_names(["BGL", "RHL", "TAL"]) == "BGL, RHL and TAL"
    assert view.join_names(["TAL"]) == "TAL"
    assert view.join_names([]) == ""


def test_conversation_title_trims_at_a_word():
    hist = [{"role": "user", "text": "Why does TAL have 432 confirmed orders past their ship date with no export invoice?"}]
    title = view.conversation_title(hist, limit=40)
    assert title.endswith("…") and len(title) <= 41 and not title[:-1].endswith(" ")
    assert view.conversation_title([]) == "New conversation"
    assert view.conversation_title([{"role": "user", "text": "  IT   password\npolicy "}]) == "IT password policy"


def _data_ref(**kw):
    base = dict(marker="D1", kind="data", database="BitopiSplint", engine="sqlserver", views=["rag.vw_OrderShipment"],
                row_keys=["ExportOrderID=TAL-1"], row_count=432, as_of=datetime(2026, 10, 7, 9, 52), tool="orders_late",
                sql="SELECT TOP 200 * FROM rag.vw_OrderShipment WHERE Factory = 'TAL'", params={"factory": "TAL"})
    base.update(kw)
    return Reference(**base)


def _pdf_ref(**kw):
    base = dict(marker="P1", kind="pdf", source="Bitopi_IT_Policy_Book.pdf", section="Passwords", page=18,
                category="IT", quote="Passwords change every 90 days.", chunk_id="Bitopi_IT_Policy_Book#p18#c1")
    base.update(kw)
    return Reference(**base)


def test_done_label_and_read_list():
    assert view.done_label([_data_ref()], 4.21) == "Queried rag.vw_OrderShipment · 432 rows · 4.2 s"
    assert view.done_label([_pdf_ref(), _pdf_ref(marker="P2", page=19)]) == "Read Bitopi_IT_Policy_Book.pdf p. 18, 19"
    assert view.done_label([]) == "No source found"
    assert view.read_list([_data_ref(), _pdf_ref()]) == [
        "BitopiSplint · rag.vw_OrderShipment · 432 rows", "Bitopi_IT_Policy_Book.pdf p. 18"]


def test_style_markers_only_touches_markers():
    assert view.style_markers("432 orders [D1], see [P2].") == "432 orders :green-badge[D1], see :green-badge[P2]."
    assert view.style_markers("no invoice[D1][D2]") == "no invoice :green-badge[D1] :green-badge[D2]"
    assert view.style_markers("[note] and [X1]") == "[note] and [X1]"


def test_export_markdown_carries_every_reference():
    hist = [{"role": "user", "text": "How many TAL orders are late?"},
            {"role": "assistant", "text": "432 [D1]. Policy [P1].", "refs": [_data_ref(), _pdf_ref()]}]
    md = view.export_markdown(hist, "TAL orders", "Scope: TAL", datetime(2026, 10, 7, 10, 0))
    assert md.startswith("# TAL orders\n")
    assert "## How many TAL orders are late?" in md and "432 [D1]. Policy [P1]." in md
    assert "[D1] BitopiSplint · rag.vw_OrderShipment · 432 row(s) · as of 07 Oct 2026 09:52" in md
    assert "SELECT TOP 200 * FROM rag.vw_OrderShipment" in md
    assert "[P1] Bitopi_IT_Policy_Book.pdf · Passwords · page 18 · “Passwords change every 90 days.”" in md


# ── freshness ─────────────────────────────────────────────────────────────────────────────────────────────────

def test_freshness_rows():
    run = {"finished": "2026-10-07T03:52:17"}
    data = [_sig(rule="line_data_age", factory=f, level="red", value=17.0, kind="data",
                 values={"day": "2026-09-20", "age": 17}) for f in ("BGL", "TAL")]
    rows = view.freshness(run, data, {"index_updated_at": "2026-09-30T04:41:25", "documents": 2},
                          now=datetime(2026, 10, 7, 4, 10))
    assert [(r.label, r.stamp, r.age, r.tone) for r in rows] == [
        ("Order data", "07 Oct 03:52", "today", "fresh"),
        ("Line data", "17 days old", "17 days", "stale"),
        ("Document index", "30 Sep 04:41", "2 indexed · 30 Sep", "plain")]
    stale = view.freshness(run, [], None, now=datetime(2026, 10, 7, 9, 0))
    assert stale[0].tone == "stale" and len(stale) == 1


# ── HTML pieces ───────────────────────────────────────────────────────────────────────────────────────────────

def test_health_ring():
    assert view.health_ring_svg(None).count("<circle") == 1                       # track only
    ring = view.health_ring_svg(25)
    assert 'stroke="#B42318"' in ring and 'stroke-dasharray="53.4 160.2"' in ring
    assert 'stroke="#1D7A45"' in view.health_ring_svg(90)
    assert 'stroke-dasharray="213.6 0.0"' in view.health_ring_svg(140)          # clamped


EVIL = '<script>alert(1)</script><img src=x onerror=alert(2)>'


def test_every_piece_escapes_its_text():
    pieces = [
        view.source_card_html(_data_ref(database=EVIL, views=[EVIL], sql=EVIL, params={"x": EVIL}, row_keys=[EVIL])),
        view.source_card_html(_pdf_ref(source=EVIL, section=EVIL, quote=EVIL, category=EVIL)),
        view.user_bubble_html(EVIL), view.header_html(EVIL, EVIL), view.attention_html(_sig(title=EVIL, factory=EVIL), EVIL),
        view.banner_html([_sig(kind="data", title=EVIL, factory=EVIL)]), view.kpi_html(EVIL, 3, EVIL),
        view.recent_current_html(EVIL), view.composer_meta_html(EVIL), view.caution_html(EVIL),
        view.greeting_html(EVIL, EVIL), view.data_status_html([view.Fresh(EVIL, EVIL, EVIL, "fresh")], EVIL, EVIL),
        view.freshness_card_html([view.Fresh(EVIL, EVIL, EVIL, "stale")]), view.topline_html(EVIL, EVIL),
    ]
    for html in pieces:
        assert "<script>" not in html and "<img src=x" not in html, html     # (icons are <img src="data:…">)
        assert "&lt;script&gt;" in html, html


def test_banner_for_data_age():
    sigs = [_sig(rule="line_data_age", factory=f, value=17.0, kind="data", values={"day": "2026-09-20", "age": 17})
            for f in ("TAL", "BGL", "RHL")]
    html = view.banner_html(sigs)
    assert "Line data is 17 days old</strong> for BGL, RHL and TAL (last update 20 Sep 2026)" in html
    assert view.banner_html([]) is None


def test_badges():
    assert "Critical" in view.badge_html("red") and "Watch" in view.badge_html("amber")


# ── sortable answer tables ────────────────────────────────────────────────────────────────────────────────────

TEMPLATED = """TAL confirmed orders past their ship date (200 shown) [D1]:

| ExportOrderID | ExportPONo | OrderQty | ShipDate | Merchandiser |
|---|---|---|---|---|
| TAL-26-1741-2 | 1267310 | 495 | 2026-09-07 | 09152 |
| TAL-26-1741-10 | 1267310 | 2,500 | 2026-09-08 | — |

Line data is old."""


def test_split_md_tables_keeps_text_around_the_table():
    parts = view.split_md_tables(TEMPLATED)
    assert [k for k, _ in parts] == ["text", "table", "text"]
    header, rows = parts[1][1]
    assert header == ["ExportOrderID", "ExportPONo", "OrderQty", "ShipDate", "Merchandiser"]
    assert rows[1] == ["TAL-26-1741-10", "1267310", "2,500", "2026-09-08", "—"]
    assert parts[0][1].strip().endswith("[D1]:") and parts[2][1].strip() == "Line data is old."


def test_split_md_tables_needs_a_separator_and_pads_short_rows():
    assert view.split_md_tables("| not | a table |\nplain") == [("text", "| not | a table |\nplain")]
    (_, (header, rows)), = view.split_md_tables("| a | b \\| c |\n|:--|--:|\n| 1 |")
    assert header == ["a", "b | c"] and rows == [["1", ""]]


def test_column_kinds():
    _, (header, rows) = view.split_md_tables(TEMPLATED)[1]
    # ExportPONo and Merchandiser are digit codes: numbers to sort by; "—" (no value) does not count
    assert view.column_kinds(rows, len(header)) == ["text", "num", "num", "date", "num"]
    assert view.column_kinds([["**Total**"], ["12.5%"]], 1) == ["text"]
    assert view.column_kinds([[""], ["—"]], 1) == ["text"]


def test_sortable_table_html():
    html = view.sortable_table_html(["Buyer", "Orders"], [["H&M", "12"], ["**Total**", "12"]])
    assert '<button type="button" class="ka-sort" title="Sort by Buyer">Buyer' in html
    assert '<th scope="col" data-kind="num" class="ka-num">' in html and '<td class="ka-num">12</td>' in html
    assert '<tr data-i="1" class="ka-total"><td>Total</td>' in html          # emphasis stripped, kept last
    assert "H&amp;M" in html
    evil = view.sortable_table_html([EVIL], [[EVIL]])
    assert "<script>" not in evil and "<img src=x" not in evil and "&lt;script&gt;" in evil


def test_sort_script_has_no_tag_like_text():
    # the frontend sanitizer drops a <script>/<style> whose text holds '<' followed by a letter or '/'
    import re
    from pathlib import Path
    js = (Path(view.__file__).with_name("sort_table.js")).read_text(encoding="utf-8")
    assert not re.search(r"<[/\w]", js)


def test_full_rows_replaces_a_preview_only():
    cols = ["ExportOrderID", "OrderQty"]
    result = [["TAL-1", "495"], ["TAL-2", "2500"], ["TAL-3", "10"]]
    assert view.full_rows(cols, result[:2], [(cols, result)]) == result               # the preview → every row
    assert view.full_rows(cols, [["TAL-1", "495"], ["TAL-9", "1"]], [(cols, result)]) is None   # other rows
    assert view.full_rows(["Buyer", "OrderQty"], result[:2], [(cols, result)]) is None       # other columns
    assert view.full_rows(cols, result, [(cols, result)]) is None                     # already complete
    note = "Line.\n\nOnly the first rows are shown here; the query returned 200 in total."
    assert view.PREVIEW_NOTE.sub("", note) == "Line."
