"""Invoice exception workflow after approval: hold / dispute / vendor response / release / reject, then a
corrected invoice re-match that unblocks the simulated payment. SIMULATION ONLY — nothing is sent or paid."""
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


def _approved_no_invoice(c, actors):
    admin, alice, carol = actors
    tid, ticket = approved_ticket(c, admin, alice, carol, invoice_id=None)
    assert ticket["status"] == "approved"
    return tid, ticket


def _match(c, admin, tid, invoice_id):
    r = c.post(f"/api/admin/tickets/{tid}/invoice-match", json={"invoice_id": invoice_id}, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


def _exc(c, admin, tid, action, note="Reason for this action"):
    return c.post(f"/api/admin/tickets/{tid}/invoice-exception", json={"action": action, "note": note},
                  headers=admin)


def _codes(c, admin, tid, invoice_id):
    e = c.get(f"{BASE}/eligibility/{invoice_id}", params={"ticket_id": tid}, headers=admin).json()
    return e["eligible"], {r["code"] for r in e["reasons"]}


def _body(tid, ticket, invoice_id, key="exc-key-0001"):
    po = ticket["workflow_result"]["procurement"]["po"]
    return {"ticket_id": tid, "invoice_id": invoice_id, "purchase_order_id": po["po_id"],
            "amount": f"{float(po['total']):.2f}", "currency": po["currency"], "simulation_only": True,
            "confirmed_by_user": True, "idempotency_key": key}


def test_dispute_then_corrected_invoice_enables_simulation(c, actors):
    admin, _, _ = actors
    tid, ticket = _approved_no_invoice(c, actors)
    view = _match(c, admin, tid, "INV-5002")
    assert view["invoice_status"] == "mismatch"
    eligible, codes = _codes(c, admin, tid, "INV-5002")
    assert not eligible and codes == {"INVOICE_MATCH_FAILED"}

    r = _exc(c, admin, tid, "dispute", "Rate 720 vs 700 and 5 meals short")
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["invoice_status"] == "disputed"
    exc = v["invoice_exception"]
    assert exc["state"] == "disputed" and exc["invoice_id"] == "INV-5002"
    assert any(ch["status"] == "fail" for ch in exc["failed_checks"])
    assert _codes(c, admin, tid, "INV-5002")[1] >= {"INVOICE_DISPUTED", "INVOICE_MATCH_FAILED"}

    assert _exc(c, admin, tid, "vendor_response", "Vendor will reissue the invoice").status_code == 200

    v = _match(c, admin, tid, "INV-5002-R")
    assert v["invoice_status"] == "matched" and v["invoice_exception"] is None
    assert v["invoice_match"]["invoice_id"] == "INV-5002-R"
    hist = v["invoice_history"]
    assert [h.get("action") for h in hist[:2]] == ["dispute", "vendor_response"]
    assert hist[-1]["invoice_id"] == "INV-5002" and hist[-1]["superseded_by"] == "INV-5002-R"
    assert hist[-1]["exception_state"] == "disputed"

    eligible, codes = _codes(c, admin, tid, "INV-5002-R")
    assert eligible, codes
    p = c.post(BASE, json=_body(tid, ticket, "INV-5002-R"), headers=admin)
    assert p.status_code == 201, p.text
    assert p.json()["payment"]["money_transferred"] is False

    audit = c.get(f"/api/admin/tickets/{tid}/audit", headers=admin).json()["events"]
    actions = [e["action"] for e in audit]
    assert "invoice_exception_dispute" in actions and "invoice_exception_vendor_response" in actions
    # After a payment exists, the invoice and its exceptions are frozen.
    assert _exc(c, admin, tid, "hold").status_code == 409


def test_hold_blocks_payment_and_release_restores(c, actors):
    admin, _, _ = actors
    tid, _ = _approved_no_invoice(c, actors)
    _match(c, admin, tid, "INV-5001")
    assert _codes(c, admin, tid, "INV-5001")[0]
    r = _exc(c, admin, tid, "hold", "Waiting for the signed delivery note")
    assert r.status_code == 200 and r.json()["invoice_status"] == "on_hold"
    eligible, codes = _codes(c, admin, tid, "INV-5001")
    assert not eligible and codes == {"INVOICE_ON_HOLD"}
    # A matched invoice can't be disputed, but it can be held and released.
    assert _exc(c, admin, tid, "release", "Delivery note received").status_code == 200
    assert _codes(c, admin, tid, "INV-5001")[0]
    assert _exc(c, admin, tid, "dispute").status_code == 409


def test_reject_blocks_until_rematch(c, actors):
    admin, _, _ = actors
    tid, _ = _approved_no_invoice(c, actors)
    _match(c, admin, tid, "INV-5002")
    assert _exc(c, admin, tid, "reject", "Vendor refused to correct").status_code == 200
    assert "INVOICE_REJECTED" in _codes(c, admin, tid, "INV-5002")[1]
    # Terminal for this invoice: no further transitions except a new match.
    assert _exc(c, admin, tid, "release").status_code == 409
    assert _exc(c, admin, tid, "dispute").status_code == 409
    v = _match(c, admin, tid, "INV-5002-R")
    assert v["invoice_exception"] is None and v["invoice_status"] == "matched"


def test_exception_rules_and_authorization(c, actors):
    admin, alice, carol = actors
    tid, _ = _approved_no_invoice(c, actors)
    # No invoice matched yet.
    assert _exc(c, admin, tid, "hold").status_code == 409
    _match(c, admin, tid, "INV-5002")
    for headers in (alice, carol):
        assert c.post(f"/api/admin/tickets/{tid}/invoice-exception", json={"action": "hold", "note": "x" * 10},
                      headers=headers).status_code == 403
    assert c.post(f"/api/admin/tickets/{tid}/invoice-exception", json={"action": "pay", "note": "x" * 10},
                  headers=admin).status_code == 422
    assert c.post(f"/api/admin/tickets/{tid}/invoice-exception", json={"action": "hold", "note": "no"},
                  headers=admin).status_code == 422
    assert c.post(f"/api/admin/tickets/{tid}/invoice-exception",
                  json={"action": "hold", "note": "long enough", "state": "released"},
                  headers=admin).status_code == 422
    assert _exc(c, admin, tid, "vendor_response").status_code == 409   # nothing disputed yet
    assert _exc(c, admin, tid, "release").status_code == 409           # not on hold


def test_workflow_run_invoice_cannot_take_exceptions(c, actors):
    admin, alice, carol = actors
    tid, _ = approved_ticket(c, admin, alice, carol, invoice_id="INV-5001")
    assert _exc(c, admin, tid, "hold").status_code == 409


def test_concurrent_exception_write_is_rejected(c, actors):
    from app.repositories.ticket_repository import set_invoice_exception
    admin, _, _ = actors
    tid, _ = _approved_no_invoice(c, actors)
    _match(c, admin, tid, "INV-5002")
    assert _exc(c, admin, tid, "hold").status_code == 200
    # A writer that read "no exception" must not overwrite the hold.
    assert set_invoice_exception(tid, {"invoice_id": "INV-5002", "state": "rejected"}, previous=None) is False
    assert get_database().tickets.find_one({"ticket_id": tid})["invoice_exception"]["state"] == "on_hold"


def test_admin_list_exposes_invoice_status(c, actors):
    admin, _, _ = actors
    tid, _ = _approved_no_invoice(c, actors)
    rows = {t["ticket_id"]: t for t in c.get("/api/admin/tickets", params={"status": "approved"},
                                             headers=admin).json()["tickets"]}
    assert rows[tid]["invoice_status"] == "needs_invoice"
    _match(c, admin, tid, "INV-5002")
    rows = {t["ticket_id"]: t for t in c.get("/api/admin/tickets", params={"status": "approved"},
                                             headers=admin).json()["tickets"]}
    assert rows[tid]["invoice_status"] == "mismatch"
