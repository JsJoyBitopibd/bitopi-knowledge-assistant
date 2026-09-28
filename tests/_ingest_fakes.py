"""Shared fakes for tests that run the real ingest pipeline on generated PDFs: an in-memory vector store,
an embedder that never loads a model, and a throwaway index."""
import pymupdf

from ragbot.ingest import pipeline
from ragbot.store import Registry


class FakeStore:
    def __init__(self):
        self.rows = {}                                       # chunk id -> Chunk

    def upsert(self, chunks, vectors):
        self.rows.update({c.id: c.model_copy() for c in chunks})

    def delete_by_source(self, source):
        self.rows = {k: c for k, c in self.rows.items() if c.source != source}

    def set_superseded(self, source, superseded):
        self.set_attributes(source, {"superseded": superseded})

    def set_attributes(self, source, attrs):
        for c in self.rows.values():
            if c.source == source:
                for k, v in attrs.items():
                    setattr(c, k, v)

    def all_ids_and_texts(self):
        return [(k, c.text) for k, c in self.rows.items()]

    def sources(self):
        return {c.source for c in self.rows.values()}


class FakeEmbedder:
    def embed(self, texts):
        return [[float(len(t)), 1.0] for t in texts]


def make_pdf(path, word):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for n in range(2):
        page = doc.new_page()
        body = f"{n + 1}. Section {word} {n}\n" + (f"The {word} procedure step {n} is approved within two days. " * 14)
        page.insert_textbox(pymupdf.Rect(50, 60, 550, 780), body, fontsize=10)
    doc.save(path)
    doc.close()


def make_index(tmp_path, monkeypatch):
    """(root, registry, store, run, files): run(**kw) ingests root into a throwaway index; files(*names,
    folder="SOP") writes generated PDFs."""
    monkeypatch.setattr(pipeline, "get_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(pipeline, "log_dir", lambda: tmp_path)
    root, idx = tmp_path / "pdfs", tmp_path / "index"
    idx.mkdir()
    reg, store = Registry(idx / "registry.db"), FakeStore()

    def run(**kw):
        return pipeline.ingest_folder(root, store=store, reg=reg, index_dir=idx, **kw)

    def files(*names, folder="SOP"):
        for n in names:
            make_pdf(root / folder / f"{n}.pdf", n)

    return root, reg, store, run, files
