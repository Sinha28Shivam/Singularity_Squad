"""Fast smoke test for scripts/benchmark_orchestration.py (in-process, offline, simulated latency)."""
import importlib.util
import json
import os
from pathlib import Path

from app.agents import policy, vendor_matcher

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_orchestration.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("benchmark_orchestration", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_percentile_nearest_rank():
    bench = _load_script()
    vals = list(range(1, 21))
    assert bench.percentile_nearest_rank(vals, 95) == 19
    assert bench.percentile_nearest_rank([5.0], 95) == 5.0


def test_benchmark_runs_and_reports_parity(tmp_path, monkeypatch, capsys):
    bench = _load_script()
    monkeypatch.setenv("WORKPLACEOPS_PARALLEL_ANALYSIS", "1")
    orig_eval, orig_match = policy.evaluate, vendor_matcher.match_vendors
    out = tmp_path / "bench.json"

    bench.main(["--reps", "2", "--simulated-latency-ms", "20", "--engines", "orchestrator",
                "--json", str(out)])

    # originals restored and env restored
    assert policy.evaluate is orig_eval and vendor_matcher.match_vendors is orig_match
    assert os.environ["WORKPLACEOPS_PARALLEL_ANALYSIS"] == "1"

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["config"]["offline"] is True
    assert data["config"]["simulated_latency_ms"] == 20
    assert "SIMULATED" in data["config"]["simulated_latency_note"]
    assert {"python", "cpu_count", "platform"} <= set(data["machine"])
    assert data["parity"] is True, data["parity_diffs"]

    modes = data["results"]["orchestrator"]
    assert set(modes) == {"sequential", "parallel"}
    expected = {"happy_path", "over_budget", "missing_info", "too_soon",
                "happy_path+INV-5001", "happy_path+INV-5002", "happy_path+INV-4987"}
    for mode in modes.values():
        assert set(mode["scenarios"]) == expected
        for stats in list(mode["scenarios"].values()) + [mode["overall"]]:
            assert {"median_ms", "p95_ms", "mean_ms", "min_ms", "max_ms"} <= set(stats)
            assert stats["min_ms"] <= stats["median_ms"] <= stats["max_ms"]

    # Two 20 ms branches: sequential >= ~40 ms, parallel ~20 ms. Allow a generous margin.
    seq = modes["sequential"]["scenarios"]["happy_path"]["median_ms"]
    par = modes["parallel"]["scenarios"]["happy_path"]["median_ms"]
    assert par + 8 < seq, (seq, par)
    assert "speedup_seq_over_par_median" in data
    assert data["business_outputs"]["happy_path"]["orchestrator/parallel"]["selected_vendor_id"] == "VEN-002"

    # config recorded; no errors; analysis-stage breakdown present (absent for the clarification path)
    cfg = data["config"]
    assert cfg["reps"] == 2 and cfg["inject_failure"] is None and cfg["llm_model"] is None
    assert cfg["parallel_analysis_env_before_run"] == "1"
    assert cfg["parallel_analysis_env_per_mode"] == {"sequential": "0", "parallel": "1"}
    for mode in modes.values():
        assert mode["overall"]["errors"] == 0 and mode["overall"]["n"] == 2 * len(expected)
        happy = mode["scenarios"]["happy_path"]["analysis"]
        assert happy["n"] == 2 and happy["policy_ms_median"] >= 15 and happy["vendor_ms_median"] >= 15
        assert happy["analysis_total_ms_median"] <= mode["scenarios"]["happy_path"]["max_ms"]
        assert mode["scenarios"]["missing_info"]["analysis"]["analysis_total_ms_median"] is None
    seq_a = modes["sequential"]["scenarios"]["happy_path"]["analysis"]["analysis_total_ms_median"]
    par_a = modes["parallel"]["scenarios"]["happy_path"]["analysis"]["analysis_total_ms_median"]
    assert par_a + 8 < seq_a, (seq_a, par_a)

    stdout = capsys.readouterr().out
    assert "| engine | scenario | mode |" in stdout and "SIMULATED" in stdout
    assert "Analysis stage" in stdout and "Errors (excluded from timings): **0**" in stdout


def test_backward_compatible_cli_without_new_flags(tmp_path):
    bench = _load_script()
    args = bench.parse_args(["--reps", "3"])
    assert args.inject_failure is None and args.reps == 3 and args.engines == "orchestrator"


def test_injected_failure_counted_as_errors_not_timed(tmp_path, monkeypatch):
    bench = _load_script()
    monkeypatch.delenv("WORKPLACEOPS_PARALLEL_ANALYSIS", raising=False)
    orig_eval, orig_match = policy.evaluate, vendor_matcher.match_vendors
    out = tmp_path / "bench.json"

    bench.main(["--reps", "2", "--inject-failure", "vendor", "--json", str(out)])

    assert policy.evaluate is orig_eval and vendor_matcher.match_vendors is orig_match
    assert "WORKPLACEOPS_PARALLEL_ANALYSIS" not in os.environ
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["config"]["inject_failure"] == "vendor"
    for mode in data["results"]["orchestrator"].values():
        happy = mode["scenarios"]["happy_path"]
        # warm-up + 2 reps all fail; no timings recorded for them
        assert happy["errors"] == 3 and happy["n"] == 0 and happy["median_ms"] is None
        assert happy["errors_by_type"] == {"AnalysisBranchError": 3}
        # clarification path never reaches analysis, so it still succeeds and is timed
        missing = mode["scenarios"]["missing_info"]
        assert missing["errors"] == 0 and missing["n"] == 2 and missing["median_ms"] is not None
        assert mode["overall"]["errors"] == 3 * 6 and mode["overall"]["n"] == 2
    assert data["speedup_seq_over_par_median"]["orchestrator"]["scenarios"]["happy_path"] is None


def test_unknown_or_missing_engine_is_skipped_with_note():
    bench = _load_script()
    engines, notes = bench.load_engines(["orchestrator", "nope"])
    assert "orchestrator" in engines and "nope" in notes
