"""Policy library ingestion: validation, extraction, chunking, dedupe, versioning, archive and authorization."""
from __future__ import annotations

import io
import json
import pathlib

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.policy_rag import events, ingestion
from app.policy_rag.chunking import chunk_pages, detect_heading
from app.policy_rag.pdf_extract import PageText, PdfError, extract_pages, normalize_letter_spacing
from app.policy_rag.schemas import PolicyDocumentMetadataIn
from app.policy_rag.storage import sanitize_filename
from app.repositories import policy_chunk_repository as chunk_repo
from app.repositories import policy_document_repository as doc_repo
from tests.pdf_factory import make_corrupt_pdf, make_pdf, text_page

POLICY_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "policies"
FILLER = ("Employees must follow this rule when arranging events and must keep records of approvals, quotes and "
          "vendor confirmations for audit purposes at all times. ")


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("POLICY_STORE_DIR", str(tmp_path / "store"))
    from app.db import ensure_indexes
    ensure_indexes()


def meta(**kw) -> PolicyDocumentMetadataIn:
    kw.setdefault("document_type", "company_policy")
    return PolicyDocumentMetadataIn(**kw)


def policy_pdf(tag: str = "A", pages: int = 3) -> bytes:
    return make_pdf([text_page(f"{i + 1}. Section {tag} {i + 1}", f"Page {i + 1} marker {tag}{i + 1}. " + FILLER * 3)
                     for i in range(pages)])


# ------------------------------------------------------------------------------------------- extraction
def test_extract_pages_preserves_page_numbers_and_flags_low_text():
    data = make_pdf([text_page("1. Purpose", FILLER * 2), [], text_page("2. Scope", FILLER * 2)])
    pages = extract_pages(data)
    assert [p.page_number for p in pages] == [1, 2, 3]
    assert [p.low_text for p in pages] == [False, True, False]
    assert "Purpose" in pages[0].text and "Scope" in pages[2].text


def test_letter_spacing_normalized_conservatively():
    assert normalize_letter_spacing("S I N G U L A R I T Y") == "SINGULARITY"
    assert normalize_letter_spacing("S I N G U L A R I T Y  ·  S Y N T H E T I C  M E N U") == \
        "SINGULARITY · SYNTHETIC MENU"
    assert normalize_letter_spacing("A team of 4 people is a small group") == "A team of 4 people is a small group"
    assert normalize_letter_spacing("Version: 2.1   |   Effective date") == "Version: 2.1   |   Effective date"


@pytest.mark.parametrize("data,code", [
    (b"hello world, not a pdf", "not_pdf"),
    (make_corrupt_pdf(), "corrupt"),
    (make_pdf([["top secret"]], encrypt=True), "encrypted"),
    (make_pdf([[], []]), "empty_text"),
])
def test_extract_typed_errors(data, code):
    with pytest.raises(PdfError) as exc:
        extract_pages(data)
    assert exc.value.code == code
    assert "secret" not in exc.value.detail


def test_too_many_pages():
    with pytest.raises(PdfError) as exc:
        extract_pages(policy_pdf(pages=3), max_pages=2)
    assert exc.value.code == "too_many_pages"


def test_chunks_never_cross_pages_and_keep_sections():
    long_text = FILLER * 40  # ~1000 words -> several chunks on one page
    pages = [PageText(1, "1. Purpose and commitment\n" + long_text), PageText(2, "9.1 Insight Eventz\nShort page.")]
    chunks = chunk_pages(pages, {"document_id": "doc_0123456789abcdef", "content_hash": "h" * 64})
    assert len(chunks) >= 3
    assert {c.page_number for c in chunks} == {1, 2}
    assert all(c.section == "1 Purpose and commitment" for c in chunks if c.page_number == 1)
    assert [c.section for c in chunks if c.page_number == 2] == ["9.1 Insight Eventz"]
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(c.token_estimate <= 470 for c in chunks)
    p1 = [c.text.split() for c in chunks if c.page_number == 1]
    assert " ".join(p1[0][-10:]) in " ".join(p1[1][:80])  # overlap carried into the next chunk


def test_heading_detection():
    assert detect_heading("9.1 Insight Eventz") == "9.1 Insight Eventz"
    assert detect_heading("1. Purpose and commitment") == "1 Purpose and commitment"
    assert detect_heading("CORPORATE EVENT MANAGEMENT") == "CORPORATE EVENT MANAGEMENT"
    assert detect_heading("Employees must not accept gifts above the limit.") is None
    assert detect_heading("3. Employees must keep records of approvals, quotes, confirmations, receipts and more "
                          "things for audit.") is None


