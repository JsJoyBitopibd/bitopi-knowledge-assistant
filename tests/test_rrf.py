from ragbot.retrieve.hybrid import rrf

def test_items_in_both_lists_rank_first():
    assert rrf([["a", "b", "c"], ["c", "a", "d"]])[:2] == ["a", "c"]
