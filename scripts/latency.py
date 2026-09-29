"""Where do the seconds go? Runs golden-set questions through the real answer path and prints p50/p95 per
stage (ragbot/trace.py), end to end and to the first answer token.

    python scripts/latency.py                     # 51 cases: 30 documents, 10 fixed-tool, 5 model-written SQL,
                                                  #           3 both, 3 not-found
    python scripts/latency.py --group documents   # one group only
    python scripts/latency.py --repeat 2          # each question twice (still no cache hits)
    python scripts/latency.py --sleep 4           # a pause between questions for a rate-limited key
    python scripts/latency.py --retrieval-only    # documents search only (no model call): the 33 documents
                                                  # and not-found questions

The questions come from tests/golden.jsonl (a fixed selection) plus tests/latency_cases.jsonl, which
holds questions no fixed tool matches (the golden set's data questions are all fixed tools). Every
question runs cold: the answer cache, the query-embedding cache and the SQL result cache are
bypassed, so the numbers are what a first-time question costs. Models are loaded before the clock starts.
Results: eval/latency/<timestamp>.json (git-ignored) — add the summary line to docs/tuning_log.md.
"""
import argparse, json, math, sys, time, _path  # noqa: F401
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from ragbot import trace
from ragbot.agent import orchestrator
from ragbot.agent.orchestrator import answer
from ragbot.auth.models import Scope
from ragbot.config import env, settings
from ragbot.models import Answer
from ragbot.retrieve import retriever

ROOT = Path(__file__).resolve().parents[1]
DOC_KINDS = ("code", "table", "paraphrase", "direct")
# the order the table prints; keys a run did not produce are left out
KEYS = ("total", "sources_shown", "first_token", "route", "llm.route", "retrieve", "retrieve.embed", "retrieve.vector",
        "retrieve.keyword", "retrieve.fetch", "retrieve.rerank", "data", "llm.sql", "data.db", "answer",
        "llm.answer.first_token", "llm.answer", "verify")


def select(cases: list[dict]) -> dict[str, list[dict]]:
    """A fixed selection, so runs compare: all code/table/paraphrase cases plus the first direct ones up to
    30 documents questions; every data, both and not-found case."""
    by = defaultdict(list)
    for c in sorted(cases, key=lambda c: c["id"]):
        by[c["kind"]].append(c)
    docs = [c for k in DOC_KINDS[:3] for c in by[k]]
    docs += by["direct"][: max(0, 30 - len(docs))]
    return {"documents": docs, "data": by["data"], "both": by["both"], "not_found": by["not_found"]}


def extra_cases() -> dict[str, list[dict]]:
    out = defaultdict(list)
    for line in (ROOT / "tests" / "latency_cases.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            c = json.loads(line)
            out[c["group"]].append(c)
    return out


def pct(values: list[float], p: float) -> float:
    """Nearest-rank percentile (p50 of 3 values is the middle one; p95 of 10 is the largest)."""
    v = sorted(values)
    return v[max(0, math.ceil(p / 100 * len(v)) - 1)]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", choices=("documents", "data", "sql", "both", "not_found"))
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--retrieval-only", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.0, help="pause between questions (a rate-limited key); not timed")
    a = ap.parse_args()
    cases = [json.loads(l) for l in (ROOT / "tests" / "golden.jsonl").read_text(encoding="utf-8").splitlines()
             if l.strip()]
    groups = select(cases)
    groups = {"documents": groups["documents"], "data": groups["data"], **extra_cases(),
              "both": groups["both"], "not_found": groups["not_found"]}
    if a.group:
        groups = {a.group: groups[a.group]}
    if a.retrieval_only:
        groups = {g: v for g, v in groups.items() if g in ("documents", "not_found")}

    scope = Scope.unrestricted()
    retriever.retrieve("warm up the models", scope=scope)      # model loading is not a question's latency
    runs, failed = [], []
    total = sum(len(g) for g in groups.values()) * a.repeat
    n = 0
    for group, members in groups.items():
        for c in members:
            for _ in range(a.repeat):
                n += 1
                if a.sleep and n > 1:
                    time.sleep(a.sleep)
                orchestrator._ANSWERS.clear()
                retriever._embed_query.cache_clear()
                if a.retrieval_only:
                    tr, ctx = trace.start()
                    ctx.run(retriever.retrieve, c["q"], scope=scope)
                    ans = Answer(text="", route="documents", request_id=tr.id, timings=tr.snapshot())
                else:
                    ans = answer(c["q"], user="latency", refresh=True, scope=scope)
                row = {"id": c["id"], "group": group, "route": ans.route, "not_found": ans.not_found,
                       "error_kind": ans.error_kind, "request_id": ans.request_id, "timings": ans.timings}
                (failed if ans.error_kind else runs).append(row)
                t = ans.timings
                ft = f"{t['first_token']:4.1f}s" if "first_token" in t else "   - "   # no model answer: no token
                print(f"[{n}/{total}] #{c['id']:<3} {group:<9} {ans.route:<9} total {t.get('total', 0):5.1f}s"
                      f"  first token {ft}" + (f"  ERROR {ans.error_kind}" if ans.error_kind else ""), flush=True)

    summary: dict[str, dict[str, dict[str, float]]] = {}
    for group in [*groups, "all"]:
        rows = [r for r in runs if group in ("all", r["group"])]
        summary[group] = {}
        for k in KEYS:
            vals = [r["timings"][k] for r in rows if k in r["timings"]]
            if vals:
                summary[group][k] = {"n": len(vals), "p50": round(pct(vals, 50), 2), "p95": round(pct(vals, 95), 2)}

    def cell(x: dict | None) -> str:
        return f"{x['p50']:.2f} / {x['p95']:.2f} ({x['n']})" if x else "-"

    print(f"\n{'stage':<24}" + "".join(f"{g:>22}" for g in summary))
    print(f"{'':<24}" + "".join(f"{'p50 / p95 (n)':>22}" for _ in summary))
    for k in KEYS:
        cells = [summary[g].get(k) for g in summary]
        if any(cells):
            print(f"{k:<24}" + "".join(f"{cell(x):>22}" for x in cells))
    if failed:
        print(f"\n{len(failed)} run(s) failed at the provider and are left out: "
              + ", ".join(f"#{r['id']} {r['error_kind']}" for r in failed))

    s = settings()
    out = ROOT / "eval" / "latency" / f"{datetime.now().strftime('%Y%m%dT%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "ts": datetime.now().isoformat(timespec="seconds"), "model": env("LLM_MODEL", ""),
        "rerank": {"enabled": env("RERANK_ENABLED", "true"), "backend": env("RERANK_BACKEND", "torch"),
                   "max_length": env("RERANK_MAX_LENGTH", ""), "candidates": s.get("retrieval.rerank_candidates")},
        "repeat": a.repeat, "retrieval_only": a.retrieval_only, "summary": summary, "runs": runs, "failed": failed}, indent=1), encoding="utf-8")
    print(f"\nwritten {out.relative_to(ROOT)}")
    sys.exit(1 if failed and not runs else 0)


if __name__ == "__main__":
    main()
