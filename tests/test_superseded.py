from ragbot.ingest.pipeline import superseded_sources


def test_two_revisions_older_is_superseded():
    titles = {"Doc_v1.pdf": "Bitopi Group IT SOP Manual v1", "Doc_v2.pdf": "Bitopi Group IT SOP Manual v2"}
    assert superseded_sources(titles) == {"Doc_v1.pdf"}


def test_rev_and_ver_and_space_forms():
    titles = {"a.pdf": "Employee Handbook Rev1", "b.pdf": "Employee Handbook Rev3",
              "c.pdf": "Policy Manual ver 1", "d.pdf": "Policy Manual ver 2"}
    assert superseded_sources(titles) == {"a.pdf", "c.pdf"}


def test_single_document_never_superseded():
    titles = {"only.pdf": "Bitopi Group IT Policy Book"}
    assert superseded_sources(titles) == set()


def test_unrelated_titles_not_grouped():
    titles = {"a.pdf": "Bitopi Group IT Policy Book", "b.pdf": "Bitopi Group IT SOP Manual"}
    assert superseded_sources(titles) == set()


def test_three_revisions_only_newest_survives():
    titles = {"a.pdf": "Manual v1", "b.pdf": "Manual v2", "c.pdf": "Manual v3"}
    assert superseded_sources(titles) == {"a.pdf", "b.pdf"}


def test_fractional_revision_numbers():
    titles = {"a.pdf": "Handbook v1.2", "b.pdf": "Handbook v1.10"}
    assert superseded_sources(titles) == {"a.pdf"}


def test_idempotent_on_repeated_call():
    titles = {"a.pdf": "Doc v1", "b.pdf": "Doc v2"}
    assert superseded_sources(titles) == superseded_sources(titles)
