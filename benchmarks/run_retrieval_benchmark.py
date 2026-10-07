"""Lexical retrieval benchmark on this repository's own functions. Pure Python: no model, no network.

    python benchmarks/run_retrieval_benchmark.py            # current shipped thresholds -> benchmarks/results.json
    python benchmarks/run_retrieval_benchmark.py --sweep    # body-only threshold sweep (table only)

Candidates are every indexable function in the working tree, parsed as the indexer does. Ranking is the shipped
lexical method (cognitive_graph.terms.rank). The offline run has no Neo4j, so the full-text score is replaced by a
BM25 proxy computed over the candidates; the database path uses Neo4j's own score. Metrics: top-1 and top-5 function
accuracy, top-1 file accuracy, false injections on negative cases, and context size. Gold answers were written by
hand from the code; the set is small, so the numbers are a baseline, not a claim.
"""
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cognitive_graph.code_parser import CodeParser  # noqa: E402
from cognitive_graph.graph_sync import source_files  # noqa: E402
from cognitive_graph.indexing_rules import SUPPORTED_SUFFIXES  # noqa: E402
from cognitive_graph.terms import rank, task_terms  # noqa: E402

TOP = 5
SWEEP = (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6)


def load_functions():
    parser = CodeParser()
    rows = []
    current, _ = source_files(ROOT, SUPPORTED_SUFFIXES)
    for rel in sorted(current):
        for fn in parser.parse_file(ROOT / rel):
            rows.append({"file": rel, "name": fn.name, "start_line": fn.start_line, "code": fn.raw_code})
    return rows


def context_chars(hits):
    return sum(len(f"- {h['file']}:{h['start_line']} `{h['name']}` [{h['label']}: {h['why']}]") for h in hits)


def run(rows, cases, body_min=None, name_min=None):
    """One pass over the cases. Returns per-case results and per-query latencies in seconds."""
    results, latencies = [], []
    for case in cases:
        t = time.perf_counter()
        hits = rank(task_terms(case["task"]), rows, TOP, body_min=body_min, name_min=name_min)
        latencies.append(time.perf_counter() - t)
        gold = set(case["gold"])
        gold_files = {r["file"] for r in rows if r["name"] in gold}
        results.append({
            "kind": case["kind"], "task": case["task"], "gold": case["gold"],
            "top1": bool(gold) and bool(hits) and hits[0]["name"] in gold,
            "top5": bool(gold) and any(h["name"] in gold for h in hits),
            "top1_file": bool(gold) and bool(hits) and hits[0]["file"] in gold_files,
            "returned": len(hits), "context_chars": context_chars(hits),
            "top": [f"{h['file']}:{h['name']} [{h['label']}]" for h in hits[:3]],
        })
    return results, latencies


def summarise(items):
    pos = [r for r in items if r["gold"]]
    neg = [r for r in items if not r["gold"]]
    n = max(len(pos), 1)
    out = {
        "top1_function": round(sum(r["top1"] for r in pos) / n, 2),
        "top5_function": round(sum(r["top5"] for r in pos) / n, 2),
        "top1_file": round(sum(r["top1_file"] for r in pos) / n, 2),
        "avg_context_chars": round(statistics.mean(r["context_chars"] for r in items), 0) if items else 0,
    }
    if neg:
        out["false_injections_on_negatives"] = sum(r["returned"] for r in neg)
    return out


def sweep(rows, cases):
    print(f"{'body_min':>8} | {'nat top1':>8} {'nat top5':>8} | {'err top5':>8} | {'exact top1':>10} | "
          f"{'neg inj':>7} | {'avg ctx':>7}")
    for body_min in SWEEP:
        results, _ = run(rows, cases, body_min=body_min, name_min=0.0)
        nat = summarise([r for r in results if r["kind"] == "natural"])
        err = summarise([r for r in results if r["kind"] == "error"])
        exact = summarise([r for r in results if r["kind"] == "exact"])
        overall = summarise(results)
        print(f"{body_min:8.1f} | {nat['top1_function']:8.2f} {nat['top5_function']:8.2f} | "
              f"{err['top5_function']:8.2f} | {exact['top1_function']:10.2f} | "
              f"{overall['false_injections_on_negatives']:7d} | {overall['avg_context_chars']:7.0f}")


def main():
    rows = load_functions()
    cases = json.loads((ROOT / "benchmarks" / "retrieval_cases.json").read_text(encoding="utf-8"))["cases"]
    if "--sweep" in sys.argv:
        sweep(rows, cases)
        return
    results, latencies = run(rows, cases)
    report = {
        "functions_indexed": len(rows), "cases": len(cases),
        "latency_ms_median": round(statistics.median(latencies) * 1000, 1),
        "overall": summarise(results),
        "by_kind": {k: summarise([r for r in results if r["kind"] == k])
                    for k in ("natural", "exact", "error", "negative")},
        "cases": results,
    }
    out = ROOT / "benchmarks" / "results.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"functions indexed: {len(rows)}; cases: {len(cases)}; median ranking time {report['latency_ms_median']} ms")
    for k, v in report["by_kind"].items():
        print(f"[{k}] " + "  ".join(f"{m}={val}" for m, val in v.items()))
    print("[overall] " + "  ".join(f"{m}={val}" for m, val in report["overall"].items()))
    print("written:", out.relative_to(ROOT))


if __name__ == "__main__":
    main()
