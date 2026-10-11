"""Phase Q: what a question names — factories, lines, units, styles, export orders, POs, files.

The 2026-10-11 failures started here: nothing in the pipeline read "TAL" as a factory or "line 2102" as a line,
so the question reached the SQL model as plain words and came back as `Line = 2102` against a text column.
"""
import pytest

from ragbot.data.entities import question_entities as qe

CODES = ["TAL", "RHL", "BGL", "KTL", "CKDL"]


def _q(text):
    return qe(text, CODES)


def test_the_owners_question_is_read_as_a_factory_and_a_line():
    e = _q("What style is running on TAL line 2102?")
    assert e.factories == ("TAL",) and e.lines == ("2102",)
    assert e.names_a_record and e.kinds() == {"factory", "line"}


@pytest.mark.parametrize("text", [
    "What style is running on TAL line 2102?",
    "on TAL in line 2102, which style is running?",
    "which buyer and style are running on line no. 2102 of TAL today",
    "what is running on TAL L-2102 yesterday",
    "TAL 2102 line style",
    "line #2102 of TAL",
])
def test_every_way_staff_write_a_line_number(text):
    e = _q(text)
    assert e.lines == ("2102",), text
    assert e.factories == ("TAL",), text


def test_an_export_order_or_a_file_code_is_never_a_factory():
    eo = _q("Show the DHU of EO TAL-26-1985-1 this week")
    assert eo.factories == () and eo.eos == ("TAL-26-1985-1",) and eo.lines == ()
    fr = _q("Which export orders are in file FR-26-1985?")
    assert fr.files == ("FR-26-1985",) and fr.eos == () and fr.factories == ()
    po = _q("What is the status of PO 594520-9192?")
    assert po.pos == ("594520-9192",) and po.factories == ()


def test_a_company_name_counts_as_its_factory_only_when_it_has_a_code():
    assert _q("Tarasima line 2102").factories == ("TAL",)
    assert _q("closing balance at Remi Holdings").factories == ("RHL",)
    misami = _q("how much grease is left in the misami maintenance store")
    assert misami.factories == () and misami.company is not None and misami.company.code == "02"


def test_all_factories_is_recognised_and_names_no_single_factory():
    e = _q("all factory line wise production summary of today with style showing")
    assert e.all_factories and e.factories == ()


def test_units_and_styles():
    assert _q("Which lines have the highest DHU in Unit 03?").units == ("03",)
    assert _q("which lines are running style 71245-B today").styles == ("71245-B",)
    assert _q("style no. 4521 output yesterday").styles == ("4521",)
    # a style word with no code after it names no style ("what style is running" must not capture "is")
    assert _q("What style is running on TAL line 2102?").styles == ()


def test_a_factory_alone_is_not_a_record_so_a_policy_question_stays_a_policy_question():
    e = _q("What is the TAL holiday policy?")
    assert e.factories == ("TAL",) and not e.names_a_record
    assert not _q("how many Kaspersky licences does the Group have?").names_a_record


def test_two_lines_are_both_read():
    assert _q("style running on lines 2102 and 2103").lines == ("2102", "2103")


# ---- what the SQL model is told about those records ---------------------------------------------------------
def _cat(name, views):
    from ragbot.data.catalog import Catalog, View
    return Catalog(database=name, dialect="tsql", connection_env="X", rules=[],
                   views=[View(name=v, grain="", description="", key_columns=[], columns=cols)
                          for v, cols in views.items()])


def test_entity_lines_name_the_column_of_each_shown_view():
    from ragbot.data.entities import entity_lines
    cats = [_cat("VistaQ_Default_RHL", {"rag.vw_PlanDay": {"Factory": "", "Line": "", "Style": ""},
                                        "rag.vw_PlanQualityDay": {"Factory": "", "Line": ""}}),
            _cat("PlanningTNA", {"rag.vw_LineDailyOutput": {"Factory": "", "LineNumber": ""}})]
    lines = entity_lines("What style is running on TAL line 2102?", cats)
    joined = "\n".join(lines)
    assert "line '2102' = Line in rag.vw_PlanDay, rag.vw_PlanQualityDay; LineNumber in rag.vw_LineDailyOutput" in joined
    assert "factory 'TAL' = Factory in rag.vw_PlanDay" in joined


def test_an_entity_with_no_column_in_the_views_says_so():
    from ragbot.data.entities import entity_lines
    lines = entity_lines("DHU of line 2102", [_cat("X", {"rag.vw_Q": {"Factory": "", "Defect": ""}})])
    assert any("no column of the views shown" in ln for ln in lines)


def test_the_company_line_is_only_for_catalogs_that_have_a_company_column():
    from ragbot.data.entities import has_company_column
    assert not has_company_column([_cat("VistaQ_Default_RHL", {"rag.vw_PlanDay": {"Factory": "", "Line": ""}})])
    assert has_company_column([_cat("Inventory", {"rag.vw_StockBalance": {"CompanyID": "", "ItemCode": ""}})])
