"""Golden-set evaluation: hit rate, faithfulness (judge), correctness (judge), citation validity,
not-found handling, refusal routing. Saves eval/results/<stamp>.json and BLOCKs on a > 5-point drop of any score.
Usage: python scripts/eval.py [--limit N] [--sleep S] [--kind data,both] [--ids 77,94] [--tag label]
                              [--budget] [--baseline eval/results/<stamp>.json]
A case the provider could not answer (quota, timeout) is unscored, not wrong: re-run it with --ids."""
import argparse, json, re, time, _path  # noqa: F401
from datetime import datetime
from pathlib import Path
from typing import Optional
from ragbot.agent.orchestrator import answer
from ragbot.auth.models import Scope
from ragbot.config import settings
from ragbot.evaluation import (SCORES, baseline_rows, blocks, infra_failure, kind_changed, outage_row, pick_baseline,
                               scores)
from ragbot.llm import get_chat
from ragbot.llm.base import LLMError

ROOT = Path(__file__).resolve().parents[1]
NF = settings()["answer.not_found_text"]
JUDGE_SYS = "You grade answers from a document-and-data assistant. Be strict. Output only the JSON requested."


def judge(sources: str, ans: str, expected: str) -> dict:
    """The judge's claims and verdict. A provider failure (timeout, quota) is retried once; if it fails
    again the case is returned unscored (verdict None) instead of crashing the run and losing it."""
    msg = (f"<sources>\n{sources}\n</sources>\n<answer>{ans}</answer>\n<expected>{expected}</expected>\n\n"
           "1. List the factual claims in the answer (at most 12; a table counts as one claim per row group, not per "
           "cell) and whether the sources support them.\n"
           "2. Say whether the answer agrees with the expected answer (same facts; wording may differ).\n"
           'Return JSON: {"claims":[{"claim":str,"supported":bool}],"verdict":"agree"|"partial"|"disagree"}')
    for attempt in (1, 2):
        try:
            # 3000: a dashboard or merged-table answer made the judge enumerate a claim per cell and overflow 1500
            # (cases 84 and 88 on 2026-10-09), which parsed as "disagree"
            r = get_chat().chat([{"role": "user", "content": msg}], system=JUDGE_SYS, max_tokens=3000, purpose="judge")
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
    ap.add_argument("--budget", action="store_true",
                    help="refuse to start when the provider's day's calls plus this run's would exceed llm.daily_budget")
    ap.add_argument("--ids", help="comma-separated case ids to run (e.g. the unscored ones of a run)")
    ap.add_argument("--baseline", metavar="FILE",
                    help="compare against this run instead of the newest complete one that covers every case")
    a = ap.parse_args()
    baseline = (Path(a.baseline) if Path(a.baseline).is_absolute() else ROOT / a.baseline) if a.baseline else None
    if a.compare:
        compare(Path(a.compare) if Path(a.compare).is_absolute() else ROOT / a.compare, baseline)
        return
    cases = [json.loads(l) for l in Path(settings().path("golden_set")).read_text(encoding="utf-8").splitlines() if l.strip()]
    if a.kind:
        cases = [c for c in cases if c.get("kind") in a.kind.split(",")]
    if a.ids:
        wanted = {int(x) for x in a.ids.split(",") if x.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if a.limit:
        cases = cases[: a.limit]
    if a.budget:
        # about 3 provider calls per case (route, SQL or answer, judge); the free tier's bucket is shared with staff
        from ragbot.quota import can_spend
        ok, why = can_spend(3 * len(cases))
        print(f"budget: {why}")
        if not ok:
            raise SystemExit(2)
    rows = []
    for c in cases:
        res = answer(c["q"], history=c.get("history"), where={"category": c["category"]} if c.get("category") else None,
                     user="eval", scope=Scope.unrestricted())   # the golden set; scope leaks: scripts/scope_check.py
        row = {"id": c["id"], "kind": c.get("kind", ""), "route": res.route, "answer": res.text, "warnings": res.warnings}
        markers = set(re.findall(r"\[([PD]\d+)\]", res.text))
        row["citation_valid"] = 1.0 if markers <= {r.marker for r in res.references} else 0.0
        if infra_failure(res):
            # the provider was out (quota, timeout): there is no answer to score. Scored, it counted as wrong —
            # and the quota message is marked not-found, so on a not-found case it would have counted as right.
            row.update(citation_valid=None, hit=None, faithful=None, correct=None, unscored=True,
                       error_kind=res.error_kind)
        elif c.get("expected_route"):
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
    unscored = [r["id"] for r in rows if r.get("unscored")]
    print(f"n={summary['n']} hit={summary['hit_rate']:.0%} faithful={summary['faithfulness']:.0%} "
          f"correct={summary['correctness']:.0%} citations_valid={summary['citation_validity']:.0%} "
          f"not_found={summary['not_found_ok']:.0%} refuse={summary['refuse_ok']:.0%}  model={summary['model']}")
    if unscored:
        print(f"  INCOMPLETE: {len(unscored)} case(s) unscored (provider or judge unreachable) — not a gate result "
              f"until they are re-run: --ids {','.join(str(i) for i in unscored)}")
    compare(out / f"{stamp}.json", baseline)


def compare(current: Path, baseline: Optional[Path] = None) -> None:
    """Compare like with like: a baseline run scored on exactly this run's cases, every score of the six.

    The baseline is `--baseline` when given, else the most recent earlier *complete* run that covered every
    case of this one (ragbot/evaluation.py: a run with an unscored or outage row is never a baseline). Cases
    this run left unscored are left out of both sides and named, so a gap is never read as a drop or a rise; so
    are cases whose kind changed between the runs (a redefined golden case), which are printed on their own.
    Only files named like a run (<date>T<time>.json) are candidates; other reports in the folder are skipped."""
    rows = json.loads(current.read_text(encoding="utf-8"))["rows"]
    ids = {r["id"] for r in rows}
    if baseline is not None:
        fname, old_rows = baseline.name, baseline_rows(baseline, ids)
    else:
        base = pick_baseline(current, ids)
        if base is None:
            print("  no complete earlier run covers these cases; nothing compared")
            return
        fname, old_rows = base
    gaps = {r["id"] for r in rows if outage_row(r)} | {o["id"] for o in old_rows if outage_row(o)}
    redefined = set(kind_changed(old_rows, rows))
    now = sorted((r for r in rows if r["id"] not in gaps | redefined), key=lambda r: r["id"])
    old = [o for o in old_rows if o["id"] not in gaps | redefined]
    before, after = scores(old), scores(now)
    dropped = set(blocks(before, after))
    print(f"  vs {fname} on the same {len(now)} cases" + (f" ({len(gaps)} unscored left out: "
          f"{', '.join(str(i) for i in sorted(gaps))})" if gaps else "") + ":")
    if redefined:
        by_id, old_by_id = {r["id"]: r for r in rows}, {o["id"]: o for o in old_rows}
        print("  redefined since the baseline, reported separately: " + ", ".join(
            f"case {i} {old_by_id[i].get('kind')} -> {by_id[i].get('kind')} (correct {old_by_id[i].get('correct')} "
            f"-> {by_id[i].get('correct')})" for i in sorted(redefined)))
    for k in SCORES:
        if before[k] is None or after[k] is None:
            continue
        flag = "  <-- BLOCK: dropped more than 5 points" if k in dropped else ""
        print(f"  {k:<18} {before[k]:.0%} -> {after[k]:.0%}{flag}")
    changed = [(r["id"], o.get("correct"), r.get("correct")) for r, o in zip(now, old)
               if r.get("correct") != o.get("correct")]
    if changed:
        print("  correctness changed: " + ", ".join(f"case {i} {a} -> {b}" for i, a, b in changed))


if __name__ == "__main__":
    main()
