"""I5/H1 (docs/PLAN.md M7): the embedding backend is recorded next to embed_model and checked on open.
One backend per index; the cache is keyed by (hash, model, backend); st-fp32 keys stay the plain model
name, so the live index and its cache need no migration and are never rewritten."""
import json

import chromadb
import numpy as np
import pytest

import ragbot.data.schema_index as si
import ragbot.embed as embed
from _ingest_fakes import make_index
from ragbot.data.catalog import Catalog, Table
from ragbot.ingest.pipeline import cache_key, embed_with_cache
from ragbot.store import ChromaStore, Registry, check_index_metadata, text_hash


@pytest.fixture
def backend(monkeypatch):
    """set(value) as if EMBED_BACKEND=value were in .env (read at call time, not import time)."""
    monkeypatch.setattr(embed, "EMBED_BACKEND", "sentence_transformers")
    return lambda value: monkeypatch.setattr(embed, "EMBED_BACKEND", value)


# ---- labels and cache keys ------------------------------------------------------------------------------
@pytest.mark.parametrize("value, label", [("sentence_transformers", "st-fp32"), ("onnx", "onnx-int8"),
                                          ("ollama", "ollama"), ("", "st-fp32"), ("anything-else", "st-fp32"),
                                          # exact, as the Embedder always compared: these ran sentence-transformers
                                          ("ONNX", "st-fp32"), ("Ollama", "st-fp32"), ("onnx ", "st-fp32")])
def test_backend_label_follows_what_the_embedder_runs(value, label):
    assert embed.backend_label(value) == label


def test_backend_label_reads_embed_backend(backend):
    assert embed.backend_label() == "st-fp32"
    backend("onnx")
    assert embed.backend_label() == "onnx-int8"


def test_cache_model_keeps_the_plain_name_for_st_fp32():
    assert embed.cache_model("BAAI/bge-m3", "st-fp32") == "BAAI/bge-m3"     # every existing row still hits
    assert embed.cache_model("BAAI/bge-m3", "onnx-int8") == "BAAI/bge-m3|onnx-int8"
    assert embed.cache_model("BAAI/bge-m3", "ollama") == "BAAI/bge-m3|ollama"


class _Emb:
    def __init__(self, value):
        self.value, self.calls = value, 0

    def embed(self, texts):
        self.calls += len(texts)
        return [[self.value, 0.5] for _ in texts]


def _cached(reg, emb, texts):
    counts = {"embedded": 0, "cached": 0}
    return embed_with_cache(texts, reg, emb, counts), counts


def test_existing_cache_rows_hit_under_st_fp32_and_never_under_another_backend(tmp_path, backend):
    reg, model, h = Registry(tmp_path / "r.db"), embed.EMBED_MODEL, text_hash("old chunk")
    reg.store_vectors([(h, [0.25, 0.5])], model)             # a row cached before H1: the plain model name
    assert cache_key() == model
    vecs, c = _cached(reg, _Emb(1.0), ["old chunk"])
    assert c == {"embedded": 0, "cached": 1} and vecs == [[0.25, 0.5]]

    backend("onnx")                                          # an int8 vector must never come from an fp32 row
    int8 = _Emb(2.0)
    vecs, c = _cached(reg, int8, ["old chunk"])
    assert c == {"embedded": 1, "cached": 0} and vecs == [[2.0, 0.5]] and int8.calls == 1
    assert reg.cached_vectors([h], f"{model}|onnx-int8") == {h: [2.0, 0.5]}

    backend("sentence_transformers")                         # and the fp32 row is untouched
    assert reg.cached_vectors([h], model) == {h: [0.25, 0.5]}


# ---- the vector store ----------------------------------------------------------------------------------
def _legacy_index(path):
    """A collection exactly like the live index: created before H1, so it records only embed_model."""
    col = chromadb.PersistentClient(path=str(path)).create_collection(
        "chunks", configuration={"hnsw": {"space": "cosine"}}, metadata={"embed_model": embed.EMBED_MODEL},
        embedding_function=None)
    col.add(ids=["a#p1#c1"], embeddings=[[1.0, 0.0]], documents=["text"], metadatas=[{"source": "a.pdf"}])


def _stored(path):
    return chromadb.PersistentClient(path=str(path)).get_collection("chunks", embedding_function=None).metadata


def test_chroma_ignores_metadata_when_reopening_an_existing_collection(tmp_path):
    """Pins Chroma's behaviour that ChromaStore relies on: get_or_create_collection(metadata=...) on an
    existing collection must not overwrite what is stored. If an upgrade changes this, ChromaStore would
    rewrite the live index's metadata (and the embed_model check would compare the config with itself)."""
    _legacy_index(tmp_path)
    again = chromadb.PersistentClient(path=str(tmp_path)).get_or_create_collection(
        "chunks", configuration={"hnsw": {"space": "cosine"}},
        metadata={"embed_model": "another/model", "embed_backend": "onnx-int8"}, embedding_function=None)
    assert again.metadata == {"embed_model": embed.EMBED_MODEL}
    assert _stored(tmp_path) == {"embed_model": embed.EMBED_MODEL}


