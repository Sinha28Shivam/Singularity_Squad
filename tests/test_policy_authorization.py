"""Authorization matrix for policy search, evidence, evaluation and rate-card routes."""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.repositories import rate_card_repository as rate_repo
from tests.policy_rag_helpers import HAPPY, TODAY, ingest, rate_card_pages, seed_basic_library


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def actors(admin_headers, user_headers_factory, approver_headers_factory):
    return {"admin": admin_headers, "alice": user_headers_factory("alice"), "bob": user_headers_factory("bob"),
            "carol": approver_headers_factory("carol")}


def _rated_ticket(c, a, monkeypatch):
    monkeypatch.setenv("POLICY_RAG_ENABLED", "1")
    seed_basic_library()
    r = c.post("/api/my/tickets", json=HAPPY, headers=a["alice"])
    tid = r.json()["ticket_id"]
    assert c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY}, headers=a["admin"]).status_code == 200
    return tid


def test_search_requires_approver_or_admin(c, actors):
    seed_basic_library()
    body = {"query": "vendor insurance verification"}
    assert c.post("/api/policy/search", json=body).status_code == 401
    assert c.post("/api/policy/search", json=body, headers=actors["alice"]).status_code == 403
    for who in ("carol", "admin"):
        r = c.post("/api/policy/search", json=body, headers=actors[who])
        assert r.status_code == 200 and r.json()["evidence"], who
        assert all("storage_name" not in e for e in r.json()["evidence"])
    bad = c.post("/api/policy/search", json={**body, "uploaded_by": "x"}, headers=actors["admin"])
    assert bad.status_code == 422


def test_evidence_full_for_staff_redacted_for_owner_404_for_others(c, actors, monkeypatch):
    tid = _rated_ticket(c, actors, monkeypatch)
    for who in ("admin", "carol"):
        full = c.get(f"/api/requests/{tid}/evidence", headers=actors[who])
        assert full.status_code == 200 and full.json()["evaluation"]["evidence"][0]["excerpt"]
    own = c.get(f"/api/requests/{tid}/evidence", headers=actors["alice"])
    assert own.status_code == 200
    data = own.json()
    assert data["redacted"] and data["citations"] and {"filename", "page_number"} <= set(data["citations"][0])
    text = own.text
    for leaked in ("excerpt", "evaluation_hash", "content_hash", "relevance_score", "chunk_id"):
        assert leaked not in text
    assert c.get(f"/api/requests/{tid}/evidence", headers=actors["bob"]).status_code == 404
    assert c.get("/api/requests/REQ-20261017-NOPE0000/evidence", headers=actors["admin"]).status_code == 404


def test_evaluate_policy_admin_only_and_reports_staleness(c, actors, monkeypatch):
    tid = _rated_ticket(c, actors, monkeypatch)
    for who in ("alice", "carol"):
        assert c.post(f"/api/requests/{tid}/evaluate-policy", headers=actors[who]).status_code == 403
    r = c.post(f"/api/requests/{tid}/evaluate-policy", headers=actors["admin"])
    assert r.status_code == 200
    body = r.json()
    assert body["snapshot_created"] is False and body["stale_against_approval"] is False
    # new evidence changes the evaluation; the pending approval is NOT superseded, only reported stale
    ingest([["Vendor verification addendum", "Event vendors must present a fire safety certificate and insurance "
             "verification documents before any workplace lunch is confirmed."]], "addendum.pdf",
           document_type="vendor_policy")
    r = c.post(f"/api/requests/{tid}/evaluate-policy", headers=actors["admin"])
    assert r.json()["stale_against_approval"] is True
    appr = c.get(f"/api/tickets/{tid}/approval", headers=actors["admin"]).json()
    assert appr["current"]["status"] == "pending"


def test_rate_card_routes(c, actors):
    card = ingest(rate_card_pages(), "rates.pdf", document_type="rate_card")
    assert c.get("/api/rate-cards", headers=actors["alice"]).status_code == 403
    r = c.get("/api/rate-cards", params={"vendor_key": "evt-ven-001"}, headers=actors["carol"])
    assert r.status_code == 200 and r.json()["rate_cards"]
    rid = r.json()["rate_cards"][0]["rate_id"]
    assert c.post(f"/api/rate-cards/{rid}/verify", json={"decision": "verify"},
                  headers=actors["alice"]).status_code == 403
    bad = c.post(f"/api/rate-cards/{rid}/verify", json={"decision": "verify", "verified_by": "mallory"},
                 headers=actors["carol"])
    assert bad.status_code == 422
    ok = c.post(f"/api/rate-cards/{rid}/verify", json={"decision": "verify"}, headers=actors["carol"])
    assert ok.status_code == 200 and ok.json()["verification_status"] == "verified"
    assert ok.json()["verified_by"] == "carol"
    again = c.post(f"/api/rate-cards/{rid}/verify", json={"decision": "reject"}, headers=actors["admin"])
    assert again.status_code == 409
    assert c.post("/api/rate-cards/rate_0000000000000000/verify", json={"decision": "verify"},
                  headers=actors["admin"]).status_code == 404
    assert rate_repo.get_entry(rid)["verification_status"] == "verified" and card.ok


def test_validate_rate_route(c, actors):
    card = ingest(rate_card_pages(), "rates.pdf", document_type="rate_card")
    for e in rate_repo.list_entries(document_id=card.document_id):
        c.post(f"/api/rate-cards/{e['rate_id']}/verify", json={"decision": "verify"}, headers=actors["carol"])
    quote = {"vendor_key": "evt-ven-001", "item_or_service_code": "team_gathering", "quoted_unit_rate": "18000",
             "quantity": 2, "as_of": "2026-10-23"}
    assert c.post("/api/policy/validate-rate", json=quote, headers=actors["alice"]).status_code == 403
    r = c.post("/api/policy/validate-rate", json=quote, headers=actors["carol"])
    assert r.status_code == 200 and r.json()["validation_status"] == "pass"
    assert r.json()["expected_total"] == "36000.00"
    assert c.post("/api/policy/validate-rate", json={**quote, "quoted_unit_rate": "abc"},
                  headers=actors["admin"]).status_code == 422
