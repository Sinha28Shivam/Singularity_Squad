"""Unit tests for scripts/ai_release_gate.py helpers (the full gate is not run inside tests)."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ai_release_gate.py"
BENCH_SCRIPT = ROOT / "scripts" / "run_rag_benchmark.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gate = _load(SCRIPT, "ai_release_gate")

# Assembled at runtime so this test file itself never contains a scannable secret literal.
LIVE_KEY = "sk-" + "Q7ZtW2pLmN4vR8sYkJ3hXc9b"
LIVE_URI = "mongodb+srv://" + "opsadmin:Zx81qPw" + "@cluster0.mongodb.net/prod"
PEM = "-----BEGIN " + "PRIVATE KEY-----"


def test_secret_scanner_detects_live_patterns_without_echoing_values():
    text = f"a = 1\nOPENAI_API_KEY={LIVE_KEY}\nuri = '{LIVE_URI}'\n{PEM}\n"
    findings = gate.scan_text(text, "config.py")
    assert {(f["rule"], f["line"]) for f in findings} == {
        ("openai_style_api_key", 2), ("credentialed_mongodb_uri", 3), ("private_key_block", 4)}
    assert all(set(f) == {"rule", "path", "line"} for f in findings)
    assert LIVE_KEY not in repr(findings)


def test_secret_scanner_ignores_placeholders():
    text = ("OPENAI_API_KEY=sk-your-key-goes-here-xxxxxxxxxxxx\n"
            "MONGODB_URI=mongodb://user:pass@localhost:27017/\n"
            "uri = 'mongodb://appuser:pw@db.example.internal:27017/'\n"
            "MONGODB_URI=mongodb://localhost:27017/\n")
    assert gate.scan_text(text) == []


def test_secret_scan_excludes_env_example_venv_and_binaries(tmp_path):
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "lib.py").write_text(f"k='{LIVE_KEY}'", encoding="utf-8")
    (tmp_path / ".env.example").write_text(f"OPENAI_API_KEY={LIVE_KEY}", encoding="utf-8")
    (tmp_path / "doc.pdf").write_text(LIVE_KEY, encoding="utf-8")
    (tmp_path / "app.py").write_text(f"k='{LIVE_KEY}'", encoding="utf-8")
    found = gate.scan_files(["venv/lib.py", ".env.example", "doc.pdf", "app.py", "missing.py"], root=tmp_path)
    assert [f["path"] for f in found] == ["app.py"]


def test_missing_required_test_paths_are_reported(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "present.py").write_text("", encoding="utf-8")
    present, missing = gate.resolve_test_paths(["tests/present.py", "tests/absent.py"], root=tmp_path)
    assert present == ["tests/present.py"] and missing == ["tests/absent.py"]


def test_pytest_summary_parsing():
    assert gate._parse_pytest_summary("...\n12 passed, 2 skipped, 1 failed in 3.2s") == {
        "passed": 12, "skipped": 2, "failed": 1}
    assert gate._parse_pytest_summary("x\n3 passed, 1 error in 1s") == {"passed": 3, "errors": 1}


def _report(**overall):
    base = {"recall_at_k": 0.9, "precision_at_k": 0.7, "mrr": 0.9, "ndcg_at_k": 0.9, "citation_validity_rate": 1.0,
            "answerability_accuracy": 0.9, "scope_violations": 0, "forbidden_evidence_hits": 0,
            "authorization_failures": 0, "degraded_mode_rate": 1.0}
    base.update(overall)
    return {"config": {"dataset_version": "rag-bench-v1"}, "overall": base,
            "cases": [{"case_id": "RB-001", "critical": True, "passed": True, "failures": []}]}


def test_baseline_comparison_via_benchmark_script():
    bench = _load(BENCH_SCRIPT, "run_rag_benchmark")
    from app.policy_rag.benchmark import compare_reports
    assert bench.compare is not None
    baseline = _report()
    assert compare_reports(_report(), baseline) == []
    assert compare_reports(_report(recall_at_k=0.85), baseline, tolerance=0.1) == []
    assert any("recall_at_k" in p for p in compare_reports(_report(recall_at_k=0.85), baseline))
    assert any("scope_violations" in p for p in compare_reports(_report(scope_violations=1), baseline,
                                                                tolerance=1.0))
    assert any("citation_validity_rate" in p for p in compare_reports(_report(citation_validity_rate=0.99),
                                                                      baseline, tolerance=1.0))
    regressed = _report()
    regressed["cases"][0].update(passed=False, failures=["scope_violation"])
    assert any("RB-001" in p for p in compare_reports(regressed, baseline, tolerance=1.0))
    other = _report()
    other["config"]["dataset_version"] = "rag-bench-v2"
    assert any("dataset_version" in p for p in compare_reports(other, baseline))
