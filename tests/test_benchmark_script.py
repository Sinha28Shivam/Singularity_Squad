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

    stdout = capsys.readouterr().out
    assert "| engine | scenario | mode |" in stdout and "SIMULATED" in stdout


def test_unknown_or_missing_engine_is_skipped_with_note():
    bench = _load_script()
    engines, notes = bench.load_engines(["orchestrator", "nope"])
    assert "orchestrator" in engines and "nope" in notes
