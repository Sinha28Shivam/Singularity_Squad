"""Benchmark sequential vs parallel analysis orchestration (and optionally the LangGraph PoC).

Usage (from the repo root):
    .\\venv\\Scripts\\python.exe scripts\\benchmark_orchestration.py --reps 30
    .\\venv\\Scripts\\python.exe scripts\\benchmark_orchestration.py --reps 20 --simulated-latency-ms 150
    .\\venv\\Scripts\\python.exe scripts\\benchmark_orchestration.py --engines orchestrator,langgraph --json out.json

Offline by default (WORKPLACEOPS_OFFLINE=1, no network). --simulated-latency-ms wraps
app.agents.policy.evaluate and app.agents.vendor_matcher.match_vendors with a sleep to model
I/O-bound LLM/RAG branches; those numbers are a SIMULATION, not real LLM latency. --live-llm is
opt-in and calls the configured LLM API. Mode is selected per run with WORKPLACEOPS_PARALLEL_ANALYSIS.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parents[1]
DEMO_DATE = date(2026, 10, 17)
MODES = ("sequential", "parallel")
MODE_ENV = {"sequential": "0", "parallel": "1"}
ENGINES = ("orchestrator", "langgraph")


def scenarios() -> list[dict[str, Any]]:
    samples = json.loads((ROOT / "data" / "sample_requests.json").read_text(encoding="utf-8"))
    out = [{"name": key, "text": samples[key], "invoice_id": None}
           for key in ("happy_path", "over_budget", "missing_info", "too_soon")]
    for inv in ("INV-5001", "INV-5002", "INV-4987"):
        out.append({"name": f"happy_path+{inv}", "text": samples["happy_path"], "invoice_id": inv})
    return out


def percentile_nearest_rank(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[rank - 1]


def summarize(values: list[float]) -> dict[str, float]:
    return {
        "n": len(values),
        "median_ms": round(statistics.median(values), 3),
        "p95_ms": round(percentile_nearest_rank(values, 95), 3),
        "mean_ms": round(statistics.fmean(values), 3),
        "min_ms": round(min(values), 3),
        "max_ms": round(max(values), 3),
    }


def business_outputs(result) -> dict[str, Any]:
    vendors = getattr(result, "vendors", None)
    proc = getattr(result, "procurement", None)
    rec = getattr(result, "reconciliation", None)
    return {
        "selected_vendor_id": vendors.selected.vendor_id if vendors and vendors.selected else None,
        "po_total": proc.po.total if proc and proc.po else None,
        "final_status": result.final_status,
        "reconciliation_status": rec.status if rec else None,
        "missing_fields": list(result.missing_fields),
        "audit_actions": [f"{a.agent}:{a.action}" for a in result.audit_trail],
    }


def load_engines(names: list[str]) -> tuple[dict[str, Callable], dict[str, str]]:
    engines: dict[str, Callable] = {}
    notes: dict[str, str] = {}
    for name in names:
        if name == "orchestrator":
            from app.orchestrator import run_workflow
            engines[name] = run_workflow
        elif name == "langgraph":
            try:
                from experiments.langgraph_poc import run_graph  # lazy: optional dependency
                engines[name] = run_graph
            except Exception as exc:  # noqa: BLE001 - any import failure means "skip"
                notes[name] = f"skipped: could not import experiments.langgraph_poc.run_graph ({exc!r})"
        else:
            notes[name] = f"skipped: unknown engine (choose from {', '.join(ENGINES)})"
    return engines, notes


def install_simulated_latency(ms: float) -> Callable[[], None]:
    """Wrap the two independent analysis branches with a sleep. Returns a restore() callable."""
    from app.agents import policy, vendor_matcher

    originals = [(policy, "evaluate", policy.evaluate),
                 (vendor_matcher, "match_vendors", vendor_matcher.match_vendors)]
    delay = ms / 1000.0

    def wrap(fn):
        def slowed(*args, **kwargs):
            time.sleep(delay)
            return fn(*args, **kwargs)
        slowed.__wrapped__ = fn
        return slowed

    for module, attr, fn in originals:
        setattr(module, attr, wrap(fn))

    def restore() -> None:
        for module, attr, fn in originals:
            setattr(module, attr, fn)
    return restore


def run_once(fn: Callable, sc: dict[str, Any]):
    return fn(sc["text"], today=DEMO_DATE, invoice_id=sc["invoice_id"], request_id="REQ-BENCH")


def diff_outputs(ref: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    return {k: {"expected": ref.get(k), "actual": other.get(k)}
            for k in sorted(ref.keys() | other.keys()) if ref.get(k) != other.get(k)}


def benchmark(engine_names: list[str], reps: int, simulated_ms: float,
              live_llm: bool) -> dict[str, Any]:
    engines, notes = load_engines(engine_names)
    scs = scenarios()
    results: dict[str, Any] = {}
    outputs: dict[str, dict[str, dict[str, Any]]] = {}  # scenario -> "engine/mode" -> outputs
    stability_issues: list[str] = []
    prev_mode = os.environ.get("WORKPLACEOPS_PARALLEL_ANALYSIS")
    restore = install_simulated_latency(simulated_ms) if simulated_ms > 0 else (lambda: None)
    try:
        for ename, fn in engines.items():
            results[ename] = {}
            for mode in MODES:
                os.environ["WORKPLACEOPS_PARALLEL_ANALYSIS"] = MODE_ENV[mode]
                per_sc: dict[str, Any] = {}
                all_times: list[float] = []
                for sc in scs:
                    warm = business_outputs(run_once(fn, sc))  # warm-up, not timed
                    outputs.setdefault(sc["name"], {})[f"{ename}/{mode}"] = warm
                    times: list[float] = []
                    for _ in range(reps):
                        t0 = time.perf_counter()
                        res = run_once(fn, sc)
                        times.append((time.perf_counter() - t0) * 1000.0)
                        if business_outputs(res) != warm:
                            stability_issues.append(f"{ename}/{mode}/{sc['name']}: output changed between reps")
                    all_times.extend(times)
                    per_sc[sc["name"]] = summarize(times)
                results[ename][mode] = {"scenarios": per_sc, "overall": summarize(all_times)}
    finally:
        restore()
        if prev_mode is None:
            os.environ.pop("WORKPLACEOPS_PARALLEL_ANALYSIS", None)
        else:
            os.environ["WORKPLACEOPS_PARALLEL_ANALYSIS"] = prev_mode

    speedups: dict[str, Any] = {}
    for ename, modes in results.items():
        seq, par = modes["sequential"], modes["parallel"]
        speedups[ename] = {
            "overall": round(seq["overall"]["median_ms"] / par["overall"]["median_ms"], 3),
            "scenarios": {n: round(seq["scenarios"][n]["median_ms"] / par["scenarios"][n]["median_ms"], 3)
                          for n in seq["scenarios"]},
        }

    parity_diffs: dict[str, Any] = {}
    for name, by_run in outputs.items():
        ref_key = next(iter(by_run))
        for key, out in by_run.items():
            d = diff_outputs(by_run[ref_key], out)
            if d:
                parity_diffs.setdefault(name, {})[f"{ref_key} vs {key}"] = d
    if stability_issues:
        parity_diffs["_stability"] = sorted(set(stability_issues))

    from app import llm
    return {
        "config": {
            "reps": reps, "warmup_runs": 1, "demo_date": DEMO_DATE.isoformat(),
            "engines_requested": engine_names, "engines_run": list(engines),
            "modes": list(MODES), "scenarios": [s["name"] for s in scs],
            "simulated_latency_ms": simulated_ms,
            "simulated_latency_note": ("SIMULATED: time.sleep added to policy.evaluate and "
                                       "vendor_matcher.match_vendors to model I/O-bound LLM/RAG branches"
                                       if simulated_ms > 0 else None),
            "live_llm": live_llm, "llm_enabled": llm.llm_enabled(),
            "offline": os.environ.get("WORKPLACEOPS_OFFLINE") == "1",
        },
        "machine": {"python": sys.version.split()[0], "implementation": platform.python_implementation(),
                    "cpu_count": os.cpu_count(), "platform": platform.platform(),
                    "processor": platform.processor()},
        "notes": notes,
        "results": results,
        "speedup_seq_over_par_median": speedups,
        "parity": not parity_diffs,
        "parity_diffs": parity_diffs,
        "business_outputs": outputs,
    }


def markdown(report: dict[str, Any]) -> str:
    cfg = report["config"]
    lines = [f"## Orchestration benchmark (reps={cfg['reps']}, warm-up=1, demo date {cfg['demo_date']})", ""]
    if cfg["simulated_latency_ms"]:
        lines.append(f"> SIMULATED latency: {cfg['simulated_latency_ms']:g} ms sleep per analysis branch "
                     "(policy.evaluate, vendor_matcher.match_vendors).")
    m = report["machine"]
    lines.append(f"> LLM enabled: {cfg['llm_enabled']} | offline: {cfg['offline']} | "
                 f"python {m['python']} | cpus {m['cpu_count']} | {m['platform']}")
    for engine, note in report["notes"].items():
        lines.append(f"> {engine}: {note}")
    lines += ["", "| engine | scenario | mode | median ms | p95 ms | mean ms | min ms | max ms | speedup |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for engine, modes in report["results"].items():
        sp = report["speedup_seq_over_par_median"][engine]
        for name in list(modes["sequential"]["scenarios"]) + ["OVERALL"]:
            for mode in MODES:
                s = modes[mode]["overall"] if name == "OVERALL" else modes[mode]["scenarios"][name]
                speed = ""
                if mode == "parallel":
                    speed = f"{(sp['overall'] if name == 'OVERALL' else sp['scenarios'][name]):.2f}x"
                label = f"**{name}**" if name == "OVERALL" else name
                lines.append(f"| {engine} | {label} | {mode} | {s['median_ms']:.2f} | {s['p95_ms']:.2f} | "
                             f"{s['mean_ms']:.2f} | {s['min_ms']:.2f} | {s['max_ms']:.2f} | {speed} |")
    lines += ["", "Speedup = sequential median / parallel median (>1 means parallel is faster).",
              f"Parity (business outputs identical across modes/engines): **{str(report['parity']).lower()}**"]
    if not report["parity"]:
        lines.append("```\n" + json.dumps(report["parity_diffs"], indent=2, default=str) + "\n```")
    return "\n".join(lines)


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reps", type=int, default=30, help="timed repetitions per engine/mode/scenario")
    p.add_argument("--simulated-latency-ms", type=float, default=0.0,
                   help="SIMULATED sleep added to each analysis branch (models I/O-bound LLM/RAG)")
    p.add_argument("--engines", default="orchestrator", help="comma list: orchestrator,langgraph")
    p.add_argument("--json", dest="json_path", help="write full results JSON to this path")
    p.add_argument("--live-llm", action="store_true",
                   help="opt-in: do NOT force offline; calls the configured LLM API")
    args = p.parse_args(argv)
    if args.reps < 1:
        p.error("--reps must be >= 1")
    if args.simulated_latency_ms < 0:
        p.error("--simulated-latency-ms must be >= 0")
    return args


def main(argv: Optional[list[str]] = None) -> dict[str, Any]:
    args = parse_args(argv)
    if args.live_llm:
        print("WARNING: --live-llm set: this benchmark will call the configured LLM API (network, cost).",
              file=sys.stderr)
        os.environ.pop("WORKPLACEOPS_OFFLINE", None)
    else:
        os.environ["WORKPLACEOPS_OFFLINE"] = "1"  # set before any app module is imported/used
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]
    report = benchmark(engines, args.reps, args.simulated_latency_ms, args.live_llm)
    print(markdown(report))
    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nJSON written to {out}")
    return report


if __name__ == "__main__":
    main()
