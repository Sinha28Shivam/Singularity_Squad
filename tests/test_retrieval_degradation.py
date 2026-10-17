"""Phase 2 §6.2 embedding degradation: the retrieval mode is reported accurately ("hybrid" only for semantic
remote embeddings, "hybrid_local_hash" for the local hashed fallback, "keyword_only_degraded" without
comparable vectors) with provider/model/availability/reason/config metadata, and any non-hybrid mode adds
POLICY_RETRIEVAL_DEGRADED and can never produce an unreviewed pass. OCR-less documents stay excluded."""
from __future__ import annotations

import pytest

from app.policy_rag import embeddings as emb
from app.policy_rag import evaluation, ingestion, retrieval
from app.policy_rag.schemas import PolicyDocumentMetadataIn, PolicySearchIn
from app.policy_rules import engine
from app.policy_rules.facts import build_facts
from app.portal_schemas import UserPublic
from tests.pdf_factory import make_pdf, text_page
from tests.policy_rag_helpers import HAPPY, ingest, seed_basic_library

APPROVER = UserPublic(username="carol", name="Carol", email="c@example.com", role="approver")
T = {"ticket_id": "REQ-20261017-AAAA0001", "run_count": 1, "submission": HAPPY}
WR = {"final_status": "pass", "request": {"date": "2026-10-23", "attendees": 100, "service": "lunch"},
      "procurement": {"po": {"po_id": "P", "vendor_id": "VEN-002", "total": 76125.0, "line_items": []}}}


def _search(q="risk assessment first aid"):
    return retrieval.search(PolicySearchIn(query=q), APPROVER)


def test_local_hash_vectors_are_not_reported_as_semantic_hybrid():
    seed_basic_library()
    res = _search()
    assert res.retrieval_mode == "hybrid_local_hash"
    assert res.embedding_provider == "local_hash" and res.embedding_model == emb.LOCAL_MODEL
    assert res.embedding_available is False and res.degradation_reason == "local_hash_embeddings_not_semantic"
    assert res.retrieval_config_version.startswith("rcfg-")
    assert any("hybrid_local_hash" in w for w in res.warnings)


def test_keyword_only_when_no_comparable_vectors(monkeypatch):
    seed_basic_library()
    monkeypatch.setattr(retrieval.emb, "embedder_for_model", lambda model: None)
    res = _search()
    assert res.retrieval_mode == "keyword_only_degraded"
    assert res.embedding_provider == "none" and res.embedding_model is None
    assert res.embedding_available is False and res.degradation_reason == "embedding_provider_unavailable"


class _FakeSemantic:
    model = "text-embedding-3-small"

    def embed(self, texts):
        return [emb.LocalHashEmbedder()._vector(t) for t in texts]


def test_semantic_embeddings_report_hybrid(monkeypatch):
    seed_basic_library()
    from app.repositories import policy_chunk_repository as chunks
    from app.db import get_database
    get_database().policy_chunks.update_many({}, {"$set": {"embedding_model": _FakeSemantic.model}})
    assert chunks.list_chunks()
    monkeypatch.setattr(retrieval.emb, "embedder_for_model",
                        lambda model: _FakeSemantic() if model == _FakeSemantic.model else None)
    res = _search()
    assert res.retrieval_mode == "hybrid" and res.embedding_provider == "openai"
    assert res.embedding_available is True and res.degradation_reason is None
    ev = evaluation.evaluate_ticket(T, WR)
    assert ev.retrieval_mode == "hybrid" and "POLICY_RETRIEVAL_DEGRADED" not in ev.approval_reason_codes


@pytest.mark.parametrize("patch", [False, True])
def test_degraded_modes_add_reason_code_and_never_pass(monkeypatch, patch):
    seed_basic_library()
    if patch:
        monkeypatch.setattr(retrieval.emb, "embedder_for_model", lambda model: None)
    ev = evaluation.evaluate_ticket(T, WR)
    assert ev.retrieval_mode in ("hybrid_local_hash", "keyword_only_degraded")
    assert "POLICY_RETRIEVAL_DEGRADED" in ev.approval_reason_codes and ev.outcome != "pass"
    assert ev.embedding_available is False and ev.degradation_reason


def test_retrieval_mode_feeds_rule_facts():
    seed_basic_library()
    ev = evaluation.evaluate_ticket(T, WR)
    facts = build_facts(T, WR, [], {"retrieval_mode": ev.retrieval_mode, "insufficient_evidence": False})
    assert facts.get("policy.retrieval_mode") == "hybrid_local_hash"
    rule = {"rule_id": "RULE-DEGRADED", "rule_version": 1, "name": "Degraded retrieval", "status": "approved",
            "severity": "review", "source": "legacy_config", "created_by": "x", "created_at": "2026-01-01T00:00:00Z",
            "condition": {"field": "policy.retrieval_mode", "op": "ne", "value": "hybrid", "value_type": "string"},
            "outcome": {"decision": "review_required", "reason_code": "POLICY_RETRIEVAL_DEGRADED",
                        "message": "Retrieval degraded."}}
    out = engine.evaluate_ticket_rules(T, WR, [], {"retrieval_mode": ev.retrieval_mode}, rules=[rule],
                                       policy_set_version=1)
    assert out.overall == "review_required" and "POLICY_RETRIEVAL_DEGRADED" in out.reason_codes


def test_ocr_less_documents_excluded_from_retrieval():
    scanned = ingestion.ingest_bytes(make_pdf([[], []]), "scanned.pdf",
                                     PolicyDocumentMetadataIn(document_type="company_policy"), "admin")
    assert scanned.status == "needs_ocr"
    partial = ingest([text_page("Large events", "For events over 150 attendees a documented risk assessment "
                                                "and first aid cover are mandatory before approval of the event."),
                      [], [], ["tiny"]], "partial.pdf")
    assert partial.status == "needs_review"
    res = _search("risk assessment first aid attendees")
    assert all(e.document_id not in (scanned.document_id, partial.document_id) for e in res.evidence)
    assert res.insufficient_evidence


def test_config_version_is_stable_and_sensitive(monkeypatch):
    a = emb.retrieval_config_version()
    assert a == emb.retrieval_config_version()
    cfg = emb.load_config()
    monkeypatch.setattr(emb, "load_config", lambda: {**cfg, "min_relevance": 0.99})
    assert emb.retrieval_config_version() != a
