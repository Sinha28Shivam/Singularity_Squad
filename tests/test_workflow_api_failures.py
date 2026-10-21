"""API boundary for analysis-branch failures: /api/workflow and the portal run return the established
``{"detail": ...}`` 500 shape, leak no internals, mark the ticket failed with an audit event, and create
no downstream artefacts (PO, approval request, reconciliation, simulated payment)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.agents import policy, vendor_matcher
from app.data_loader import load_json
from app.db import get_database
from app.main import app

TODAY = date(2026, 10, 17)
S = load_json("sample_requests.json")
SECRET = "sk-secret provider body"
WORKFLOW_MSG = "Workflow analysis failed; the run was stopped and marked as failed."
HAPPY = {"title": "Quarterly team lunch", "event_category": "team_lunch", "service": "lunch",
         "event_date": "2026-10-23", "attendees": 100, "budget_inr": 100000, "location": "Bengaluru",
         "approved_vendor_only": True}


@pytest.fixture(params=["sequential", "parallel"])
def mode(request, monkeypatch):
    monkeypatch.setenv("WORKPLACEOPS_PARALLEL_ANALYSIS", "1" if request.param == "parallel" else "0")
    return request.param


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


def _boom(*_a, **_k):
    raise RuntimeError(SECRET)


def _inject(monkeypatch, failing):
    if failing in ("policy", "both"):
        monkeypatch.setattr(policy, "evaluate", _boom)
    if failing in ("vendor", "both"):
        monkeypatch.setattr(vendor_matcher, "match_vendors", _boom)


def _expected_branch(failing):
    return "vendor" if failing == "vendor" else "policy"  # policy is reported first when both fail


def _assert_safe(r, request_text):
    assert r.status_code == 500, r.text
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"detail": WORKFLOW_MSG}
    for leaked in ("sk-secret", "Traceback", "RuntimeError", request_text):
        assert leaked not in r.text


def _assert_failed_ticket(c, admin_headers, tid, failing):
    db = get_database()
    ticket = db.tickets.find_one({"ticket_id": tid}, {"_id": 0})
    assert ticket["status"] == "failed"
    assert "sk-secret" not in str(ticket.get("error"))
    assert db.purchase_orders.count_documents({}) == 0
    assert db.approval_requests.count_documents({}) == 0
    assert db.payment_simulations.count_documents({}) == 0
    audit = c.get(f"/api/tickets/{tid}/audit", headers=admin_headers)
    assert audit.status_code == 200, audit.text
    events = audit.json()["events"]
    failed = [e for e in events if e["action"] == "analysis_branch_failed"]
    assert len(failed) == 1 and failed[0]["status"] == "fail"
    assert failed[0]["detail"].startswith(f"{_expected_branch(failing)} analysis branch failed (RuntimeError)")
    assert "sk-secret" not in audit.text
    return ticket


@pytest.mark.parametrize("failing", ["policy", "vendor", "both"])
@pytest.mark.parametrize("invoice_id", [None, "INV-5001"])
def test_api_workflow_branch_failure(c, mode, monkeypatch, admin_headers, failing, invoice_id):
    _inject(monkeypatch, failing)
    text = S["happy_path"]
    body = {"text": text, "today": TODAY.isoformat()}
    if invoice_id:
        body["invoice_id"] = invoice_id
    r = c.post("/api/workflow", json=body, headers=admin_headers)
    _assert_safe(r, text)
    tickets = list(get_database().tickets.find({}, {"_id": 0}))
    assert len(tickets) == 1
    ticket = _assert_failed_ticket(c, admin_headers, tickets[0]["ticket_id"], failing)
    assert ticket.get("workflow_result") is None


def test_api_workflow_persistence_error_still_503(c, monkeypatch, admin_headers):
    from app.db import PersistenceError
    from app.services import ticket_service

    def down(*_a, **_k):
        raise PersistenceError("Database unavailable.")

    monkeypatch.setattr(ticket_service, "run_and_persist", down)
    r = c.post("/api/workflow", json={"text": S["happy_path"]}, headers=admin_headers)
    assert r.status_code == 503 and r.json() == {"detail": "Database unavailable."}


@pytest.mark.parametrize("failing", ["policy", "vendor", "both"])
def test_portal_run_with_invoice_branch_failure(c, mode, monkeypatch, admin_headers, user_headers_factory,
                                                failing):
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "1")
    owner = user_headers_factory("alice")
    r = c.post("/api/my/tickets", json=HAPPY, headers=owner)
    assert r.status_code == 201, r.text
    tid = r.json()["ticket_id"]
    raw_text = get_database().tickets.find_one({"ticket_id": tid})["raw_text"]

    _inject(monkeypatch, failing)
    r = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY.isoformat(), "invoice_id": "INV-5001"},
               headers=admin_headers)
    assert r.status_code == 500, r.text
    assert set(r.json()) == {"detail"}
    for leaked in ("sk-secret", "Traceback", raw_text):
        assert leaked not in r.text

    ticket = get_database().tickets.find_one({"ticket_id": tid}, {"_id": 0})
    assert ticket["status"] == "failed"
    assert not (ticket.get("workflow_result") or {}).get("reconciliation")
    audit = c.get(f"/api/admin/tickets/{tid}/audit", headers=admin_headers).json()["events"]
    failed = [e for e in audit if e["action"] == "analysis_branch_failed"]
    assert len(failed) == 1
    assert failed[0]["detail"].startswith(f"{_expected_branch(failing)} analysis branch failed (RuntimeError)")
    db = get_database()
    assert db.purchase_orders.count_documents({"ticket_id": tid}) == 0
    assert db.approval_requests.count_documents({"ticket_id": tid}) == 0
    assert db.payment_simulations.count_documents({}) == 0

    elig = c.get("/api/payments/simulation/eligibility/INV-5001", params={"ticket_id": tid},
                 headers=admin_headers)
    assert elig.status_code == 200, elig.text
    data = elig.json()
    assert data["eligible"] is False
    codes = {x["code"] for x in data["reasons"]}
    assert "TICKET_NOT_APPROVED" in codes or "RECONCILIATION_REQUIRED" in codes, data
    assert db.payment_simulations.count_documents({}) == 0
