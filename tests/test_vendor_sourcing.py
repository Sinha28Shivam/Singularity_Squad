import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.main import app


def _parallel(fn, n=2):
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


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


def test_http_concurrent_duplicate_create_one_201_one_409(admin_headers):
    client = TestClient(app, headers=admin_headers)
    codes = _parallel(lambda i: client.post("/api/vendor-sourcing/candidates", json={
        "name": "Parallel Caterers", "services": ["lunch"]}).status_code)
    assert sorted(codes) == [201, 409]


def test_http_concurrent_decisions_one_wins(admin_headers):
    client = TestClient(app, headers=admin_headers)
    cid = client.post("/api/vendor-sourcing/candidates", json={
        "name": "Decision Race", "services": ["lunch"]}).json()["candidate_id"]
    codes = _parallel(lambda i: client.post(f"/api/vendor-sourcing/candidates/{cid}/decision", json={
        "decision": "approve" if i else "reject", "reason": "Parallel review"}).status_code)
    assert sorted(codes) == [200, 409]


def test_candidate_response_adds_fields_and_evidence(admin_headers):
    client = TestClient(app, headers=admin_headers)
    r = client.post("/api/vendor-sourcing/candidates", json={
        "name": "Evidence Caterers", "services": ["lunch"], "compliance_documents": ["GST certificate"],
        "compliance_evidence": [{"document_type": "FSSAI licence", "reference": "FSSAI-1",
                                 "expires_on": "2027-01-31"}]})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["audit_pending"] is False and body["compliance_documents"] == ["GST certificate"]
    assert body["compliance_evidence"][0]["verified"] is False
    assert body["compliance_evidence"][0]["expires_on"] == "2027-01-31"
    assert body["status_history"][0]["to_status"] == "pending_review"
    bad = client.post("/api/vendor-sourcing/candidates", json={
        "name": "Self Verified", "services": ["lunch"],
        "compliance_evidence": [{"document_type": "GST", "reference": "X", "verified": True}]})
    assert bad.status_code == 422


def test_candidate_detail_events_and_pagination_routes(admin_headers):
    client = TestClient(app, headers=admin_headers)
    ids = [client.post("/api/vendor-sourcing/candidates", json={
        "name": f"Paged Vendor {i}", "services": ["snacks"]}).json()["candidate_id"] for i in range(3)]
    client.post(f"/api/vendor-sourcing/candidates/{ids[0]}/decision",
                json={"decision": "approve", "reason": "Docs reviewed"}, headers={"X-Correlation-ID": "x"})
    detail = client.get(f"/api/vendor-sourcing/candidates/{ids[0]}")
    assert detail.status_code == 200 and detail.json()["status"] == "approved"
    events = client.get(f"/api/vendor-sourcing/candidates/{ids[0]}/events").json()["events"]
    assert [e["event_type"] for e in events] == ["candidate_submitted", "candidate_approved"]
    assert events[1]["from_status"] == "pending_review" and events[1]["to_status"] == "approved"
    assert client.get("/api/vendor-sourcing/candidates/VCAND-NOPE").status_code == 404
    assert client.get("/api/vendor-sourcing/candidates/VCAND-NOPE/events").status_code == 404
    page = client.get("/api/vendor-sourcing/candidates?limit=2&offset=0").json()
    assert page["total"] == 3 and len(page["items"]) == 2 and page["next_offset"] == 2
    assert client.get("/api/vendor-sourcing/candidates?limit=201").status_code == 422
    assert client.get("/api/vendor-sourcing/candidates?offset=-1").status_code == 422
    assert client.post("/api/vendor-sourcing/candidates/repair-events").json()["scanned"] == 0


def test_candidate_routes_require_admin(user_headers_factory):
    client = TestClient(app)
    assert client.get("/api/vendor-sourcing/candidates").status_code == 401
    user = TestClient(app, headers=user_headers_factory("bob"))
    assert user.get("/api/vendor-sourcing/candidates").status_code == 403
    assert user.get("/api/vendor-sourcing/candidates/VCAND-X/events").status_code == 403
    assert user.post("/api/vendor-sourcing/candidates/repair-events").status_code == 403


def test_audit_failure_returns_committed_state_with_audit_pending(admin_headers, monkeypatch):
    from pymongo.errors import AutoReconnect
    from app.vendor_sourcing import repository

    def boom(event):
        raise AutoReconnect("down")

    real = repository._insert_event
    monkeypatch.setattr(repository, "_insert_event", boom)
    client = TestClient(app, headers=admin_headers)
    r = client.post("/api/vendor-sourcing/candidates", json={"name": "Pending Audit", "services": ["lunch"]})
    assert r.status_code == 201 and r.json()["audit_pending"] is True
    monkeypatch.setattr(repository, "_insert_event", real)
    rep = client.post("/api/vendor-sourcing/candidates/repair-events").json()
    assert rep["delivered_events"] == 1
    events = client.get(f"/api/vendor-sourcing/candidates/{r.json()['candidate_id']}/events").json()["events"]
    assert len(events) == 1
