"""D1: keyword search on SQLite FTS5 (chunk_fts in registry.db), maintained per document."""
from ragbot.models import Chunk
from ragbot.retrieve.keyword import FtsKeywordIndex
from ragbot.store import Registry


def _c(cid, text, source="SOP.pdf", category="SOP", superseded=False):
    return Chunk(id=cid, text=text, source=source, title=source[:-4], page=1, category=category, superseded=superseded)


def _setup(tmp_path):
    reg = Registry(tmp_path / "registry.db")
    reg.replace_chunks("SOP.pdf", [
        _c("SOP#p4#c1", "SOP › 3.2\nApproval on form PCD-02 by the Planning Head within 2 days."),
        _c("SOP#p5#c1", "SOP › 3.3\nThe Planning Head signs the approval."),
        _c("SOP#p6#c1", "SOP › 4.1\nকাপড় রোল পরিদর্শন প্রতিদিন হয়।"),          # Bangla: fabric roll inspection
    ])
    reg.replace_chunks("IT.pdf", [_c("IT#p9#c1", "Form IT-03 is the laptop requisition.", "IT.pdf", "IT")])
    return reg, FtsKeywordIndex(tmp_path / "registry.db")


def test_exact_codes_match(tmp_path):
    _, kw = _setup(tmp_path)
    assert kw.search("which form is PCD-02?", 3)[0][0] == "SOP#p4#c1"
    assert kw.search("IT-03", 3)[0][0] == "IT#p9#c1"


def test_bangla_word_matches(tmp_path):
    _, kw = _setup(tmp_path)
    assert [cid for cid, _ in kw.search("কাপড়", 3)] == ["SOP#p6#c1"]


def test_scores_are_positive_and_ranked(tmp_path):
    _, kw = _setup(tmp_path)
    hits = kw.search("planning head approval", 5)
    assert hits[0][0] in ("SOP#p4#c1", "SOP#p5#c1") and all(s > 0 for _, s in hits)
    assert [s for _, s in hits] == sorted((s for _, s in hits), reverse=True)


def test_replacing_a_document_updates_the_index(tmp_path):
    reg, kw = _setup(tmp_path)
    reg.replace_chunks("SOP.pdf", [_c("SOP#p4#c1", "SOP › 3.2\nApproval on form PCD-07 now.")])
    assert kw.search("within days", 5) == []                          # words only the old text had
    assert kw.search("PCD-07", 5)[0][0] == "SOP#p4#c1"
    assert not kw.search("কাপড়", 5)                                    # old chunk gone


def test_removed_document_leaves_the_index(tmp_path):
    reg, kw = _setup(tmp_path)
    reg.remove_document("IT.pdf")
    assert kw.search("IT-03 laptop", 5) == []


def test_filters_are_applied_inside_the_search(tmp_path):
    reg, kw = _setup(tmp_path)
    assert {c for c, _ in kw.search("form", 5, {"category": ["IT"]})} == {"IT#p9#c1"}
    reg.set_superseded("SOP.pdf", True)
    assert all(not c.startswith("SOP") for c, _ in kw.search("approval form", 5, {"superseded": False}))
    assert any(c.startswith("SOP") for c, _ in kw.search("approval form", 5))


def test_question_with_fts_syntax_is_safe(tmp_path):
    _, kw = _setup(tmp_path)
    assert kw.search('"approval" AND NOT (form) OR * -- ; DROP', 3)          # quotes/operators never break it
    assert kw.search("???", 3) == []


def test_pre_d1_registry_is_seeded_from_the_store(tmp_path):
    reg = Registry(tmp_path / "registry.db")
    reg.upsert_document(source="SOP.pdf", title="SOP", category="SOP", superseded=1, status="ok")
    reg.db.execute("INSERT INTO chunk(id, source, page, section, kind, chars) VALUES('SOP#p1#c1','SOP.pdf',1,'s','text',9)")
    reg.db.commit()

    class Store:
        def all_ids_and_texts(self):
            yield "SOP#p1#c1", "Approval on form PCD-02."

    assert reg.seed_fts(Store()) == 1 and reg.seed_fts(Store()) == 0
    kw = FtsKeywordIndex(tmp_path / "registry.db")
    assert kw.search("PCD-02", 3)[0][0] == "SOP#p1#c1"
    assert kw.search("PCD-02", 3, {"superseded": False}) == []           # superseded flag carried over


def test_stopwords_do_not_decide_the_match(tmp_path):
    reg, kw = _setup(tmp_path)
    # "what is the ..." alone would match everything; the content word decides
    assert kw.search("what is the laptop form", 5)[0][0] == "IT#p9#c1"
    assert kw.search("what is the", 5)          # only stopwords: still searched, not an empty result
