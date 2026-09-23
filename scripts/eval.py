"""Golden-set evaluation: hit rate, faithfulness (judge), correctness (judge), citation validity,
not-found handling, refusal routing. Saves eval/results/<stamp>.json and BLOCKs on a > 5-point drop.
Usage: python scripts/eval.py [--limit N] [--sleep S] [--kind data,both] [--tag label]"""
import argparse, json, re, time, _path  # noqa: F401
from datetime import datetime
from pathlib import Path
from ragbot.agent.orchestrator import answer
from ragbot.config import settings
from ragbot.llm import get_chat

ROOT = Path(__file__).resolve().parents[1]
NF = settings()["answer.not_found_text"]
JUDGE_SYS = "You grade answers from a document-and-data assistant. Be strict. Output only the JSON requested."


def judge(sources: str, ans: str, expected: str) -> dict:
    msg = (f"<sources>\n{sources}\n</sources>\n<answer>{ans}</answer>\n<expected>{expected}</expected>\n\n"
           "1. List each factual claim in the answer and whether the sources support it.\n"
           "2. Say whether the answer agrees with the expected answer (same facts; wording may differ).\n"
           'Return JSON: {"claims":[{"claim":str,"supported":bool}],"verdict":"agree"|"partial"|"disagree"}')
    r = get_chat().chat([{"role": "user", "content": msg}], system=JUDGE_SYS, max_tokens=1500, purpose="judge")
    raw = re.sub(r"^```(json)?|```$", "", r.text.strip(), flags=re.M).strip()
    try:
        return json.loads(raw)
    except Exception:
        return {"claims": [], "verdict": "disagree", "judge_error": raw[:200]}


def mean_sub(rows, kind):
    xs = [r["correct"] for r in rows if r["kind"] == kind and r.get("correct") is not None]
    return sum(xs) / len(xs) if xs else 1.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int); ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--kind", help="comma-separated kinds to run"); ap.add_argument("--tag", default="")
    a = ap.parse_args()
    cases = [json.loads(l) for l in Path(settings().path("golden_set")).read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.kind:
        cases = [c for c in cases if c.get("kind") in a.kind.split(",")]
    if a.limit:
        cases = cases[: a.limit]
    rows = []
    for c in cases:
        res = answer(c["q"], history=c.get("history"), where={"category": c["category"]} if c.get("category") else None,
                     user="eval")
        row = {"id": c["id"], "kind": c.get("kind", ""), "route": res.route, "answer": res.text, "warnings": res.warnings}
        markers = set(re.findall(r"\[([PD]\d+)\]", res.text))
        row["citation_valid"] = 1.0 if markers <= {r.marker for r in res.references} else 0.0
        if c.get("expected_route"):
            row.update(hit=None, faithful=None, correct=1.0 if res.route == c["expected_route"] else 0.0)
        elif c.get("expected_not_found"):
            row.update(hit=None, faithful=None, correct=1.0 if res.not_found else 0.0)
        else:
            pages = {(r.source, r.page) for r in res.references if r.kind == "pdf"}
            views = {v.lower() for r in res.references if r.kind == "data" for v in (r.views or [])}
            # `accept` lists other pages that carry the same fact (a form defined in an annexure and
            # cited in a procedure, say) — honour it here exactly as scripts/hit_rate.py does.
            ok_pages = {(c["source"], c.get("page"))} if c.get("source") else set()
            ok_pages |= {(src, pg) for src, pg in c.get("accept", [])}
            want_pdf = bool(c.get("source")) and bool(ok_pages & pages)
            want_view = bool(c.get("view")) and c["view"].lower() in views
            if c.get("source") and c.get("view"):
                row["hit"] = 1.0 if (want_pdf and want_view) else 0.0
            else:
                row["hit"] = 1.0 if (want_pdf or want_view) else 0.0
            src_text = res.sources_text or "\n".join((r.quote or "") + " " + (r.sql or "") for r in res.references)
            if res.not_found:
                row["faithful"], row["correct"] = None, 0.0
            else:
                j = judge(src_text, res.text, c.get("expected", ""))
                cl = j.get("claims", [])
                row["faithful"] = sum(1 for k in cl if k.get("supported")) / len(cl) if cl else 0.0
                row["correct"] = {"agree": 1.0, "partial": 0.5, "disagree": 0.0}.get(j.get("verdict"), 0.0)
                if "judge_error" in j:
                    row["judge_error"] = j["judge_error"]
        rows.append(row)
        print(f"  case {row['id']:>3} [{row['kind']:<10}] route={row['route']:<9} hit={row.get('hit')} "
              f"faithful={row.get('faithful')} correct={row.get('correct')} cit={row['citation_valid']}", flush=True)
        if a.sleep:
            time.sleep(a.sleep)

    def mean(k):
        xs = [r[k] for r in rows if r.get(k) is not None]
        return sum(xs) / len(xs) if xs else 0.0

    summary = {"date": datetime.now().isoformat(timespec="minutes"), "tag": a.tag, "model": get_chat().model,
               "n": len(rows), "hit_rate": mean("hit"), "faithfulness": mean("faithful"), "correctness": mean("correct"),
               "citation_validity": mean("citation_valid"),
               "not_found_ok": mean_sub(rows, "not_found"), "refuse_ok": mean_sub(rows, "refuse")}
    out = ROOT / "eval" / "results"; out.mkdir(parents=True, exist_ok=True)
    stamp = summary["date"].replace(":", "").replace("-", "")
    (out / f"{stamp}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False),
                                       encoding="utf-8")
    print(f"n={summary['n']} hit={summary['hit_rate']:.0%} faithful={summary['faithfulness']:.0%} "
          f"correct={summary['correctness']:.0%} citations_valid={summary['citation_validity']:.0%} "
          f"not_found={summary['not_found_ok']:.0%} refuse={summary['refuse_ok']:.0%}  model={summary['model']}")
    prev = sorted(out.glob("*.json"))[:-1]
    if prev:
        last = json.loads(prev[-1].read_text(encoding="utf-8"))["summary"]
        for k in ("hit_rate", "faithfulness", "correctness", "citation_validity"):
            drop = last.get(k, 0) - summary[k]
            flag = "  <-- BLOCK: dropped more than 5 points" if drop > 0.05 else ""
            print(f"  {k:<18} {last.get(k, 0):.0%} -> {summary[k]:.0%}{flag}")


if __name__ == "__main__":
    main()