def test_sanitize_filename():
    assert sanitize_filename("..\\..\\etc/passwd<script>.pdf") == "passwd_script_.pdf"
    assert sanitize_filename("Vander Manu.pdf") == "Vander Manu.pdf"
    assert sanitize_filename("") == "document.pdf"


# ------------------------------------------------------------------------------------------- ingestion
def test_ingest_ready_document_and_chunks_have_pages(tmp_path):
    res = ingestion.ingest_bytes(policy_pdf("A"), "company.pdf", meta(logical_key="co-policy"), "admin")
    assert res.ok and res.status == "ready" and res.document_id.startswith("doc_")
    doc = doc_repo.get_document(res.document_id)
    assert doc["page_count"] == 3 and doc["chunk_count"] >= 3 and doc["version"] == 1
    assert doc["uploaded_by"] == "admin" and len(doc["content_hash"]) == 64
    chunks = chunk_repo.list_chunks(document_ids=[res.document_id])
    for c in chunks:
        assert f"marker A{c['page_number']}" in c["text"]
        assert c["document_status"] == "ready" and c["document_type"] == "company_policy"
    assert (tmp_path / "store" / f"{res.document_id}.pdf").exists()
    types = [e["event_type"] for e in events.list_events(document_id=res.document_id)]
    assert {"document_uploaded", "document_extraction_completed", "document_indexing_completed"} <= set(types)
    assert all("text" not in e for e in events.list_events(document_id=res.document_id))


def test_batch_with_corrupt_file_continues():
    results = ingestion.ingest_batch(
        [("a.pdf", policy_pdf("A")), ("bad.pdf", make_corrupt_pdf()), ("b.pdf", policy_pdf("B")),
         ("notes.txt", b"plain text"), ("fake.pdf", b"MZ\x90\x00 executable")],
        [meta(), meta(), meta(document_type="vendor_policy"), meta(), meta()], "admin")
    assert [r.ok for r in results] == [True, False, True, False, False]
    assert [r.error_code for r in results] == [None, "corrupt", None, "not_pdf", "not_pdf"]
    assert results[1].message and results[1].document_id is None
    assert len(doc_repo.list_documents()) == 2


def test_duplicate_hash_detected_without_reingest():
    first = ingestion.ingest_bytes(policy_pdf("A"), "a.pdf", meta(), "admin")
    second = ingestion.ingest_bytes(policy_pdf("A"), "copy of a.pdf", meta(), "admin")
    assert second.ok and second.duplicate_of == first.document_id and second.document_id == first.document_id
    assert len(doc_repo.list_documents()) == 1


def test_low_text_document_needs_review():
    data = make_pdf([text_page("1. Intro", FILLER * 2), [], [], ["tiny"]])
    res = ingestion.ingest_bytes(data, "scan.pdf", meta(), "admin")
    assert res.ok and res.status == "needs_review"
    doc = doc_repo.get_document(res.document_id)
    assert doc["low_text_pages"] == [2, 3, 4] and doc["extraction_status"] == "partial"
    assert chunk_repo.list_chunks(document_ids=[res.document_id])[0]["document_status"] == "needs_review"


def test_image_only_pdf_needs_ocr():
    res = ingestion.ingest_bytes(make_pdf([[], []]), "scanned.pdf", meta(), "admin")
    assert res.ok and res.status == "needs_ocr" and res.error_code == "empty_text"
    assert doc_repo.get_document(res.document_id)["page_count"] == 2


def test_too_large(monkeypatch):
    monkeypatch.setattr(ingestion, "upload_max_bytes", lambda: 100)
    res = ingestion.ingest_bytes(policy_pdf(), "big.pdf", meta(), "admin")
    assert not res.ok and res.error_code == "too_large"


def test_invalid_metadata_date_range():
    with pytest.raises(ValueError):
        PolicyDocumentMetadataIn(document_type="rate_card", effective_from="2026-10-01", effective_to="2026-01-01")


def test_versioning_supersedes_previous_without_overwrite():
    v1 = ingestion.ingest_bytes(policy_pdf("A"), "p.pdf", meta(logical_key="proc", version_label="1"), "admin")
    v2 = ingestion.ingest_bytes(policy_pdf("B"), "p.pdf", meta(logical_key="proc", version_label="2"), "admin")
    d1, d2 = doc_repo.get_document(v1.document_id), doc_repo.get_document(v2.document_id)
    assert (d1["version"], d2["version"]) == (1, 2)
    assert d1["status"] == "superseded" and d1["superseded_by"] == v2.document_id
    assert d2["status"] == "ready" and d2["supersedes"] == v1.document_id
    assert d1["content_hash"] != d2["content_hash"] and d1["version_label"] == "1"
    assert {c["document_status"] for c in chunk_repo.list_chunks(document_ids=[v1.document_id])} == {"superseded"}
    assert any(e["event_type"] == "document_version_superseded" for e in events.list_events(document_id=v1.document_id))


