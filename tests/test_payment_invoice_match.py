"""Post-approval invoice match: an approved ticket run without an invoice can have its invoice matched against
the approved draft PO (no re-approval) and then be simulated. SIMULATION ONLY."""
import pytest
from fastapi.testclient import TestClient

from app.db import get_database
from app.main import app
from tests.test_payment_api import BASE, approved_ticket

pytestmark = pytest.mark.usefixtures("_enable")


@pytest.fixture
def _enable(monkeypatch):
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "1")


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def actors(admin_headers, user_headers_factory, approver_headers_factory):
    return admin_headers, user_headers_factory("alice"), approver_headers_factory("carol")


def _no_invoice_approved(c, actors):
    admin, alice, carol = actors
    tid, ticket = approved_ticket(c, admin, alice, carol, invoice_id=None)
    assert ticket["status"] == "approved"
    assert not (ticket["workflow_result"] or {}).get("reconciliation")
    return tid, ticket


def _body(tid, ticket, key="match-key-0001", amount=None):
    po = ticket["workflow_result"]["procurement"]["po"]
    return {"ticket_id": tid, "invoice_id": "INV-5001", "purchase_order_id": po["po_id"],
            "amount": amount or f"{float(po['total']):.2f}", "currency": po["currency"], "simulation_only": True,
            "confirmed_by_user": True, "idempotency_key": key}


def test_unmatched_approved_ticket_blocked_then_match_enables_simulation(c, actors):
    admin, _, _ = actors
    tid, ticket = _no_invoice_approved(c, actors)
    e = c.get(f"{BASE}/eligibility/INV-5001", params={"ticket_id": tid}, headers=admin).json()
    assert not e["eligible"] and "RECONCILIATION_REQUIRED" in {r["code"] for r in e["reasons"]}

    r = c.post(f"/api/admin/tickets/{tid}/invoice-match", json={"invoice_id": "INV-5001"}, headers=admin)
    assert r.status_code == 200, r.text
    m = r.json()["invoice_match"]
    assert m["status"] == "match" and m["post_approval"] and m["po_id"] == ticket["workflow_result"]["procurement"]["po"]["po_id"]
    assert r.json()["status"] == "approved"   # approval untouched, no re-run

    e = c.get(f"{BASE}/eligibility/INV-5001", params={"ticket_id": tid}, headers=admin).json()
    assert e["eligible"], e["reasons"]
    assert e["amount"] == "76125.00"
    p = c.post(BASE, json=_body(tid, ticket), headers=admin)
    assert p.status_code == 201, p.text
    assert p.json()["payment"]["status"] == "accepted_by_simulator"
    assert p.json()["payment"]["money_transferred"] is False
    po = get_database().purchase_orders.find_one({"ticket_id": tid})
    assert po["status"] == "draft"
    audit = c.get(f"/api/admin/tickets/{tid}/audit", headers=admin).json()["events"]
    assert any(ev["action"] == "post_approval_invoice_match" for ev in audit)


def test_rematch_refused_after_payment(c, actors):
    admin, _, _ = actors
    tid, ticket = _no_invoice_approved(c, actors)
    assert c.post(f"/api/admin/tickets/{tid}/invoice-match", json={"invoice_id": "INV-5001"},
                  headers=admin).status_code == 200
    assert c.post(BASE, json=_body(tid, ticket), headers=admin).status_code == 201
    r = c.post(f"/api/admin/tickets/{tid}/invoice-match", json={"invoice_id": "INV-5002"}, headers=admin)
    assert r.status_code == 409


@pytest.mark.parametrize("invoice,code", [("INV-5002", "INVOICE_MATCH_FAILED"), ("INV-4987", "DUPLICATE_INVOICE")])
def test_bad_post_approval_invoice_blocks(c, actors, invoice, code):
    admin, _, _ = actors
    tid, _ = _no_invoice_approved(c, actors)
    assert c.post(f"/api/admin/tickets/{tid}/invoice-match", json={"invoice_id": invoice},
                  headers=admin).status_code == 200
    e = c.get(f"{BASE}/eligibility/{invoice}", params={"ticket_id": tid}, headers=admin).json()
    assert not e["eligible"] and code in {r["code"] for r in e["reasons"]}


def test_match_rules(c, actors, user_headers_factory, approver_headers_factory):
    admin, alice, carol = actors
    tid, _ = _no_invoice_approved(c, actors)
    assert c.post(f"/api/admin/tickets/{tid}/invoice-match", json={"invoice_id": "INV-5001"},
                  headers=alice).status_code == 403
    assert c.post(f"/api/admin/tickets/{tid}/invoice-match", json={"invoice_id": "INV-5001"},
                  headers=carol).status_code == 403
    assert c.post(f"/api/admin/tickets/{tid}/invoice-match", json={"invoice_id": "INV-9999"},
                  headers=admin).status_code == 404
    assert c.post(f"/api/admin/tickets/{tid}/invoice-match", json={"invoice_id": "INV-5001", "status": "match"},
                  headers=admin).status_code == 422
    # ticket that already has a workflow reconciliation -> 409
    tid2, _ = approved_ticket(c, admin, alice, carol, invoice_id="INV-5001")
    assert c.post(f"/api/admin/tickets/{tid2}/invoice-match", json={"invoice_id": "INV-5001"},
                  headers=admin).status_code == 409
    # not approved -> 409
    tid3, _ = approved_ticket(c, admin, alice, carol, invoice_id=None, approve=False)
    assert c.post(f"/api/admin/tickets/{tid3}/invoice-match", json={"invoice_id": "INV-5001"},
                  headers=admin).status_code == 409


def test_tampered_match_is_stale(c, actors):
    admin, _, _ = actors
    tid, _ = _no_invoice_approved(c, actors)
    assert c.post(f"/api/admin/tickets/{tid}/invoice-match", json={"invoice_id": "INV-5001"},
                  headers=admin).status_code == 200
    get_database().tickets.update_one({"ticket_id": tid}, {"$set": {"invoice_match.run_count": 99}})
    e = c.get(f"{BASE}/eligibility/INV-5001", params={"ticket_id": tid}, headers=admin).json()
    assert not e["eligible"] and "APPROVAL_STALE" in {r["code"] for r in e["reasons"]}
