from fastapi.testclient import TestClient

from app.main import app


def test_vendor_search_returns_internal_catalog_first(admin_headers):
    client = TestClient(app, headers=admin_headers)
    response = client.get("/api/vendor-sourcing/vendors?q=GreenLeaf")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["internal_vendors"][0]["vendor_id"] == "VEN-002"
    assert body["internal_vendors"][0]["read_only"] is True
    assert body["candidates"] == []
    assert body["external_discovery_enabled"] is False


def test_candidate_starts_pending_and_requires_explicit_review(admin_headers):
    client = TestClient(app, headers=admin_headers)
    created = client.post("/api/vendor-sourcing/candidates", json={
        "name": "  North Star Catering  ",
        "services": ["lunch", "lunch", "snacks"],
        "regions": ["Delhi NCR"],
        "source": "manual",
        "notes": "Synthetic test candidate",
        "compliance_documents": ["GST certificate"]
    })
    assert created.status_code == 201, created.text
    candidate = created.json()
    assert candidate["name"] == "North Star Catering"
    assert candidate["services"] == ["lunch", "snacks"]
    assert candidate["status"] == "pending_review"
    assert candidate["reviewed_at"] is None

    found = client.get("/api/vendor-sourcing/vendors?q=North Star")
    assert found.status_code == 200
    assert found.json()["candidates"][0]["candidate_id"] == candidate["candidate_id"]

    decision = client.post(
        f"/api/vendor-sourcing/candidates/{candidate['candidate_id']}/decision",
        json={"decision": "approve", "reason": "Required documents reviewed"}
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "approved"
    assert decision.json()["reviewed_by"] == "admin"


def test_candidate_reject_and_invalid_transition(admin_headers):
    client = TestClient(app, headers=admin_headers)
    created = client.post("/api/vendor-sourcing/candidates", json={
        "name": "Rejected Candidate",
        "services": ["dinner"],
        "source": "external_discovery",
        "source_reference": "manual research note"
    })
    assert created.status_code == 201
    candidate_id = created.json()["candidate_id"]

    rejected = client.post(
        f"/api/vendor-sourcing/candidates/{candidate_id}/decision",
        json={"decision": "reject", "reason": "Compliance evidence is incomplete"}
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    invalid = client.post(
        f"/api/vendor-sourcing/candidates/{candidate_id}/decision",
        json={"decision": "approve", "reason": "Trying to override rejection"}
    )
    assert invalid.status_code == 409


def test_vendor_sourcing_requires_admin_authentication():
    client = TestClient(app)
    assert client.get("/api/vendor-sourcing/vendors").status_code == 401
    assert client.post("/api/vendor-sourcing/candidates", json={
        "name": "Unauthorized Candidate", "services": ["lunch"]
    }).status_code == 401


def test_candidate_payload_forbids_client_controlled_status(admin_headers):
    client = TestClient(app, headers=admin_headers)
    response = client.post("/api/vendor-sourcing/candidates", json={
        "name": "Status Injection",
        "services": ["lunch"],
        "status": "approved"
    })
    assert response.status_code == 422