def test_archive_excludes_chunks_from_ready_set_and_keeps_audit():
    res = ingestion.ingest_bytes(policy_pdf("A"), "a.pdf", meta(), "admin")
    ingestion.archive(res.document_id, "admin")
    assert doc_repo.get_document(res.document_id)["status"] == "archived"
    assert chunk_repo.list_chunks(document_status="ready") == []
    assert len(chunk_repo.list_chunks(document_ids=[res.document_id])) > 0
    assert any(e["event_type"] == "document_archived" for e in events.list_events(document_id=res.document_id))
    # Re-uploading archived content creates a new document (archived records are not duplicates).
    again = ingestion.ingest_bytes(policy_pdf("A"), "a.pdf", meta(), "admin")
    assert again.ok and again.duplicate_of is None and again.document_id != res.document_id


def test_archive_excludes_from_search_if_retrieval_available():
    retrieval = pytest.importorskip("app.policy_rag.retrieval")
    from app.policy_rag.schemas import PolicySearchIn
    from app.portal_schemas import UserPublic
    res = ingestion.ingest_bytes(make_pdf([text_page("1. Trampoline safety", "Trampoline zebra supervision " * 20)]),
                                 "t.pdf", meta(), "admin")
    viewer = UserPublic(username="admin", name="Admin", email="a@example.com", role="admin")
    before = retrieval.search(PolicySearchIn(query="trampoline zebra supervision"), viewer)
    assert any(e.document_id == res.document_id for e in before.evidence)
    ingestion.archive(res.document_id, "admin")
    after = retrieval.search(PolicySearchIn(query="trampoline zebra supervision"), viewer)
    assert all(e.document_id != res.document_id for e in after.evidence)


def test_reprocess_is_idempotent_and_archived_conflicts():
    res = ingestion.ingest_bytes(policy_pdf("A"), "a.pdf", meta(), "admin")
    ids = [c["chunk_id"] for c in chunk_repo.list_chunks(document_ids=[res.document_id])]
    out = ingestion.reprocess(res.document_id, "admin")
    assert out.ok and out.status == "ready"
    assert [c["chunk_id"] for c in chunk_repo.list_chunks(document_ids=[res.document_id])] == ids
    ingestion.archive(res.document_id, "admin")
    from app.policy_rag.errors import PolicyRagError
    with pytest.raises(PolicyRagError) as exc:
        ingestion.reprocess(res.document_id, "admin")
    assert exc.value.status_code == 409


def test_real_policy_pdfs_ingest_ready_with_page_counts():
    results = ingestion.seed_from_directory(POLICY_DIR)
    by_name = {r.filename: r for r in results}
    assert set(by_name) == {"Company Policy.pdf", "Vander Policy.pdf", "Vander Manu.pdf"}
    expected = {"Company Policy.pdf": (16, "company_policy", "company-conduct-policy"),
                "Vander Policy.pdf": (10, "vendor_policy", "event-vendor-venue-policy"),
                "Vander Manu.pdf": (6, "rate_card", "synthetic-event-menu")}
    for name, (pages, dtype, key) in expected.items():
        r = by_name[name]
        assert r.ok and r.status == "ready", (name, r)
        doc = doc_repo.get_document(r.document_id)
        assert (doc["page_count"], doc["document_type"], doc["logical_key"]) == (pages, dtype, key)
        assert doc["chunk_count"] > 0 and doc["low_text_pages"] == []
    chunks = chunk_repo.list_chunks(document_ids=[by_name["Vander Policy.pdf"].document_id], include_embeddings=False)
    assert all(not c["text"].startswith("S I N G") for c in chunks)
    assert doc_repo.get_document(by_name["Vander Manu.pdf"].document_id)["rate_entry_count"] > 0
    assert ingestion.seed_from_directory(POLICY_DIR) == []  # idempotent by content hash


