"""Golden-set evaluation: hit rate, faithfulness (judge), correctness (judge), citation validity,
not-found handling, refusal routing. Saves eval/results/<stamp>.json and BLOCKs on a > 5-point drop.
Usage: python scripts/eval.py [--limit N] [--sleep S] [--kind data,both] [--tag label]"""
import argparse, json, re, time, _path  # noqa: F401
from datetime import datetime
from pathlib import Path
from ragbot.agent.orchestrator import answer
from ragbot.auth.models import Scope
from ragbot.config import settings
from ragbot.llm import get_chat
from ragbot.llm.base import LLMError

ROOT = Path(__file__).resolve().parents[1]
NF = settings()["answer.not_found_text"]
JUDGE_SYS = "You grade answers from a document-and-data assistant. Be strict. Output only the JSON requested."


def judge(sources: str, ans: str, expected: str) -> dict:
    """The judge's claims and verdict. A provider failure (timeout, quota) is retried once; if it fails
    again the case is returned unscored (verdict None) instead of crashing the run and losing it."""
    msg = (f"<sources>\n{sources}\n</sources>\n<answer>{ans}</answer>\n<expected>{expected}</expected>\n\n"
           "1. List each factual claim in the answer and whether the sources support it.\n"
           "2. Say whether the answer agrees with the expected answer (same facts; wording may differ).\n"
           'Return JSON: {"claims":[{"claim":str,"supported":bool}],"verdict":"agree"|"partial"|"disagree"}')
    for attempt in (1, 2):
        try:
            r = get_chat().chat([{"role": "user", "content": msg}], system=JUDGE_SYS, max_tokens=1500, purpose="judge")
            break
        except LLMError as e:
            if attempt == 2:
                return {"claims": [], "verdict": None, "judge_error": f"judge call failed: {e}"[:200]}
            time.sleep(10)
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
    ap.add_argument("--compare", metavar="FILE", help="only compare a saved run (eval/results/<stamp>.json)")
    a = ap.parse_args()
    if a.compare:
        compare(Path(a.compare) if Path(a.compare).is_absolute() else ROOT / a.compare)
        return
    cases = [json.loads(l) for l in Path(settings().path("golden_set")).read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.kind:
        cases = [c for c in cases if c.get("kind") in a.kind.split(",")]
    if a.limit:
        cases = cases[: a.limit]
    rows = []
    for c in cases:
        res = answer(c["q"], history=c.get("history"), where={"category": c["category"]} if c.get("category") else None,
                     user="eval", scope=Scope.unrestricted())   # the golden set; scope leaks: scripts/scope_check.py
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
                if j.get("verdict") is None:        # the judge could not be reached: unscored, not wrong
                    row["faithful"] = row["correct"] = None
                    row["unscored"] = True
                else:
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
               "not_found_ok": mean_sub(rows, "not_found"), "refuse_ok": mean_sub(rows, "refuse"),
               "unscored": sum(1 for r in rows if r.get("unscored"))}
    out = ROOT / "eval" / "results"; out.mkdir(parents=True, exist_ok=True)
    stamp = summary["date"].replace(":", "").replace("-", "")
    (out / f"{stamp}.json").write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False),
                                       encoding="utf-8")
    print(f"n={summary['n']} hit={summary['hit_rate']:.0%} faithful={summary['faithfulness']:.0%} "
          f"correct={summary['correctness']:.0%} citations_valid={summary['citation_validity']:.0%} "
          f"not_found={summary['not_found_ok']:.0%} refuse={summary['refuse_ok']:.0%}  model={summary['model']}"
          + (f"  UNSCORED={summary['unscored']} (judge unreachable; re-run them)" if summary["unscored"] else ""))
    compare(out / f"{stamp}.json")


def compare(current: Path) -> None:
    """Compare like with like: the most recent earlier run that covered every case of this one, scored on
    exactly these cases. (Comparing a --kind subset with a different run's totals printed false BLOCKs.)
    Only files named like a run (<date>T<time>.json) are candidates; other reports in the folder (and
    the run itself) are skipped."""
    run = json.loads(current.read_text(encoding="utf-8"))
    rows, summary = run["rows"], run["summary"]
    ids = {r["id"] for r in rows}
    base = None
    runs = sorted(f for f in current.parent.glob("*.json") if re.fullmatch(r"\d{8}T\d{4}", f.stem))
    for f in reversed([f for f in runs if f.name < current.name]):
        old = {r["id"]: r for r in json.loads(f.read_text(encoding="utf-8")).get("rows", [])}
        if ids <= set(old):
            base = (f.name, [old[i] for i in sorted(ids)])
            break
    if base:
        fname, old_rows = base
        print(f"  vs {fname} on the same {len(ids)} cases:")
        for k, col in (("hit_rate", "hit"), ("faithfulness", "faithful"), ("correctness", "correct"),
                       ("citation_validity", "citation_valid")):
            xs = [r[col] for r in old_rows if r.get(col) is not None]
            before = sum(xs) / len(xs) if xs else 0.0
            flag = "  <-- BLOCK: dropped more than 5 points" if before - summary[k] > 0.05 else ""
            print(f"  {k:<18} {before:.0%} -> {summary[k]:.0%}{flag}")
        changed = [(r["id"], o.get("correct"), r.get("correct")) for r, o in zip(sorted(rows, key=lambda r: r["id"]), old_rows)
                   if r.get("correct") != o.get("correct")]
        if changed:
            print("  correctness changed: " + ", ".join(f"case {i} {a} -> {b}" for i, a, b in changed))


if __name__ == "__main__":
    main()
