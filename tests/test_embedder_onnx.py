"""I5/H2: the pure parts of the int8 ONNX embedder and its parity check — CLS pooling, cosine statistics,
which texts are compared, the hit-rate loop shared with scripts/hit_rate.py, and that EMBED_BACKEND=onnx
is the only way to the ONNX embedder. No model is loaded (the export and --check are run by hand)."""
import math

import numpy as np
import pytest

import ragbot.embed as embed
from ragbot import embed_parity as par
from ragbot.retrieve.hit_rate import HitRate, hit_rate, load_cases, wanted


# ---- CLS pooling ----------------------------------------------------------------------------------------
def test_cls_pool_takes_the_first_token_and_normalises():
    hidden = np.zeros((2, 3, 4), dtype=np.float32)          # 2 texts x 3 tokens x 4 dims
    hidden[0, 0] = [3.0, 4.0, 0.0, 0.0]                      # [CLS] of text 1
    hidden[0, 1:] = 100.0                                    # the other tokens must not count (no mean pooling)
    hidden[1, 0] = [0.0, 0.0, 0.0, 2.0]
    hidden[1, 2] = [9.0, 9.0, 9.0, 9.0]                      # a padding position, say
    got = embed.cls_pool(hidden)
    assert got.dtype == np.float32 and got.shape == (2, 4)
    np.testing.assert_allclose(got, [[0.6, 0.8, 0.0, 0.0], [0.0, 0.0, 0.0, 1.0]], atol=1e-7)


def test_cls_pool_of_a_zero_vector_does_not_divide_by_zero():
    assert np.isfinite(embed.cls_pool(np.zeros((1, 2, 3)))).all()


# ---- cosine statistics ----------------------------------------------------------------------------------
def test_rowwise_cosine():
    a = np.array([[1.0, 0.0], [1.0, 0.0], [2.0, 2.0], [1.0, 0.0]])
    b = np.array([[1.0, 0.0], [0.0, 3.0], [1.0, 1.0], [-1.0, 0.0]])
    np.testing.assert_allclose(par.rowwise_cosine(a, b), [1.0, 0.0, 1.0, -1.0], atol=1e-12)   # scale-free
    with pytest.raises(ValueError):
        par.rowwise_cosine(a, b[:2])


def test_cosine_stats():
    cos = np.linspace(0.9, 1.0, 101)                         # 0.900, 0.901, ... 1.000
    s = par.cosine_stats(cos)
    assert s["n"] == 101 and s["min"] == pytest.approx(0.9) and s["mean"] == pytest.approx(0.95)
    assert s["p1"] == pytest.approx(0.901)
    empty = par.cosine_stats([])
    assert empty["n"] == 0 and math.isnan(empty["min"]) and math.isnan(empty["mean"])


def test_parity_lines_split_by_length_and_kind():
    cos, tokens = [0.999, 0.98, 0.995, 0.97], [100, 600, 512, 513]
    lines = par.parity_lines(cos, tokens, ["chunk", "chunk", "schema", "question"])
    assert lines[0].startswith("all texts") and "n=    4" in lines[0] and "min 0.97000" in lines[0]
    assert lines[1].startswith("<= 512 tokens") and "n=    2" in lines[1] and "min 0.99500" in lines[1]
    assert lines[2].startswith("> 512 tokens") and "n=    2" in lines[2] and "min 0.97000" in lines[2]
    assert [ln[:24].strip() for ln in lines[3:]] == ["chunks", "table documents", "questions"]
    assert "n=    2" in lines[3] and "min 0.98000" in lines[3]


def test_even_sample_and_plan_selection():
    assert par.even_sample(10, 4) == [0, 2, 5, 7]
    assert par.even_sample(3, 10) == [0, 1, 2] and par.even_sample(5, 0) == []
    # every chunk always; then questions; then table documents spread over all databases, to the limit
    assert par.plan_selection(250, 130, 3700, 1000) == (130, par.even_sample(3700, 620))
    assert par.plan_selection(10, 5, 4, 1000) == (5, [0, 1, 2, 3])
    # a large index never crowds out what the app embeds every day: a quarter of the limit, half of it for tables
    assert par.plan_selection(990, 130, 3700, 1000) == (125, par.even_sample(3700, 125))
    assert par.plan_selection(5000, 130, 3700, 1000) == (125, par.even_sample(3700, 125))
    assert par.plan_selection(5000, 130, 0, 1000) == (130, [])
    assert par.plan_selection(5000, 40, 3700, 1000) == (40, par.even_sample(3700, 210))


# ---- the hit-rate loop (scripts/hit_rate.py and the parity check) ----------------------------------------
CASES = [
    {"q": "who approves", "source": "a.pdf", "page": 3},
    {"q": "retention", "source": "a.pdf", "page": 7, "accept": [["b.pdf", 2]]},
    {"q": "laptop spec", "source": "b.pdf", "page": 9},
]


