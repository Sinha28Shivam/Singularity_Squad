"""Role-specific pending-actions inbox (derived, read-only)."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db import get_database
from app.main import app
from tests.test_payment_api import HAPPY, approved_ticket


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def actors(admin_headers, user_headers_factory, approver_headers_factory):
    return admin_headers, user_headers_factory("alice"), approver_headers_factory("carol")


def _items(c, headers):
    r = c.get("/api/inbox", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _for(data, tid):
    return [i for i in data["items"] if i["ticket_id"] == tid]


def test_requires_auth(c):
    assert c.get("/api/inbox").status_code == 401


def test_admin_sees_new_request_then_invoice_work(c, actors):
    admin, alice, carol = actors
    tid = c.post("/api/my/tickets", json=HAPPY, headers=alice).json()["ticket_id"]
    assert [i["kind"] for i in _for(_items(c, admin), tid)] == ["run_workflow"]

    tid2, _ = approved_ticket(c, admin, alice, carol, invoice_id=None)
    item = _for(_items(c, admin), tid2)[0]
    assert item["kind"] == "match_invoice" and item["link"] == f"/admin/requests/{tid2}"
    c.post(f"/api/admin/tickets/{tid2}/invoice-match", json={"invoice_id": "INV-5002"}, headers=admin)
    item = _for(_items(c, admin), tid2)[0]
    assert item["kind"] == "resolve_invoice" and item["priority"] == 1
    c.post(f"/api/admin/tickets/{tid2}/invoice-exception", json={"action": "dispute", "note": "rate mismatch"},
           headers=admin)
    assert _for(_items(c, admin), tid2)[0]["kind"] == "await_vendor"

    tid3, _ = approved_ticket(c, admin, alice, carol, invoice_id="INV-5001")
    item = _for(_items(c, admin), tid3)[0]
    assert item["kind"] == "simulate_payment" and item["link"] == "/admin/payments"


def test_approver_inbox_lists_pending_and_flags_stalled(c, actors, monkeypatch):
    admin, alice, carol = actors
    tid, _ = approved_ticket(c, admin, alice, carol, approve=False)
    data = _items(c, carol)
    mine = _for(data, tid)
    assert mine and mine[0]["kind"] == "decide_approval" and mine[0]["link"].startswith("/approvals/APR")
    assert not mine[0]["stalled"]
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    get_database().approval_requests.update_many({"ticket_id": tid}, {"$set": {"created_at": old}})
    data = _items(c, carol)
    assert _for(data, tid)[0]["stalled"] and data["stalled"] >= 1
    # Requesters see only their own actions; the approval isn't theirs to decide.
    assert all(i["kind"] in {"provide_information", "resubmit_request"} for i in _items(c, alice)["items"])


def test_approver_never_sees_own_request(c, actors, approver_headers_factory):
    admin, alice, carol = actors
    tid, _ = approved_ticket(c, admin, alice, carol, approve=False)
    get_database().approval_requests.update_many({"ticket_id": tid}, {"$set": {"requester_id": "carol"}})
    assert not _for(_items(c, carol), tid)
