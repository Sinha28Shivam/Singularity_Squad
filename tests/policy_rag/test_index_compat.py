"""Plan C4/C5: embedding-index compatibility, degraded reporting, safe retrieval telemetry, re-index detection
and extraction quality metadata."""
from __future__ import annotations

import json

import pytest

from app.ai_security import telemetry
from app.db import get_database
from app.policy_rag import embeddings as emb
from app.policy_rag import ingestion, retrieval
from app.policy_rag.schemas import PolicyDocumentMetadataIn, PolicySearchIn
from app.portal_schemas import UserPublic
from app.repositories import policy_chunk_repository as chunk_repo
from app.repositories import policy_document_repository as doc_repo
from tests.pdf_factory import make_pdf, text_page
from tests.policy_rag_helpers import ingest, seed_basic_library

APPROVER = UserPublic(username="carol", name="Carol", email="c@example.com", role="approver")
QUERY = "risk assessment first aid supervision"


def _search(q=QUERY):
    return retrieval.search(PolicySearchIn(query=q), APPROVER)


class _FakeSemantic:
    model = "text-embedding-3-small"
    dims = 64

    def embed(self, texts):
        return [emb.LocalHashEmbedder(self.dims)._vector(t) for t in texts]


def _spy_cosine(monkeypatch):
    calls = []
    real = emb.cosine

    def spy(a, b):
        calls.append((len(a), len(b)))
        return real(a, b)

    monkeypatch.setattr(retrieval.emb, "cosine", spy)
    return calls


def test_local_hash_reported_degraded_with_index_metadata():
    seed_basic_library()
    res = _search()
    assert res.retrieval_mode == "hybrid_local_hash" and res.embedding_available is False
    assert res.degradation_reason == "local_hash_embeddings_not_semantic"
    assert res.mixed_index_warning is False and res.tokenizer_id and res.chunking_config_version
    assert not any("mixes embedding models" in w for w in res.warnings)


def test_mixed_model_index_detected_and_only_matching_vectors_compared(monkeypatch):
    a, b = seed_basic_library()
    other = _FakeSemantic()
    for c in chunk_repo.list_chunks(document_ids=[b.document_id]):
        get_database().policy_chunks.update_one(
            {"chunk_id": c["chunk_id"]},
            {"$set": {"embedding_model": other.model, "embedding_dims": other.dims,
                      "embedding": other.embed([c["text"]])[0], "embedding_provider": "openai"}})
    summary = chunk_repo.index_summary()
    assert summary["mixed_embedding_index"] is True
    calls = _spy_cosine(monkeypatch)
    res = _search()
    assert res.mixed_index_warning is True
    assert any("mixes embedding models" in w for w in res.warnings)
    assert res.retrieval_mode == "hybrid_local_hash" and res.degradation_reason
    assert calls and all(x == y == emb.LOCAL_DIMS for x, y in calls)  # never cross-model / cross-dimension
    # Keyword scoring still finds evidence in the chunks whose vectors were not comparable.
    res2 = _search("vendor licenses public liability insurance")
    assert any(e.document_id == b.document_id for e in res2.evidence)


def test_mixed_index_with_semantic_majority_is_flagged(monkeypatch):
    a, b = seed_basic_library()
    other = _FakeSemantic()
    for c in chunk_repo.list_chunks(document_ids=[a.document_id]):
        get_database().policy_chunks.update_one(
            {"chunk_id": c["chunk_id"]},
            {"$set": {"embedding_model": other.model, "embedding_dims": other.dims,
                      "embedding": other.embed([c["text"]])[0]}})
    monkeypatch.setattr(retrieval.emb, "embedder_for_model",
                        lambda model: other if model == other.model else emb.LocalHashEmbedder())
    res = _search()
    assert res.mixed_index_warning is True and res.degradation_reason == "mixed_embedding_index"


def test_dimension_mismatch_vectors_are_ignored(monkeypatch):
    a, _b = seed_basic_library()
    for c in chunk_repo.list_chunks(document_ids=[a.document_id])[:1]:
        get_database().policy_chunks.update_one({"chunk_id": c["chunk_id"]},
                                                {"$set": {"embedding": [0.1] * 10, "embedding_dims": 10}})
    calls = _spy_cosine(monkeypatch)
    res = _search()
    assert all(x == y == emb.LOCAL_DIMS for x, y in calls)
    assert res.mixed_index_warning is True
    assert res.retrieval_mode == "hybrid_local_hash"


def test_query_vector_dimension_mismatch_degrades_to_keywords(monkeypatch):
    seed_basic_library()
    monkeypatch.setattr(retrieval.emb, "embedder_for_model", lambda model: emb.LocalHashEmbedder(32))
    calls = _spy_cosine(monkeypatch)
    res = _search()
    assert not calls
    assert res.retrieval_mode == "keyword_only_degraded"
    assert res.degradation_reason == "query_embedding_dimension_mismatch"
    assert res.evidence  # keyword scoring still works


