"""Run the offline RAG retrieval benchmark (AI plan C1/C2) against an in-memory mongomock database.

    python scripts/run_rag_benchmark.py [--out data/eval/reports/latest.json] [--baseline data/eval/baseline_rag_bench_v1.json]
                                        [--tolerance 0.0] [--k 5]

Fully offline: WORKPLACEOPS_OFFLINE=1 (local hashed embeddings), POLICY_RAG_ENABLED=1, a temporary
POLICY_STORE_DIR and a fresh mongomock client. Exit code 1 when --baseline is given and the run regresses
(see app.policy_rag.benchmark.compare_reports), 2 when the benchmark itself is invalid.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_OUT = ROOT / "data" / "eval" / "reports" / "latest.json"


def run(k: int = 5, dataset: str | None = None, corpus: str | None = None) -> dict:
    os.environ["WORKPLACEOPS_OFFLINE"] = "1"
    os.environ["POLICY_RAG_ENABLED"] = "1"
    os.environ["POLICY_RAG_SEED"] = "0"
    os.environ["POLICY_RULES_ENABLED"] = "0"
    os.environ.setdefault("WORKPLACEOPS_PERSISTENCE", "required")
    import mongomock

    from app.db.mongo import close_client, set_client
    from app.policy_rag import benchmark

    with tempfile.TemporaryDirectory(prefix="rag_bench_store_") as store:
        previous_store = os.environ.get("POLICY_STORE_DIR")
        os.environ["POLICY_STORE_DIR"] = store
        set_client(mongomock.MongoClient(tz_aware=True))
        try:
            kwargs = {"k": k}
            if dataset:
                kwargs["dataset_path"] = dataset
            if corpus:
                kwargs["corpus_path"] = corpus
            return benchmark.run_benchmark(**kwargs)
        finally:
            close_client()
            if previous_store is None:
                os.environ.pop("POLICY_STORE_DIR", None)
            else:
                os.environ["POLICY_STORE_DIR"] = previous_store


def compare(current: dict, baseline_path: str | Path, tolerance: float) -> list[str]:
    from app.policy_rag.benchmark import compare_reports
    baseline = json.loads(Path(baseline_path).read_text(encoding="utf-8"))
    return compare_reports(current, baseline, tolerance=tolerance)


def _summary_line(report: dict) -> str:
    o = report["overall"]
    keys = ("recall_at_k", "precision_at_k", "mrr", "ndcg_at_k", "citation_validity_rate",
            "answerability_accuracy", "scope_violations", "degraded_mode_rate", "latency_ms_p50", "latency_ms_p95")
    return " ".join(f"{key}={o.get(key)}" for key in keys)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--tolerance", type=float, default=0.0)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--dataset", default=None)
    ap.add_argument("--corpus", default=None)
    args = ap.parse_args(argv)

    from app.policy_rag.benchmark import BenchmarkError
    try:
        report = run(args.k, args.dataset, args.corpus)
    except BenchmarkError as exc:
        print(f"BENCHMARK INVALID: {exc}", file=sys.stderr)
        return 2
    problems: list[str] = []
    if args.baseline:
        problems = compare(report, args.baseline, args.tolerance)
        report["baseline_comparison"] = {"baseline": Path(args.baseline).name, "tolerance": args.tolerance,
                                         "regressions": problems, "passed": not problems}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(f"RAG benchmark {report['config']['dataset_version']}: {_summary_line(report)}")
    print(f"failing critical cases: {[c['case_id'] for c in report['failing_critical_cases']]}")
    print(f"report written to {out}")
    if problems:
        print("REGRESSIONS vs baseline:")
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
