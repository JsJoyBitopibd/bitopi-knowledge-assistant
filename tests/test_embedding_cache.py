"""E3: embedding cache — only new or changed chunk texts are embedded; cache keyed by model; pruning;
registries from before E3 are migrated."""
import sqlite3

from ragbot.ingest.pipeline import embed_with_cache
from ragbot.models import Chunk
from ragbot.store import Registry, text_hash


class CountingEmbedder:
    def __init__(self):
        self.seen = []

    def embed(self, texts):
        self.seen += texts
        return [[float(len(t)), 0.5, 0.25] for t in texts]    # exactly representable in float32


def _run(reg, emb, texts):
    counts = {"embedded": 0, "cached": 0}
    return embed_with_cache(texts, reg, emb, counts), counts


def test_second_ingest_of_same_text_embeds_nothing(tmp_path):
    reg, emb = Registry(tmp_path / "r.db"), CountingEmbedder()
    texts = ["SOP › 3.2\nApproval within 2 days.", "SOP › 3.3\nThe Planning Head signs.", "Form IT-03"]
    v1, c1 = _run(reg, emb, texts)
    v2, c2 = _run(reg, emb, texts)
    assert c1 == {"embedded": 3, "cached": 0} and c2 == {"embedded": 0, "cached": 3}
    assert v1 == v2 and len(emb.seen) == 3


def test_revised_document_embeds_only_changed_chunks(tmp_path):
    reg, emb = Registry(tmp_path / "r.db"), CountingEmbedder()
    _run(reg, emb, [f"page {i} text" for i in range(40)])
    revised = [f"page {i} text" for i in range(40)]
    revised[7] = "page 7 text, amended in revision 3"
    _, c = _run(reg, emb, revised)
    assert c == {"embedded": 1, "cached": 39} and emb.seen[-1] == revised[7]


def test_duplicate_texts_in_a_batch_are_embedded_once(tmp_path):
    reg, emb = Registry(tmp_path / "r.db"), CountingEmbedder()
    vecs, c = _run(reg, emb, ["same header", "same header", "other"])
    assert len(emb.seen) == 2 and vecs[0] == vecs[1] and c["embedded"] == 2


def test_cache_is_per_model(tmp_path):
    reg = Registry(tmp_path / "r.db")
    reg.store_vectors([(text_hash("x"), [1.0, 2.0])], "BAAI/bge-m3")
    assert reg.cached_vectors([text_hash("x")], "BAAI/bge-m3") == {text_hash("x"): [1.0, 2.0]}
    assert reg.cached_vectors([text_hash("x")], "another-model") == {}


def test_prune_drops_rows_no_chunk_uses(tmp_path):
    reg = Registry(tmp_path / "r.db")
    keep = Chunk(id="d#p1#c1", text="kept text", source="d.pdf", title="d", page=1)
    reg.replace_chunks("d.pdf", [keep])
    reg.store_vectors([(text_hash("kept text"), [1.0]), (text_hash("old revision text"), [2.0])], "m")
    assert reg.prune_embedding_cache() == 1
    assert set(reg.cached_vectors([text_hash("kept text"), text_hash("old revision text")], "m")) == {text_hash("kept text")}


def test_pre_e3_registry_is_migrated(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE chunk(id TEXT PRIMARY KEY, source TEXT, page INTEGER, section TEXT, kind TEXT, chars INTEGER)")
    con.execute("INSERT INTO chunk VALUES('a#p1#c1', 'a.pdf', 1, 's', 'text', 10)")
    con.commit(); con.close()
    reg = Registry(db)
    cols = {r[1] for r in reg.db.execute("PRAGMA table_info(chunk)")}
    assert "text_hash" in cols and reg.db.execute("SELECT COUNT(*) FROM chunk").fetchone()[0] == 1
