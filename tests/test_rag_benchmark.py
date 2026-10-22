"""Offline RAG benchmark (plan C1/C2): runs through the real ingestion + retrieval path on mongomock."""
import copy
import json
from pathlib import Path

import pytest

from app.policy_rag import benchmark

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "data" / "eval" / "baseline_rag_bench_v1.json"
_REPORT: dict = {}


@pytest.fixture
def report(monkeypatch):
    monkeypatch.setenv("POLICY_RAG_ENABLED", "1")
    if not _REPORT:  # the report is plain data; reuse it across tests (each test still gets a fresh DB)
        _REPORT.update(benchmark.run_benchmark())
    return copy.deepcopy(_REPORT)


def test_dataset_is_versioned_and_honest_about_review():
    cases = benchmark.load_cases()
    assert 20 <= len(cases) <= 40
    assert {c["dataset_version"] for c in cases} == {"rag-bench-v1"}
    assert {c["status"] for c in cases} == {"draft"}
    assert {c["reviewer"] for c in cases} == {"pending-domain-owner-review"}
    types = {c["query_type"] for c in cases}
    assert {"exact_clause", "paraphrase", "numeric_threshold", "exception_negation", "effective_date",
            "vendor_specific", "missing_coverage", "misleading_overlap", "injection", "unauthorized_scope",
            "mixed_language", "table"} <= types
    assert any(c["critical"] for c in cases)


def test_benchmark_runs_offline_and_metrics_are_bounded(report):
    o = report["overall"]
    assert o["cases"] == len(benchmark.load_cases())
    for key in ("recall_at_k", "precision_at_k", "mrr", "ndcg_at_k", "citation_validity_rate",
                "answerability_accuracy", "degraded_mode_rate"):
        assert 0.0 <= o[key] <= 1.0, key
    assert o["latency_ms_p50"] is not None and o["latency_ms_p95"] >= o["latency_ms_p50"]
    cfg = report["config"]
    assert cfg["dataset_version"] == "rag-bench-v1" and cfg["llm_judge"] is False
    assert cfg["retrieval_config_versions"] and cfg["embedding_models"] == ["local-hash-512"]
    assert set(cfg["retrieval_modes"]) == {"hybrid_local_hash"}  # offline: never reported as semantic hybrid
    assert o["degraded_mode_rate"] == 1.0
    assert cfg["tokenizer_id"]
    json.dumps(report)  # JSON-serializable


def test_per_query_type_breakdown(report):
    by_type = report["by_query_type"]
    assert {"exact_clause", "injection", "vendor_specific", "unauthorized_scope", "missing_coverage"} <= set(by_type)
    assert sum(v["cases"] for v in by_type.values()) == report["overall"]["cases"]


def test_security_invariants_hold(report):
    o = report["overall"]
    assert o["scope_violations"] == 0
    assert o["forbidden_evidence_hits"] == 0
    assert o["authorization_failures"] == 0
    assert o["citation_validity_rate"] == 1.0 and o["citations_total"] > 0
    assert report["failing_critical_cases"] == []
    denied = next(c for c in report["cases"] if c["case_id"] == "RB-025")
    assert denied["access_denied"] and denied["returned_chunk_ids"] == []
    expired = next(c for c in report["cases"] if c["case_id"] == "RB-014")
    assert expired["predicted_answerability"] == "insufficient_evidence"


def test_injection_document_is_only_retrieved_data(report):
    inj = next(c for c in report["cases"] if c["case_id"] == "RB-024")
    assert "hospitality_injection" in inj["returned_documents"]
    assert inj["injection_flagged_evidence"] >= 1  # flagged as a signal, metrics still computed normally
    assert inj["recall_at_k"] == 1.0 and inj["scope_violations"] == []
    benign = next(c for c in report["cases"] if c["case_id"] == "RB-023")
    assert benign["passed"]


def test_citation_validity_check_rejects_fabrication():
    class Ev:
        document_id, content_hash, page_number, excerpt = "doc_1", "h1", 1, "real chunk text"
    chunk = {"document_id": "doc_1", "content_hash": "h1", "page_number": 1, "text": "some real chunk text here"}
    doc = {"document_id": "doc_1", "content_hash": "h1"}
    assert benchmark.citation_valid(Ev, chunk, doc)
    assert not benchmark.citation_valid(Ev, None, doc)
    assert not benchmark.citation_valid(Ev, chunk, {**doc, "content_hash": "other"})
    fake = type("Fake", (Ev,), {"excerpt": "fabricated quote"})
    assert not benchmark.citation_valid(fake, chunk, doc)


def test_regression_comparison_flags_synthetic_regression(report):
    assert benchmark.compare_reports(report, report) == []
    worse = copy.deepcopy(report)
    worse["overall"]["recall_at_k"] = round(report["overall"]["recall_at_k"] - 0.2, 4)
    worse["overall"]["scope_violations"] = 1
    worse["overall"]["citation_validity_rate"] = 0.9
    crit = next(c for c in worse["cases"] if c["critical"])
    crit["passed"], crit["failures"] = False, ["recall"]
    problems = benchmark.compare_reports(worse, report)
    text = "\n".join(problems)
    assert "recall_at_k dropped" in text and "scope_violations" in text and "citation_validity_rate" in text
    assert f"critical case regressed: {crit['case_id']}" in text
    assert not any("recall_at_k" in p for p in benchmark.compare_reports(worse, report, tolerance=0.25))


def test_committed_baseline_matches_current_run(report):
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert baseline["config"]["dataset_version"] == "rag-bench-v1"
    assert benchmark.compare_reports(report, baseline) == []