def test_hit_rate_on_a_fake_retriever():
    found = {"who approves": [("a.pdf", 1), ("a.pdf", 3)],
             "retention": [("b.pdf", 2)],                                  # an accepted alternative page
             "laptop spec": [("b.pdf", p) for p in range(1, 7)]}          # page 9 not among them
    r = hit_rate(CASES, lambda q: found[q])
    assert (r.hits, r.total) == (2, 3)
    assert r.misses == [("laptop spec", [("b.pdf", 9)], [("b.pdf", 1), ("b.pdf", 2), ("b.pdf", 3), ("b.pdf", 4)])]
    assert r.lines() == ["hit rate: 2/3 = 67%",                          # what scripts/hit_rate.py printed
                         "  MISS  want [('b.pdf', 9)]  got [('b.pdf', 1), ('b.pdf', 2), ('b.pdf', 3), ('b.pdf', 4)]"
                         "  <- laptop spec"]


def test_hit_rate_edge_cases(tmp_path):
    assert HitRate(0, 0).summary() == "hit rate: 0/0 = 0%"
    assert wanted({"q": "x", "accept": [["c.pdf", "4"]]}) == {("c.pdf", 4)}
    f = tmp_path / "cases.jsonl"
    f.write_text('{"q": "Bangla: কোড", "source": "a.pdf", "page": 1}\n\n', encoding="utf-8")
    assert load_cases(f) == [{"q": "Bangla: কোড", "source": "a.pdf", "page": 1}]


# ---- backend selection ------------------------------------------------------------------------------------
def test_only_embed_backend_onnx_selects_the_onnx_embedder(monkeypatch):
    monkeypatch.setattr(embed, "Embedder", lambda: "sentence-transformers")
    monkeypatch.setattr(embed, "OnnxEmbedder", lambda: "onnx int8")
    try:
        for value, want in (("sentence_transformers", "sentence-transformers"), ("ollama", "sentence-transformers"),
                            ("onnx", "onnx int8")):
            monkeypatch.setattr(embed, "EMBED_BACKEND", value)
            embed.get_embedder.cache_clear()
            assert embed.get_embedder() == want
    finally:
        embed.get_embedder.cache_clear()
    assert embed.backend_label("onnx") == "onnx-int8"
    assert embed.cache_model(embed.EMBED_MODEL, "onnx-int8") == f"{embed.EMBED_MODEL}|onnx-int8"


def test_the_onnx_embedder_refuses_a_missing_export(tmp_path):
    with pytest.raises(RuntimeError, match="export_embedder_onnx"):
        embed.OnnxEmbedder(tmp_path)


# ---- --check: the int8 copy and the backend it needs (review of 2026-10-15) ------------------------------
@pytest.fixture
def parity_script(monkeypatch):
    """scripts/export_embedder_onnx.py, loaded fresh (nothing runs at import)."""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / "scripts"))           # for its `import _path`
    spec = importlib.util.spec_from_file_location("export_embedder_onnx_under_test",
                                                  root / "scripts" / "export_embedder_onnx.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_int8_copy_holds_exactly_the_compared_chunks(parity_script, tmp_path):
    """A chunk the ingest worker adds or removes during --check neither breaks the copy nor joins it."""
    from ragbot.store import ChromaStore
    live = ChromaStore(path=tmp_path / "live", embed_backend="st-fp32")
    x = [1.0] + [0.0] * 1023
    live.col.upsert(ids=["a#p1#c1", "b#p1#c1", "new#p1#c1"], documents=["A", "B", "N"],
                    metadatas=[{"source": "a.pdf"}, {"source": "b.pdf"}, {"source": "new.pdf"}], embeddings=[x] * 3)
    compared = [par.ParityText("chunk", "a#p1#c1", "A", None), par.ParityText("chunk", "b#p1#c1", "B", None),
                par.ParityText("chunk", "gone#p1#c1", "G", None)]
    y = np.asarray([0.0, 1.0] + [0.0] * 1022, dtype=np.float32)
    copy, gone = parity_script._int8_copy(live, compared, {t.key: y for t in compared}, tmp_path / "copy")
    assert gone == 1                                                # removed from the live index meanwhile
    got = copy.col.get(include=["documents", "metadatas", "embeddings"])
    assert sorted(got["ids"]) == ["a#p1#c1", "b#p1#c1"]             # not the chunk added meanwhile
    by_id = dict(zip(got["ids"], zip(got["documents"], got["metadatas"], got["embeddings"])))
    assert by_id["a#p1#c1"][0] == "A" and by_id["a#p1#c1"][1] == {"source": "a.pdf"}
    assert by_id["a#p1#c1"][2][1] == pytest.approx(1.0)              # the int8 vector, not the live one
    assert copy.col.metadata["embed_backend"] == "onnx-int8"
    assert live.count() == 3 and live.col.metadata["embed_backend"] == "st-fp32"   # the live index is untouched


def test_check_refuses_another_backend_before_opening_anything(parity_script, monkeypatch):
    """The keyword index opens the live store through store.get_store(), which refuses an st-fp32 index under
    EMBED_BACKEND=onnx: --check stops at once instead of half-way through a 30-minute run."""
    monkeypatch.setattr(embed, "EMBED_BACKEND", "onnx")
    monkeypatch.setattr("ragbot.store.ChromaStore", lambda *a, **k: pytest.fail("the index was opened"))
    with pytest.raises(SystemExit, match="EMBED_BACKEND unset"):
        parity_script.check(1000, 100, None, True)