def test_a_new_collection_records_model_and_backend(tmp_path, backend):
    ChromaStore(tmp_path / "fp32")
    assert _stored(tmp_path / "fp32") == {"embed_model": embed.EMBED_MODEL, "embed_backend": "st-fp32"}
    backend("onnx")
    assert ChromaStore(tmp_path / "int8").embed_backend == "onnx-int8"
    assert _stored(tmp_path / "int8") == {"embed_model": embed.EMBED_MODEL, "embed_backend": "onnx-int8"}


def test_an_index_without_embed_backend_opens_as_st_fp32_and_is_left_unchanged(tmp_path, backend):
    _legacy_index(tmp_path)
    s = ChromaStore(tmp_path)
    assert s.count() == 1 and s.embed_backend == "st-fp32"
    assert _stored(tmp_path) == {"embed_model": embed.EMBED_MODEL}          # nothing was added to it


def test_an_index_without_embed_backend_is_refused_under_onnx_int8(tmp_path, backend):
    _legacy_index(tmp_path)
    with pytest.raises(RuntimeError, match="embed_backend='st-fp32'.*'onnx-int8'"):
        ChromaStore(tmp_path, embed_backend="onnx-int8")
    backend("onnx")                                          # the same through EMBED_BACKEND
    with pytest.raises(RuntimeError, match="reindex"):
        ChromaStore(tmp_path)
    assert _stored(tmp_path) == {"embed_model": embed.EMBED_MODEL}


def test_an_int8_index_is_refused_under_st_fp32(tmp_path):
    ChromaStore(tmp_path, embed_backend="onnx-int8")
    with pytest.raises(RuntimeError, match="embed_backend='onnx-int8'"):
        ChromaStore(tmp_path, embed_backend="st-fp32")
    assert ChromaStore(tmp_path, embed_backend="onnx-int8").count() == 0


def test_check_index_metadata():
    check_index_metadata({}, "BAAI/bge-m3", "st-fp32")                     # a brand-new empty metadata
    check_index_metadata({"embed_model": "BAAI/bge-m3"}, "BAAI/bge-m3", "st-fp32")
    check_index_metadata({"embed_model": "BAAI/bge-m3", "embed_backend": "onnx-int8"}, "BAAI/bge-m3", "onnx-int8")
    with pytest.raises(RuntimeError, match="embed_model"):
        check_index_metadata({"embed_model": "other/model"}, "BAAI/bge-m3", "st-fp32")
    with pytest.raises(RuntimeError, match="embed_backend"):
        check_index_metadata({"embed_model": "BAAI/bge-m3"}, "BAAI/bge-m3", "ollama")


# ---- ingest_state.json and the schema vector files -----------------------------------------------------------
def test_ingest_state_records_the_backend(tmp_path, monkeypatch, backend):
    root, reg, store, run, files = make_index(tmp_path, monkeypatch)
    files("alpha")
    run()
    state = json.loads((tmp_path / "index" / "ingest_state.json").read_text(encoding="utf-8"))
    assert state["embed_model"] == embed.EMBED_MODEL and state["embed_backend"] == "st-fp32"


def _npz(path, sha="sha-1", names=("dbo.A", "dbo.B"), **extra):
    np.savez_compressed(path, names=np.array(list(names)), vectors=np.eye(2, dtype=np.float32), sha=sha,
                        model=embed.EMBED_MODEL, **extra)
    return path


def test_a_schema_file_without_embed_backend_counts_as_st_fp32(tmp_path, backend):
    p = _npz(tmp_path / "Demo.npz")                          # written before H1
    assert si.load_vectors(p, "sha-1", ["dbo.A", "dbo.B"]) is not None
    assert si.load_vectors(p, "sha-1", ["dbo.A", "dbo.B"], backend="onnx-int8") is None
    backend("onnx")
    assert si.load_vectors(p, "sha-1", ["dbo.A", "dbo.B"]) is None


def test_schema_vectors_of_another_backend_or_discovery_are_ignored(tmp_path, backend):
    p = _npz(tmp_path / "Demo.npz", embed_backend="onnx-int8")
    assert si.load_vectors(p, "sha-1", ["dbo.A", "dbo.B"]) is None
    assert si.load_vectors(p, "sha-1", ["dbo.A", "dbo.B"], backend="onnx-int8") is not None
    assert si.load_vectors(p, "sha-2", ["dbo.A", "dbo.B"], backend="onnx-int8") is None     # stale discovery
    assert si.load_vectors(p, "sha-1", ["dbo.A"], backend="onnx-int8") is None              # other tables
    assert si.load_vectors(tmp_path / "missing.npz", "sha-1", ["dbo.A"]) is None


def test_schema_build_records_the_backend(tmp_path, monkeypatch, backend):
    from types import SimpleNamespace as NS
    monkeypatch.setattr(si, "vectors_path", lambda db: tmp_path / f"{db}.npz")
    monkeypatch.setattr(si, "json_sha", lambda cat: "sha-1")
    monkeypatch.setattr(embed, "get_embedder", lambda: NS(embed=lambda texts: [[1.0, 0.0] for _ in texts]))
    cat = Catalog("Demo", "tsql", "X", [], [], tables=[Table("dbo.Supplier", "table", 1, "", [], [{"name": "Id", "type": "int"}], [])])
    si.build(cat, embed=True)
    with np.load(tmp_path / "Demo.npz") as z:
        assert str(z["embed_backend"]) == "st-fp32" and str(z["model"]) == embed.EMBED_MODEL
