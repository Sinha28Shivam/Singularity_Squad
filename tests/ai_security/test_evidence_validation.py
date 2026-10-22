"""Shared citation validator (plan A3): every reason code, spoofed ids/excerpts, cross-scope citations."""
from __future__ import annotations

import datetime as dt

from app.ai_security import evidence_validation as ev
from app.db import get_database
from app.repositories import policy_chunk_repository as chunk_repo
from app.repositories import policy_document_repository as doc_repo
from tests.policy_rag_helpers import COMPANY_PAGES, VENDOR_POLICY_PAGES, ingest

TODAY = dt.date(2026, 10, 22)


def _doc(pages=COMPANY_PAGES, name="company.pdf", **meta):
    res = ingest(pages, name, **meta)
    assert res.ok and res.status == "ready", res
    return res.document_id


def _chunk(document_id, needle=None):
    chunks = chunk_repo.list_chunks(document_ids=[document_id], include_embeddings=False)
    return next(c for c in chunks if needle is None or needle in " ".join(c["text"].split()))


def test_valid_citation_uses_server_metadata_not_model_metadata():
    doc_id = _doc()
    c = _chunk(doc_id, "150 attendees")
    r = ev.validate_citation({"evidence_id": c["chunk_id"], "excerpt": "OVER  150\nattendees",
                              "page_number": 99, "section": "forged"}, expected_document_id=doc_id, as_of=TODAY)
    assert r.valid and r.reason_code is None
    assert r.page_number == c["page_number"] == 3 and r.section == (c.get("section") or None)
    assert r.content_hash == doc_repo.get_document(doc_id)["content_hash"] and r.document_version == 1
    assert r.metadata()["chunk_id"] == c["chunk_id"]


def test_fabricated_and_missing_ids_are_not_found():
    _doc()
    assert ev.validate_citation({"evidence_id": "chk_fabricated0000"}).reason_code == ev.NOT_FOUND
    assert ev.validate_citation({"excerpt": "anything"}).reason_code == ev.NOT_FOUND


def test_real_id_with_fake_excerpt_is_rejected():
    doc_id = _doc()
    c = _chunk(doc_id, "150 attendees")
    r = ev.validate_citation({"chunk_id": c["chunk_id"], "excerpt": "events over 5 attendees are banned"},
                             as_of=TODAY)
    assert not r.valid and r.reason_code == ev.EXCERPT_MISMATCH


def test_cross_document_citation_is_rejected():
    a = _doc()
    b = _doc(VENDOR_POLICY_PAGES, "vendor.pdf", document_type="vendor_policy", logical_key="vendor")
    cb = _chunk(b)
    assert ev.validate_citation({"evidence_id": cb["chunk_id"]}, expected_document_id=a,
                                as_of=TODAY).reason_code == ev.WRONG_DOCUMENT
    # a claimed document id that does not own the chunk is also rejected
    assert ev.validate_citation({"evidence_id": cb["chunk_id"], "document_id": a},
                                as_of=TODAY).reason_code == ev.WRONG_DOCUMENT


def test_cross_vendor_citation_is_out_of_scope():
    d = _doc(VENDOR_POLICY_PAGES, "acme.pdf", document_type="vendor_contract", vendor_key="acme", logical_key="acme")
    c = _chunk(d)
    assert ev.validate_citation({"evidence_id": c["chunk_id"]}, vendor_key="globex",
                                as_of=TODAY).reason_code == ev.OUT_OF_SCOPE
    assert ev.validate_citation({"evidence_id": c["chunk_id"]}, vendor_key=None,
                                as_of=TODAY).reason_code == ev.OUT_OF_SCOPE
    assert ev.validate_citation({"evidence_id": c["chunk_id"]}, vendor_key="acme", as_of=TODAY).valid
    # vendor-neutral documents are in scope for any vendor
    neutral = _chunk(_doc(name="neutral.pdf", logical_key="neutral"))
    assert ev.validate_citation({"evidence_id": neutral["chunk_id"]}, vendor_key="globex", as_of=TODAY).valid


def test_superseded_document_is_not_authoritative_unless_historical():
    old = _doc(name="v1.pdf", logical_key="handbook")
    new = _doc(COMPANY_PAGES[:2], "v2.pdf", logical_key="handbook")
    assert doc_repo.get_document(old)["status"] == "superseded" and new != old
    c = _chunk(old)
    assert ev.validate_citation({"evidence_id": c["chunk_id"]}, as_of=TODAY).reason_code == ev.NOT_AUTHORITATIVE
    assert ev.validate_citation({"evidence_id": c["chunk_id"]}, as_of=TODAY, include_historical=True).valid


def test_non_ready_statuses_are_not_authoritative():
    d = _doc()
    c = _chunk(d)
    for status in ("needs_review", "archived", "failed"):
        doc_repo.update_document(d, {"status": status})
        assert ev.validate_citation({"evidence_id": c["chunk_id"]}, as_of=TODAY).reason_code == ev.NOT_AUTHORITATIVE
    get_database().policy_documents.delete_one({"document_id": d})
    assert ev.validate_citation({"evidence_id": c["chunk_id"]}, as_of=TODAY).reason_code == ev.NOT_AUTHORITATIVE


def test_expired_and_not_yet_effective_documents():
    d = _doc(effective_from=dt.date(2025, 1, 1), effective_to=dt.date(2025, 12, 31), logical_key="old-policy")
    c = _chunk(d)
    assert ev.validate_citation({"evidence_id": c["chunk_id"]}, as_of=TODAY).reason_code == ev.EXPIRED
    assert ev.validate_citation({"evidence_id": c["chunk_id"]}, as_of=dt.date(2025, 6, 1)).valid
    assert ev.validate_citation({"evidence_id": c["chunk_id"]}, as_of=TODAY, include_historical=True).valid
    future = _chunk(_doc(name="future.pdf", effective_from=dt.date(2027, 1, 1), logical_key="future"))
    assert ev.validate_citation({"evidence_id": future["chunk_id"]}, as_of=TODAY).reason_code == ev.EXPIRED


def test_hash_mismatch_is_rejected():
    d = _doc()
    c = _chunk(d)
    assert ev.validate_citation({"evidence_id": c["chunk_id"], "content_hash": "0" * 64},
                                as_of=TODAY).reason_code == ev.HASH_MISMATCH
    get_database().policy_chunks.update_one({"chunk_id": c["chunk_id"]}, {"$set": {"content_hash": "f" * 64}})
    assert ev.validate_citation({"evidence_id": c["chunk_id"]}, as_of=TODAY).reason_code == ev.HASH_MISMATCH


def test_validate_citations_batch_stats():
    d = _doc()
    c = _chunk(d, "150 attendees")
    valid, rejected, stats = ev.validate_citations(
        [{"evidence_id": c["chunk_id"], "excerpt": "over 150 attendees"},
         {"evidence_id": "chk_fake"},
         {"evidence_id": c["chunk_id"], "excerpt": "not in the chunk"}], expected_document_id=d, as_of=TODAY)
    assert [r.chunk_id for r in valid] == [c["chunk_id"]]
    assert stats == {"total": 3, "valid": 1, "rejected": 2,
                     "by_reason": {ev.NOT_FOUND: 1, ev.EXCERPT_MISMATCH: 1}}
    assert set(ev.REASON_CODES) >= {r.reason_code for r in rejected}
