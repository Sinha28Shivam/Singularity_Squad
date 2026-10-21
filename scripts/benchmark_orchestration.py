"""Benchmark sequential vs parallel analysis orchestration (and optionally the LangGraph PoC).

Usage (from the repo root):
    .\\venv\\Scripts\\python.exe scripts\\benchmark_orchestration.py --reps 30
    .\\venv\\Scripts\\python.exe scripts\\benchmark_orchestration.py --reps 20 --simulated-latency-ms 150
    .\\venv\\Scripts\\python.exe scripts\\benchmark_orchestration.py --engines orchestrator,langgraph --json out.json

Offline by default (WORKPLACEOPS_OFFLINE=1, no network). --simulated-latency-ms wraps
app.agents.policy.evaluate and app.agents.vendor_matcher.match_vendors with a sleep to model
I/O-bound LLM/RAG branches; those numbers are a SIMULATION, not real LLM latency. --live-llm is
opt-in and calls the configured LLM API. Mode is selected per run with WORKPLACEOPS_PARALLEL_ANALYSIS.

Reports end-to-end wall time and the analysis-stage breakdown (policy/vendor/total medians read from
result.extra["orchestration"]). Runs that raise are counted per scenario/mode as errors and excluded from
timing stats; --inject-failure policy|vendor is a test hook that makes one branch raise.
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


def summarize(values: list[float]) -> dict[str, Any]:
    if not values:  # every rep errored: no timing stats (errors are reported separately)
        return {"n": 0, "median_ms": None, "p95_ms": None, "mean_ms": None, "min_ms": None, "max_ms": None}
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


class InjectedBranchFailure(RuntimeError):
    """Raised by --inject-failure (benchmark test hook only)."""


def install_simulated_latency(ms: float, inject_failure: Optional[str] = None) -> Callable[[], None]:
    """Wrap the two independent analysis branches with a sleep (and optionally make one of them raise
    after its sleep). Returns a restore() callable."""
    from app.agents import policy, vendor_matcher

    originals = [("policy", policy, "evaluate", policy.evaluate),
                 ("vendor", vendor_matcher, "match_vendors", vendor_matcher.match_vendors)]
    delay = ms / 1000.0

    def wrap(fn, fail: bool):
        def slowed(*args, **kwargs):
            if delay:
                time.sleep(delay)
            if fail:
                raise InjectedBranchFailure("benchmark-injected branch failure")
            return fn(*args, **kwargs)
        slowed.__wrapped__ = fn
        return slowed

    for branch, module, attr, fn in originals:
        setattr(module, attr, wrap(fn, branch == inject_failure))

    def restore() -> None:
        for _branch, module, attr, fn in originals:
            setattr(module, attr, fn)
    return restore


def analysis_timings(result) -> dict[str, Optional[float]]:
    """Analysis-stage timings from result.extra["orchestration"]; every key is optional."""
    orch = (getattr(result, "extra", None) or {}).get("orchestration") or {}
    analysis = orch.get("analysis_ms") if isinstance(orch.get("analysis_ms"), dict) else {}
    workflow_ms = orch.get("workflow_ms")
    return {"policy_ms": analysis.get("policy"), "vendor_ms": analysis.get("vendor"),
            "analysis_total_ms": analysis.get("total"),
            "workflow_ms": workflow_ms if isinstance(workflow_ms, (int, float)) else None}


def summarize_analysis(samples: list[dict[str, Optional[float]]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("policy_ms", "vendor_ms", "analysis_total_ms", "workflow_ms"):
        vals = [s[key] for s in samples if isinstance(s.get(key), (int, float))]
        out[f"{key}_median"] = round(statistics.median(vals), 3) if vals else None
    out["n"] = sum(1 for s in samples if s.get("analysis_total_ms") is not None)
    return out


def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    return round(a / b, 3) if a and b else None


def run_once(fn: Callable, sc: dict[str, Any]):
    return fn(sc["text"], today=DEMO_DATE, invoice_id=sc["invoice_id"], request_id="REQ-BENCH")


def diff_outputs(ref: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    return {k: {"expected": ref.get(k), "actual": other.get(k)}
            for k in sorted(ref.keys() | other.keys()) if ref.get(k) != other.get(k)}


def benchmark(engine_names: list[str], reps: int, simulated_ms: float,
              live_llm: bool, inject_failure: Optional[str] = None) -> dict[str, Any]:
    engines, notes = load_engines(engine_names)
    scs = scenarios()
    results: dict[str, Any] = {}
    outputs: dict[str, dict[str, dict[str, Any]]] = {}  # scenario -> "engine/mode" -> outputs
    stability_issues: list[str] = []
    prev_mode = os.environ.get("WORKPLACEOPS_PARALLEL_ANALYSIS")
    restore = (install_simulated_latency(simulated_ms, inject_failure)
               if simulated_ms > 0 or inject_failure else (lambda: None))
    try:
        for ename, fn in engines.items():
            results[ename] = {}
            for mode in MODES:
                os.environ["WORKPLACEOPS_PARALLEL_ANALYSIS"] = MODE_ENV[mode]
                per_sc: dict[str, Any] = {}
                all_times: list[float] = []
                all_analysis: list[dict[str, Optional[float]]] = []
                mode_errors: dict[str, int] = {}
                for sc in scs:
                    errors: dict[str, int] = {}
                    warm: Optional[dict[str, Any]] = None
                    try:
                        warm = business_outputs(run_once(fn, sc))  # warm-up, not timed
                        outputs.setdefault(sc["name"], {})[f"{ename}/{mode}"] = warm
                    except Exception as exc:  # noqa: BLE001 - counted, never timed
                        errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
                    times: list[float] = []
                    analysis: list[dict[str, Optional[float]]] = []
                    for _ in range(reps):
                        t0 = time.perf_counter()
                        try:
                            res = run_once(fn, sc)
                        except Exception as exc:  # noqa: BLE001 - errors are excluded from timing stats
                            errors[type(exc).__name__] = errors.get(type(exc).__name__, 0) + 1
                            continue
                        times.append((time.perf_counter() - t0) * 1000.0)
                        analysis.append(analysis_timings(res))
                        if warm is not None and business_outputs(res) != warm:
                            stability_issues.append(f"{ename}/{mode}/{sc['name']}: output changed between reps")
                    all_times.extend(times)
                    all_analysis.extend(analysis)
                    for k, v in errors.items():
                        mode_errors[k] = mode_errors.get(k, 0) + v
                    per_sc[sc["name"]] = {**summarize(times), "errors": sum(errors.values()),
                                          "errors_by_type": errors, "analysis": summarize_analysis(analysis)}
                results[ename][mode] = {"scenarios": per_sc,
                                        "overall": {**summarize(all_times), "errors": sum(mode_errors.values()),
                                                    "errors_by_type": mode_errors,
                                                    "analysis": summarize_analysis(all_analysis)}}
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
            "overall": _ratio(seq["overall"]["median_ms"], par["overall"]["median_ms"]),
            "scenarios": {n: _ratio(seq["scenarios"][n]["median_ms"], par["scenarios"][n]["median_ms"])
                          for n in seq["scenarios"]},
            "analysis_overall": _ratio(seq["overall"]["analysis"]["analysis_total_ms_median"],
                                       par["overall"]["analysis"]["analysis_total_ms_median"]),
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
            "llm_model": llm.model_name() if live_llm and llm.llm_enabled() else None,
            "offline": os.environ.get("WORKPLACEOPS_OFFLINE") == "1",
            "parallel_analysis_env_before_run": prev_mode,
            "parallel_analysis_env_per_mode": dict(MODE_ENV),
            "inject_failure": inject_failure,
            "timing_note": ("end-to-end ms = run_workflow wall time measured here; analysis ms = "
                            "result.extra['orchestration']['analysis_ms'] (policy/vendor/total). Runs that "
                            "raised are counted in 'errors' and excluded from all timing stats."),
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
    if cfg.get("inject_failure"):
        lines.append(f"> TEST HOOK: --inject-failure {cfg['inject_failure']} (failing runs counted as errors, "
                     "excluded from timings).")
    for engine, note in report["notes"].items():
        lines.append(f"> {engine}: {note}")

    def f(v: Optional[float]) -> str:
        return "-" if v is None else f"{v:.2f}"

    lines += ["", "### End-to-end (run_workflow wall time)", "",
              "| engine | scenario | mode | n | errors | median ms | p95 ms | mean ms | min ms | max ms | speedup |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for engine, modes in report["results"].items():
        sp = report["speedup_seq_over_par_median"][engine]
        for name in list(modes["sequential"]["scenarios"]) + ["OVERALL"]:
            for mode in MODES:
                s = modes[mode]["overall"] if name == "OVERALL" else modes[mode]["scenarios"][name]
                speed = ""
                if mode == "parallel":
                    ratio = sp["overall"] if name == "OVERALL" else sp["scenarios"][name]
                    speed = "-" if ratio is None else f"{ratio:.2f}x"
                label = f"**{name}**" if name == "OVERALL" else name
                lines.append(f"| {engine} | {label} | {mode} | {s['n']} | {s.get('errors', 0)} | "
                             f"{f(s['median_ms'])} | {f(s['p95_ms'])} | {f(s['mean_ms'])} | {f(s['min_ms'])} | "
                             f"{f(s['max_ms'])} | {speed} |")
    lines += ["", "### Analysis stage (medians from result.extra['orchestration'])", "",
              "| engine | scenario | mode | policy ms | vendor ms | analysis total ms | workflow ms |",
              "|---|---|---|---:|---:|---:|---:|"]
    for engine, modes in report["results"].items():
        for name in list(modes["sequential"]["scenarios"]) + ["OVERALL"]:
            for mode in MODES:
                s = modes[mode]["overall"] if name == "OVERALL" else modes[mode]["scenarios"][name]
                a = s.get("analysis") or {}
                label = f"**{name}**" if name == "OVERALL" else name
                lines.append(f"| {engine} | {label} | {mode} | {f(a.get('policy_ms_median'))} | "
                             f"{f(a.get('vendor_ms_median'))} | {f(a.get('analysis_total_ms_median'))} | "
                             f"{f(a.get('workflow_ms_median'))} |")
    total_errors = sum(m[mode]["overall"].get("errors", 0) for m in report["results"].values() for mode in MODES)
    lines += ["", "Speedup = sequential median / parallel median (>1 means parallel is faster).",
              "Analysis '-' = no analysis ran (e.g. missing_info clarification path) or key absent.",
              f"Errors (excluded from timings): **{total_errors}**",
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
    p.add_argument("--inject-failure", choices=("policy", "vendor"), default=None,
                   help="TEST HOOK: make one analysis branch raise; failures are counted, not timed")
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
    report = benchmark(engines, args.reps, args.simulated_latency_ms, args.live_llm, args.inject_failure)
    print(markdown(report))
    if args.json_path:
        out = Path(args.json_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nJSON written to {out}")
    return report


if __name__ == "__main__":
    main()