def test_retrieval_telemetry_has_no_query_text():
    seed_basic_library()
    secret_query = "risk assessment zebracorn-unique-term first aid"
    res = _search(secret_query)
    events = [e for e in telemetry.recent(50) if e.get("kind") == "retrieval"]
    assert events
    ev = events[-1]
    for key in ("retrieval_mode", "degradation_reason", "result_count", "latency_ms", "insufficient_evidence",
                "retrieval_config_version"):
        assert key in ev
    assert ev["retrieval_mode"] == res.retrieval_mode and ev["result_count"] == len(res.evidence)
    blob = json.dumps(ev, default=str).lower()
    assert "zebracorn" not in blob and "query" not in {k.lower() for k in ev}
    assert all(e.excerpt.lower() not in blob for e in res.evidence)


def test_retrieval_config_version_includes_tokenizer(monkeypatch):
    monkeypatch.setenv("WORKPLACEOPS_TOKENIZER", "tiktoken")
    a = emb.retrieval_config_version()
    monkeypatch.setenv("WORKPLACEOPS_TOKENIZER", "estimate")
    b = emb.retrieval_config_version()
    if a == b:
        pytest.skip("tiktoken not available")
    cfg = emb.load_config()
    monkeypatch.setattr(emb, "load_config", lambda: {**cfg, "chunk_max_tokens": 300})
    assert emb.retrieval_config_version() != b


def test_reindex_detection_after_config_change(monkeypatch):
    a, b = seed_basic_library()
    assert ingestion.documents_needing_reindex() == []
    before = {c["chunk_id"]: c["text"] for c in chunk_repo.list_chunks(document_ids=[a.document_id])}
    cfg = ingestion.config()
    monkeypatch.setattr(ingestion, "config", lambda: {**cfg, "chunk_max_tokens": 400})
    assert ingestion.documents_needing_reindex() == sorted([a.document_id, b.document_id])
    res = ingestion.reindex_document(a.document_id, "admin")
    assert res.ok and res.status == "ready"
    assert ingestion.documents_needing_reindex() == [b.document_id]
    ingestion.reindex_stale_documents("admin")
    assert ingestion.documents_needing_reindex() == []
    after = {c["chunk_id"]: c["text"] for c in chunk_repo.list_chunks(document_ids=[a.document_id])}
    assert after == before  # small documents: same deterministic chunks, only metadata changed
    again = ingestion.reindex_document(a.document_id, "admin")
    assert again.ok and {c["chunk_id"]: c["text"] for c in chunk_repo.list_chunks(document_ids=[a.document_id])} == after


def test_reindex_detection_after_tokenizer_change(monkeypatch):
    monkeypatch.setenv("WORKPLACEOPS_TOKENIZER", "tiktoken")
    a, _ = seed_basic_library()
    monkeypatch.setenv("WORKPLACEOPS_TOKENIZER", "estimate")
    stale = ingestion.documents_needing_reindex()
    rows = chunk_repo.list_chunks(document_ids=[a.document_id])
    if rows[0]["tokenizer_id"] == "estimate-v1":
        assert stale == []  # tiktoken unavailable: nothing changed
        return
    assert a.document_id in stale
    ingestion.reindex_document(a.document_id)
    assert all(r["tokenizer_id"] == "estimate-v1" and r["token_count_exact"] is False
               for r in chunk_repo.list_chunks(document_ids=[a.document_id]))


def test_reindex_detection_for_embedding_model_change():
    a, _ = seed_basic_library()
    assert a.document_id in ingestion.documents_needing_reindex(embedding_model="text-embedding-3-small")
    assert ingestion.documents_needing_reindex(embedding_model=emb.LOCAL_MODEL) == []


# --------------------------------------------------------------------------------- extraction quality (C5)
def test_extraction_quality_metadata_recorded():
    res = ingest([text_page("1. Purpose", "Every workplace event needs a named sponsor and approved budget. " * 4)],
                 "ok.pdf")
    q = doc_repo.get_document(res.document_id)["extraction_quality"]
    assert q["tool"] == "pypdf" and q["tool_version"]
    assert q["low_text_page_count"] == 0 and q["low_text_ratio"] == 0 and q["quality"] == "complete"
    assert q["needs_review"] is False and q["page_count"] == 1


def test_low_text_document_visibly_flagged():
    partial = ingest([text_page("Large events", "For events over 150 attendees a documented risk assessment "
                                                "and first aid cover are mandatory before approval of the event."),
                      [], [], ["tiny"]], "partial.pdf")
    assert partial.status == "needs_review"
    doc = doc_repo.get_document(partial.document_id)
    q = doc["extraction_quality"]
    assert q["low_text_pages"] == doc["low_text_pages"] == [2, 3, 4]
    assert q["low_text_page_count"] == 3 and q["low_text_ratio"] == 0.75
    assert q["needs_review"] is True and q["quality"] == "partial"
    assert "low_text_ratio_exceeds_review_threshold" in q["warnings"] and "pages_without_text" in q["warnings"]
    assert "risk assessment" not in json.dumps(q)  # metadata only, no text


def test_scanned_document_quality_marks_failure():
    res = ingestion.ingest_bytes(make_pdf([[], []]), "scan.pdf",
                                 PolicyDocumentMetadataIn(document_type="company_policy"), "admin")
    assert res.status == "needs_ocr"
    q = doc_repo.get_document(res.document_id)["extraction_quality"]
    assert q["quality"] == "failed" and q["needs_review"] is True and q["empty_text_pages"] == [1, 2]
