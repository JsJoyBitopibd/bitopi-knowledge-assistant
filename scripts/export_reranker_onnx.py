"""Export the reranker (bge-reranker-v2-m3) to ONNX and quantize it to int8 (Phase D4).

Writes data/models/<name>-int8/model.onnx (+ tokenizer files); src/ragbot/embed.py uses it when
RERANK_BACKEND=onnx. Needs only torch (export), onnx and onnxruntime (quantization) — deliberately not
`optimum`, whose current release pins transformers < 4.58 and would break sentence-transformers 6
(it downgraded the venv on 2026-09-27; reverted).

Usage: python scripts/export_reranker_onnx.py [--check]
  --check  compare the int8 model's scores with the PyTorch reranker on real chunks (parity + speed)
"""
import argparse, os, shutil, sys, time, _path  # noqa: F401
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def out_dir() -> Path:
    name = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3").split("/")[-1]
    return ROOT / "data" / "models" / f"{name}-int8"


def export() -> Path:
    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    model_id = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
    dest = out_dir(); tmp = dest.parent / (dest.name + "-fp32")
    dest.mkdir(parents=True, exist_ok=True); tmp.mkdir(parents=True, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(model_id)
    model = AutoModelForSequenceClassification.from_pretrained(model_id).eval()
    enc = tok([("question", "passage text")], padding=True, truncation=True, max_length=512, return_tensors="pt")
    t0 = time.perf_counter()
    with torch.no_grad():
        torch.onnx.export(model, (enc["input_ids"], enc["attention_mask"]), str(tmp / "model.onnx"),
                          input_names=["input_ids", "attention_mask"], output_names=["logits"],
                          dynamic_axes={"input_ids": {0: "batch", 1: "seq"}, "attention_mask": {0: "batch", 1: "seq"},
                                        "logits": {0: "batch"}},
                          opset_version=17, dynamo=False)
    print(f"exported fp32 ONNX in {time.perf_counter() - t0:.0f} s")
    t0 = time.perf_counter()
    quantize_dynamic(str(tmp / "model.onnx"), str(dest / "model.onnx"), weight_type=QuantType.QInt8)
    print(f"quantized to int8 in {time.perf_counter() - t0:.0f} s -> {dest / 'model.onnx'} "
          f"({(dest / 'model.onnx').stat().st_size / 1e6:.0f} MB)")
    tok.save_pretrained(dest)
    shutil.rmtree(tmp)          # the fp32 export (~2.3 GB) is only an intermediate
    return dest


def check() -> None:
    """Parity and speed of the int8 ONNX reranker against the PyTorch one, on real indexed chunks."""
    import statistics as st
    import numpy as np
    from ragbot.embed import OnnxReranker, Reranker
    from ragbot.retrieve.keyword import get_keyword_index
    from ragbot.store import get_store
    store, kw = get_store(), get_keyword_index()
    pt, ox = Reranker(), OnnxReranker(out_dir())
    qs = ["Which form is used for a laptop requisition?", "How long are backup records retained?",
          "Who approves an IT purchase?", "What is the minimum password length for privileged accounts?",
          "How often must the firewall rules be reviewed?", "Who approves a new user account?"]
    t_pt, t_ox, same_order, top1, maxdiff = [], [], 0, 0, 0.0
    for q in qs:
        texts = [c.text for c in store.get([cid for cid, _ in kw.search(q, 10)])]
        a = time.perf_counter(); s_pt = np.array(pt.score(q, texts)); t_pt.append(time.perf_counter() - a)
        a = time.perf_counter(); s_ox = np.array(ox.score(q, texts)); t_ox.append(time.perf_counter() - a)
        same_order += list(np.argsort(-s_pt)) == list(np.argsort(-s_ox))
        top1 += int(np.argmax(s_pt) == np.argmax(s_ox))
        maxdiff = max(maxdiff, float(np.abs(s_pt - s_ox).max()))
    print(f"{len(qs)} questions x 10 candidates | PyTorch {st.mean(t_pt):.1f} s | ONNX int8 {st.mean(t_ox):.1f} s "
          f"({st.mean(t_pt) / st.mean(t_ox):.1f}x) | same top-1 {top1}/{len(qs)} | identical order {same_order}/{len(qs)} "
          f"| max score diff {maxdiff:.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check:
        check()
    else:
        export()


if __name__ == "__main__":
    main()
