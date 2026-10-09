"""P1: entity dictionaries — a company named by code or by name becomes a bound parameter, never question text."""
from pathlib import Path

import pytest

from ragbot.data.entities import Entities, Company, company_from_question, load_entities


def test_the_real_dictionary_loads_and_knows_misami():
    ents = load_entities(Path("config/reference"))
    assert ents.company("02").name.startswith("MISAMI") and ents.company("2").short == "MGL"
    assert ents.company("06").factory == "TAL" and ents.company("02").factory == ""    # Misami has no scope code
    assert "maintenance store" in ents.store_aliases and "closing balance" in ents.metrics


@pytest.mark.parametrize("q, code", [
    ("please find the closing balance of 1178108 this item code for nonproduction iten and companyid=02 and store name mantainance store of misami", "02"),
    ("closing balance of 1178108 at misami", "02"),
    ("goods receipts of item 1178108 at company 02", "02"),
    ("stock of 1178108 for company id 06", "06"),
    ("stock of 1178108 at Tarasima", "06"),
    ("stock of 1178108 at MGL", "02"),
    ("stock of 1178108 at the Kaifeng store", "08"),
])
def test_company_from_question(q, code):
    c = company_from_question(q, load_entities(Path("config/reference")))
    assert c is not None and c.code == code


def test_two_companies_or_none_means_no_assumption():
    ents = load_entities(Path("config/reference"))
    assert company_from_question("stock of 1178108 at misami and tarasima", ents) is None
    assert company_from_question("stock of 1178108", ents) is None
    assert company_from_question("the misamix store", ents) is None          # whole words only


def test_a_dictionary_without_the_file_is_empty(tmp_path):
    assert load_entities(tmp_path).companies == []
    assert company_from_question("company 02", Entities()) is None


def test_names_shorter_than_three_letters_never_match():
    ents = Entities([Company("99", "X", "AB", ("ab",))])
    assert company_from_question("ab test", ents) is None
    assert company_from_question("company 99", ents).code == "99"
