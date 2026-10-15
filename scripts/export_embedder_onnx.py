"""Export the embedding model (bge-m3) to ONNX and quantize it to int8 (I5, docs/PLAN.md M7), and check the
int8 vectors against the stored fp32 ones before any index is switched.

Writes data/models/<name>-int8/ (model.onnx, tokenizer files, embedder.json): what EMBED_BACKEND=onnx loads
(src/ragbot/embed.py:OnnxEmbedder). The unquantized export stays in data/models/<name>-fp32/ (~2.3 GB) for the
--check sanity comparison; delete it once --check has run. Same recipe as scripts/export_reranker_onnx.py:
torch.onnx.export (opset 17) + onnxruntime quantize_dynamic (QInt8), and deliberately not `optimum` (it pins
an older transformers). The graph outputs the last hidden state; OnnxEmbedder takes the [CLS] token and
L2-normalises it, as sentence-transformers does for bge-m3. The maximum length is the sentence-transformers
model's max_seq_length, recorded in embedder.json (not hard-coded).

An index is switched to onnx-int8 only with the user's approval, after --check shows cosine >= 0.99 against
the stored fp32 vectors and a hit-rate drop <= 1 point (with 30 retrieval cases: no case lost), and then by
scripts/reindex.py and scripts/index_schema.py: store.py refuses an st-fp32 index under EMBED_BACKEND=onnx.
The app and the ingest-worker both embed, so both mount ./data/models (docker-compose.yml). Record the result
in docs/tuning_log.md.

--check runs with EMBED_BACKEND unset (sentence_transformers) and refuses otherwise: it reads the live st-fp32
index, and retrieval opens the keyword index through the app's own store.get_store(), which would refuse it.

Usage: python scripts/export_embedder_onnx.py           export (minutes; ~5 GB of memory)
       python scripts/export_embedder_onnx.py --check   parity; reads the live index, never writes it
           [--limit 1000]      texts compared: every chunk, then golden/retrieval questions, then table documents
                               (those two always get at least a quarter of the limit, however many chunks)
           [--sanity 100]      texts for the fp32 sanity comparisons
           [--k N]             top-k of the hit-rate runs (default retrieval.final_top_k)
           [--no-hit-rate]     cosines only
"""
import argparse, json, tempfile, time, _path  # noqa: F401
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- export
def export_onnx(model, input_ids, attention_mask, path: Path) -> None:
    """Export a transformers encoder (AutoModel) to ONNX: inputs input_ids + attention_mask, one output
    last_hidden_state (batch, seq, dims), dynamic batch and sequence axes, opset 17."""
    import torch

    class LastHidden(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, input_ids, attention_mask):
            return self.m(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state

    with torch.no_grad():
        torch.onnx.export(LastHidden(model).eval(), (input_ids, attention_mask), str(path),
                          input_names=["input_ids", "attention_mask"], output_names=["last_hidden_state"],
                          dynamic_axes={"input_ids": {0: "batch", 1: "seq"}, "attention_mask": {0: "batch", 1: "seq"},
                                        "last_hidden_state": {0: "batch", 1: "seq"}},
                          opset_version=17, dynamo=False)


def export() -> Path:
    import shutil
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from sentence_transformers import SentenceTransformer
    from transformers import AutoModel, AutoTokenizer
    from ragbot.embed import EMBED_MODEL, ONNX_EMBEDDER_CONFIG, onnx_embedder_dir
    dest, fp32 = onnx_embedder_dir("int8"), onnx_embedder_dir("fp32")
    if fp32.exists():
        shutil.rmtree(fp32)         # the fp32 weights are external-data files: none may be left from an earlier export
    fp32.mkdir(parents=True); dest.mkdir(parents=True, exist_ok=True)
    st = SentenceTransformer(EMBED_MODEL, device="cpu")
    max_length = int(st.max_seq_length)          # what sentence-transformers truncates to
    del st
    tok = AutoTokenizer.from_pretrained(EMBED_MODEL)
    model = AutoModel.from_pretrained(EMBED_MODEL).eval()
    enc = tok(["a short passage", "a second, somewhat longer passage of text"], padding=True, return_tensors="pt")
    t0 = time.perf_counter()
    export_onnx(model, enc["input_ids"], enc["attention_mask"], fp32 / "model.onnx")
    print(f"exported fp32 ONNX in {time.perf_counter() - t0:.0f} s -> {fp32}")
    del model
    t0 = time.perf_counter()
    quantize_dynamic(str(fp32 / "model.onnx"), str(dest / "model.onnx"), weight_type=QuantType.QInt8)
    print(f"quantized to int8 in {time.perf_counter() - t0:.0f} s -> {dest / 'model.onnx'} "
          f"({(dest / 'model.onnx').stat().st_size / 1e6:.0f} MB)")
    cfg = {"model": EMBED_MODEL, "max_length": max_length, "pooling": "cls", "normalize": True}
    for d, precision in ((dest, "int8"), (fp32, "fp32")):
        tok.save_pretrained(d)
        (d / ONNX_EMBEDDER_CONFIG).write_text(json.dumps({**cfg, "precision": precision}, indent=1), encoding="utf-8")
    print(f"max length {max_length} (sentence-transformers max_seq_length). The fp32 export in {fp32} is only "
          "for --check: delete it afterwards.")
    return dest


# ---------------------------------------------------------------- --check
def _questions() -> list[str]:
    """Golden and retrieval questions, each once, in file order."""
    from ragbot.retrieve.hit_rate import load_cases
    qs = [c["q"] for f in ("golden.jsonl", "retrieval_cases.jsonl") for c in load_cases(ROOT / "tests" / f)]
    return list(dict.fromkeys(qs))


def _schema_documents():
    """(ParityText per offered table with its stored vector, databases skipped): a database whose .npz is
    missing or stale (other discovery, model, backend or tables) has no vectors to compare with."""
    import numpy as np
    from ragbot.data import schema_index as si
    from ragbot.data.catalog import load_catalogs
    from ragbot.embed_parity import ParityText
    out, skipped = [], []
    for name, cat in sorted(load_catalogs().items()):
        tables = cat.offered_tables
        if not tables:
            continue
        vecs = si.load_vectors(si.vectors_path(cat.database), si.json_sha(cat), [t.name for t in tables],
                               backend="st-fp32")
        if vecs is None:
            skipped.append(name)
            continue
        out += [ParityText("schema", f"{name}:{t.name}", si.table_document(t), np.asarray(v, dtype=np.float32))
                for t, v in zip(tables, vecs)]
    return out, skipped


def _embed_each(emb, texts: list[str], label: str):
    """(vectors, seconds per text): one text at a time, as the CPU default batch size of 1 does."""
    import numpy as np
    vecs, secs, t_start = [], [], time.perf_counter()
    for i, t in enumerate(texts, start=1):
        a = time.perf_counter()
        vecs.append(emb.embed([t])[0])
        secs.append(time.perf_counter() - a)
        if i % 100 == 0 or i == len(texts):
            print(f"  {label}: {i}/{len(texts)} in {time.perf_counter() - t_start:.0f} s", flush=True)
    return np.asarray(vecs, dtype=np.float32).reshape(len(texts), -1), np.asarray(secs)


@contextmanager
def _retrieval_using(store, embedder):
    """retrieve() with this vector store and query embedder; the app's are restored afterwards. Only the
    retriever's own lookups are swapped: the keyword index still opens the live store (store.get_store), which
    holds the same chunk ids and texts as the int8 copy, and which check() makes sure opens (st-fp32)."""
    import ragbot.retrieve.retriever as rt
    saved = rt.get_store, rt.get_embedder
    rt.get_store, rt.get_embedder = (lambda: store), (lambda: embedder)
    rt._embed_query.cache_clear()
    try:
        yield
    finally:
        rt.get_store, rt.get_embedder = saved
        rt._embed_query.cache_clear()


def _int8_copy(live, chunks: list, vectors: dict, path: Path):
    """(a throwaway collection holding int8 vectors, chunks gone from the live index meanwhile): exactly the
    chunks read when --check started — their ids and texts, their metadata read now — so a chunk the
    ingest worker adds or removes during the run can neither break the copy nor change what is compared."""
    from ragbot.store import ChromaStore
    copy, gone = ChromaStore(path=path, embed_backend="onnx-int8"), 0
    for off in range(0, len(chunks), 1000):
        part = chunks[off:off + 1000]
        res = live.col.get(ids=[t.key for t in part], include=["metadatas"])
        meta = dict(zip(res["ids"], res["metadatas"]))
        keep = [t for t in part if t.key in meta]
        gone += len(part) - len(keep)
        if keep:
            copy.col.upsert(ids=[t.key for t in keep], documents=[t.text for t in keep],
                            metadatas=[meta[t.key] for t in keep], embeddings=[vectors[t.key].tolist() for t in keep])
    return copy, gone


def check(limit: int, sanity: int, k: int | None, with_hit_rate: bool) -> None:
    import numpy as np
    from ragbot import embed_parity as par
    from ragbot.embed import ST_FP32, Embedder, OnnxEmbedder, backend_label, onnx_embedder_dir
    from ragbot.ingest.priority import lower_priority
    from ragbot.retrieve.hit_rate import hit_rate, load_cases, retriever_search
    from ragbot.store import ChromaStore

    if backend_label() != ST_FP32:
        raise SystemExit(f"--check needs EMBED_BACKEND unset (sentence_transformers), not {backend_label()}: it reads "
                         "the live st-fp32 index, which the keyword index opens through store.get_store()")
    print(f"priority {lower_priority()}")
    live = ChromaStore(embed_backend="st-fp32")          # the reference: read here, never written
    chunks = [par.ParityText("chunk", cid, text, np.asarray(vec, dtype=np.float32))
              for cid, text, vec in live.all_texts_and_vectors()]
    if not chunks:
        print("the index holds no chunks: nothing to compare")
        return
    questions = _questions()
    schema, skipped = _schema_documents()
    n_q, s_idx = par.plan_selection(len(chunks), len(questions), len(schema), limit)
    items = chunks + [par.ParityText("question", f"q{i}", q, None) for i, q in enumerate(questions[:n_q], 1)] \
        + [schema[i] for i in s_idx]
    texts = [t.text for t in items]
    print(f"texts: {len(chunks)} chunks (all), {n_q} questions, {len(s_idx)} of {len(schema)} table documents"
          + (f"; no current vectors, skipped: {', '.join(skipped)}" if skipped else ""))

    int8 = OnnxEmbedder(onnx_embedder_dir("int8"))
    tokens = int8.token_counts(texts)
    v8, t8 = _embed_each(int8, texts, "int8")
    st = Embedder("sentence_transformers")
    stored = [i for i, t in enumerate(items) if t.ref is not None]
    asked = [i for i, t in enumerate(items) if t.ref is None]
    refs = np.zeros_like(v8)
    for i in stored:
        refs[i] = items[i].ref
    if asked:                                              # a question has no stored vector: fresh fp32 is its reference
        refs[asked] = _embed_each(st, [texts[i] for i in asked], "st-fp32 (questions)")[0]
    cos = par.rowwise_cosine(v8, refs)
    print("\nint8 vs fp32 (stored vectors; fresh fp32 for questions)")
    print("\n".join(par.parity_lines(cos, tokens, [t.kind for t in items])))

    # sanity: are the stored vectors what fp32 computes today, and is the export itself (before int8) exact?
    sub = [stored[j] for j in par.even_sample(len(stored), sanity)]
    if sub:
        v_st, t_st = _embed_each(st, [texts[i] for i in sub], "st-fp32 (sanity)")
        c_st = par.rowwise_cosine(v_st, refs[sub])
        print(f"\nsanity, {len(sub)} texts (expect >= {par.SAME_COSINE})")
        print(par.stats_line("fresh st-fp32 vs stored", c_st) + ("  ok" if c_st.min() >= par.SAME_COSINE else "  LOW"))
        fp32_dir = onnx_embedder_dir("fp32")
        if (fp32_dir / "model.onnx").exists():
            v_ox = _embed_each(OnnxEmbedder(fp32_dir), [texts[i] for i in sub], "onnx-fp32")[0]
            c_ox = par.rowwise_cosine(v_ox, v_st)
            print(par.stats_line("onnx-fp32 vs st-fp32", c_ox) + ("  ok" if c_ox.min() >= par.SAME_COSINE else "  LOW"))
        else:
            print(f"onnx-fp32 vs st-fp32: skipped, no {fp32_dir / 'model.onnx'} (run the export again to get it)")
        n_tok, s_st, s_8 = sum(tokens[i] for i in sub), max(t_st.sum(), 1e-9), max(t8[sub].sum(), 1e-9)
        print(f"speed on these texts: st-fp32 {n_tok / s_st:.0f} tokens/s, int8 {n_tok / s_8:.0f} tokens/s "
              f"({s_st / s_8:.1f}x)")

    # below the gate: an int8 problem, or a stored vector that no longer matches its text (e.g. a table note
    # edited after index_schema.py ran)? Fresh fp32 tells them apart.
    below = [int(i) for i in np.argsort(cos) if cos[i] < par.GATE_COSINE]
    shown = below[:50]
    recheck = [i for i in shown if items[i].ref is not None]
    fresh_cos: dict[int, float] = {}
    if recheck:
        fresh = _embed_each(st, [texts[i] for i in recheck], "st-fp32 (below the gate)")[0]
        fresh_cos = dict(zip(recheck, par.rowwise_cosine(fresh, refs[recheck])))
    stale = {i for i, cf in fresh_cos.items() if cf < par.SAME_COSINE}
    if shown:
        print(f"\nbelow {par.GATE_COSINE}: {len(below)} texts" + (", the worst 50:" if len(below) > 50 else ":"))
        for i in shown:
            print(f"  {cos[i]:.5f}  {tokens[i]:>5} tokens  {items[i].kind:<8} {items[i].key}"
                  + (f"  STALE stored vector (fresh fp32 vs stored {fresh_cos[i]:.5f})" if i in stale else ""))
    keep = [i for i in range(len(items)) if i not in stale]
    min_kept = float(cos[keep].min()) if keep else float("nan")

    rates, n_cases = {}, 0
    if with_hit_rate:
        cases = load_cases(ROOT / "tests" / "retrieval_cases.jsonl")
        n_cases = len(cases)
        vec_of = {t.key: v8[i] for i, t in enumerate(items) if t.kind == "chunk"}
        print(f"\nhit rate, {len(cases)} retrieval cases")
        with tempfile.TemporaryDirectory(prefix="parity-int8-", ignore_cleanup_errors=True) as tmp:
            copy, gone = _int8_copy(live, chunks, vec_of, Path(tmp) / "chroma")
            if gone or live.count() != len(chunks):
                print(f"note: the live index changed during --check ({len(chunks)} chunks compared, {live.count()} "
                      f"now, {gone} of the compared gone): the fp32 runs read it as it is now")
            runs = [("fp32 docs, fp32 queries", live, st), ("fp32 docs, int8 queries", live, int8),
                    ("int8 docs, int8 queries", copy, int8)]
            for label, store, emb in runs:
                with _retrieval_using(store, emb):
                    r = hit_rate(cases, retriever_search(k))
                rates[label] = r.hits
                print(f"{label}: {r.summary()}")
                for line in r.lines()[1:]:
                    print("  " + line)

    print(f"\ngate (docs/PLAN.md M7): min cosine >= {par.GATE_COSINE}: "
          f"{'PASS' if cos.min() >= par.GATE_COSINE else 'FAIL'} ({cos.min():.5f}"
          + (f"; {min_kept:.5f} without {len(stale)} stale stored vectors" if stale else "") + ")")
    if rates:
        base = rates["fp32 docs, fp32 queries"]
        drop = base - min(rates.values())
        points = 100 * drop / max(n_cases, 1)            # PLAN M7: "drops <= 1 point"; one of 30 cases is 3.3
        print(f"gate: hit-rate drop <= 1 point: {'PASS' if points <= 1 else 'FAIL'} (drop {drop} of {n_cases} "
              f"cases = {points:.1f} points; {', '.join(f'{label} {n}' for label, n in rates.items())})")
    print("record the result in docs/tuning_log.md; switching needs the user's approval and scripts/reindex.py")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--sanity", type=int, default=100)
    ap.add_argument("--k", type=int, default=None)
    ap.add_argument("--no-hit-rate", action="store_true")
    a = ap.parse_args()
    if a.check:
        check(a.limit, a.sanity, a.k, not a.no_hit_rate)
    else:
        export()


if __name__ == "__main__":
    main()