# ----------------------------------------------------------------------------------------------- routes
def _app() -> FastAPI:
    from app.main import app as main_app
    if any(getattr(r, "path", "").startswith("/api/policy-documents") for r in main_app.routes):
        return main_app
    from app.auth.errors import AuthError
    from app.db.errors import PersistenceError
    from app.policy_rag.errors import PolicyRagError
    from app.routers import auth as auth_router
    from app.routers import policy_documents

    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(policy_documents.router)

    @app.exception_handler(AuthError)
    async def _auth(_: Request, exc: AuthError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(PolicyRagError)
    async def _rag(_: Request, exc: PolicyRagError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(PersistenceError)
    async def _pers(_: Request, exc: PersistenceError):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    return app


@pytest.fixture
def api():
    return TestClient(_app())


def _upload(api, headers, files, metas):
    return api.post("/api/policy-documents/upload", headers=headers,
                    files=[("files", (name, io.BytesIO(data), "application/pdf")) for name, data in files],
                    data={"metadata": json.dumps(metas)})


def test_upload_route_batch_partial_failure(api, admin_headers):
    r = _upload(api, admin_headers, [("a.pdf", policy_pdf("A")), ("bad.pdf", make_corrupt_pdf())],
                [{"document_type": "company_policy"}, {"document_type": "other"}])
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert [x["ok"] for x in results] == [True, False] and results[1]["error_code"] == "corrupt"
    listed = api.get("/api/policy-documents", headers=admin_headers).json()["documents"]
    assert len(listed) == 1
    body = json.dumps(listed)
    assert "storage_name" not in body and "marker" not in body and "policy_store" not in body
    one = api.get(f"/api/policy-documents/{results[0]['document_id']}", headers=admin_headers).json()
    assert "storage_name" not in one and "text" not in one and one["page_count"] == 3


def test_upload_route_metadata_validation(api, admin_headers):
    files = [("a.pdf", policy_pdf("A"))]
    r = _upload(api, admin_headers, files, [{"document_type": "rate_card", "effective_from": "2026-10-01",
                                              "effective_to": "2026-01-01"}])
    assert r.status_code == 422 and "effective_to" in r.json()["detail"]
    r = _upload(api, admin_headers, files, [{"document_type": "rate_card", "uploaded_by": "mallory"}])
    assert r.status_code == 422
    r = _upload(api, admin_headers, files, [{"document_type": "company_policy"}] * 2)
    assert r.status_code == 422
    r = api.post("/api/policy-documents/upload", headers=admin_headers,
                 files=[("files", ("a.pdf", io.BytesIO(policy_pdf()), "application/pdf"))], data={"metadata": "{"})
    assert r.status_code == 422
    assert doc_repo.list_documents() == []


def test_upload_route_batch_too_large(api, admin_headers, monkeypatch):
    monkeypatch.setattr(ingestion, "batch_max_bytes", lambda: 1000)
    r = _upload(api, admin_headers, [("a.pdf", policy_pdf("A")), ("b.pdf", policy_pdf("B"))],
                [{"document_type": "company_policy"}] * 2)
    assert r.status_code == 413
    assert doc_repo.list_documents() == []


def test_routes_authorization(api, admin_headers, user_headers_factory, approver_headers_factory):
    user, approver = user_headers_factory("alice"), approver_headers_factory("carol")
    files, metas = [("a.pdf", policy_pdf("A"))], [{"document_type": "company_policy"}]
    assert _upload(api, {}, files, metas).status_code == 401
    assert _upload(api, user, files, metas).status_code == 403
    assert _upload(api, approver, files, metas).status_code == 403
    doc_id = _upload(api, admin_headers, files, metas).json()["results"][0]["document_id"]
    assert api.get("/api/policy-documents", headers=user).status_code == 403
    assert api.get(f"/api/policy-documents/{doc_id}", headers=user).status_code == 403
    assert api.get("/api/policy-documents", headers=approver).status_code == 200
    assert api.get(f"/api/policy-documents/{doc_id}", headers=approver).status_code == 200
    for action in ("reprocess", "archive"):
        assert api.post(f"/api/policy-documents/{doc_id}/{action}", headers=user).status_code == 403
        assert api.post(f"/api/policy-documents/{doc_id}/{action}", headers=approver).status_code == 403
    r = api.post(f"/api/policy-documents/{doc_id}/reprocess", headers=admin_headers)
    assert r.status_code == 200 and r.json()["result"]["status"] == "ready"
    r = api.post(f"/api/policy-documents/{doc_id}/archive", headers=admin_headers)
    assert r.status_code == 200 and r.json()["document"]["status"] == "archived"
    assert "storage_name" not in r.json()["document"]
    assert api.post(f"/api/policy-documents/{doc_id}/reprocess", headers=admin_headers).status_code == 409
    assert api.get("/api/policy-documents/doc_ffffffffffffffff", headers=admin_headers).status_code == 404
    assert api.get("/api/policy-documents/../etc", headers=admin_headers).status_code == 404
    assert api.get("/api/policy-documents/not-an-id", headers=admin_headers).status_code == 422
    listed = api.get("/api/policy-documents?status=archived", headers=approver).json()["documents"]
    assert [d["document_id"] for d in listed] == [doc_id]
