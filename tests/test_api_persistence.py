import re

import pytest
from fastapi.testclient import TestClient

from app.data_loader import load_json
from app.db import PersistenceError, get_database
from app.main import app
from app.repositories import get_ticket
from app.services import ticket_service

S = load_json("sample_requests.json")
TICKET_RE = re.compile(r"^REQ-\d{8}-[0-9A-F]{8}$")
TODAY = "2026-10-17"


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def _post(c, key="happy_path", **extra):
    return c.post("/api/workflow", json={"text": S[key], "today": TODAY, **extra})


def test_new_ticket_id_format():
    assert TICKET_RE.match(ticket_service.new_ticket_id())


def test_workflow_returns_unique_persisted_tickets(client):
    a, b = _post(client), _post(client)
    assert a.status_code == b.status_code == 200
    a, b = a.json(), b.json()
    assert TICKET_RE.match(a["ticket_id"]) and TICKET_RE.match(b["ticket_id"])
    assert a["ticket_id"] != b["ticket_id"]
    assert a["persisted"] is True and b["persisted"] is True


def test_ticket_and_po_persisted(client):
    res = _post(client).json()
    tid = res["ticket_id"]
    r = client.get(f"/api/tickets/{tid}")
    assert r.status_code == 200
    doc = r.json()
    assert "_id" not in doc
    assert doc["ticket_id"] == tid
    assert doc["status"] == res["final_status"] == "pass"
    assert doc["raw_text"] == S["happy_path"]
    assert doc["schema_version"] == 1
    assert doc["workflow_result"]["final_status"] == "pass"
    assert doc["workflow_result"]["procurement"]["po"]["po_id"] == "DRAFT-PO-" + tid[4:]
    assert res["procurement"]["po"]["po_id"] == "DRAFT-PO-" + tid[4:]
    po = get_database()["purchase_orders"].find_one({"ticket_id": tid})
    assert po is not None
    assert po["po_id"] == "DRAFT-PO-" + tid[4:]
    assert po["status"] == "draft"
    assert get_database()["purchase_orders"].count_documents({"ticket_id": tid}) == 1


def test_audit_events_ordered(client):
    res = _post(client).json()
    tid = res["ticket_id"]
    r = client.get(f"/api/tickets/{tid}/audit")
    assert r.status_code == 200
    body = r.json()
    assert body["ticket_id"] == tid
    seqs = [e["sequence"] for e in body["events"]]
    assert seqs == list(range(1, len(res["audit_trail"]) + 1))
    assert [e["action"] for e in body["events"]] == [e["action"] for e in res["audit_trail"]]


def test_recent_tickets(client):
    tid = _post(client).json()["ticket_id"]
    r = client.get("/api/tickets", params={"limit": 5})
    assert r.status_code == 200
    assert tid in [t["ticket_id"] for t in r.json()["tickets"]]
    assert client.get("/api/tickets", params={"limit": 0}).status_code == 422
    assert client.get("/api/tickets", params={"limit": 101}).status_code == 422


def test_unknown_and_malformed_ticket(client):
    assert client.get("/api/tickets/REQ-20261017-DEADBEEF").status_code == 404
    assert client.get("/api/tickets/REQ-20261017-DEADBEEF/audit").status_code == 404
    assert client.get("/api/tickets/not-a-ticket").status_code == 422
    assert client.get("/api/tickets/REQ-bad$id/audit").status_code == 422
    assert client.get("/api/tickets/REQ-20261017-DEADBEEF/audit", params={"limit": 501}).status_code == 422


def test_db_failure_on_create_returns_503(client, monkeypatch):
    def boom(*a, **k):
        raise PersistenceError()
    monkeypatch.setattr(ticket_service, "create_ticket", boom)
    r = _post(client)
    assert r.status_code == 503
    assert "mongodb://" not in r.text
    assert "detail" in r.json()


def test_failure_after_create_marks_persistence_error(client, monkeypatch):
    seen = {}
    real_create = ticket_service.create_ticket

    def spy_create(document, **k):
        seen["id"] = document["ticket_id"]
        return real_create(document, **k)

    def boom(*a, **k):
        raise PersistenceError()

    monkeypatch.setattr(ticket_service, "create_ticket", spy_create)
    monkeypatch.setattr(ticket_service, "update_ticket_result", boom)
    r = _post(client)
    assert r.status_code == 503
    assert "mongodb://" not in r.text
    doc = get_ticket(seen["id"])
    assert doc is not None and doc["status"] == "persistence_error"


def test_health_includes_database(client):
    r = client.get("/api/health")
    assert "mongodb://" not in r.text and "mongodb+srv://" not in r.text
    h = r.json()
    assert h["status"] == "ok"
    assert "llm_enabled" in h and "model" in h
    assert h["database"] == "connected"
    assert h["persistence"] == "required"


def test_disabled_mode(client, monkeypatch):
    monkeypatch.setenv("WORKPLACEOPS_PERSISTENCE", "disabled")
    r = _post(client)
    assert r.status_code == 200
    body = r.json()
    assert body["persisted"] is False
    assert TICKET_RE.match(body["ticket_id"])
    assert client.get(f"/api/tickets/{body['ticket_id']}").status_code == 503
    assert client.get(f"/api/tickets/{body['ticket_id']}/audit").status_code == 503
    assert client.get("/api/tickets").status_code == 503
    assert client.get("/api/health").json()["persistence"] == "disabled"


def test_over_budget_stores_no_po(client):
    res = _post(client, "over_budget").json()
    assert res["persisted"] is True
    assert res["procurement"]["po"] is None
    assert get_database()["purchase_orders"].count_documents({"ticket_id": res["ticket_id"]}) == 0
    assert client.get(f"/api/tickets/{res['ticket_id']}").json()["status"] == "blocked"


@pytest.mark.parametrize("invoice_id,expected,final", [
    ("INV-5001", "match", "pass"),
    ("INV-5002", "mismatch", "blocked"),
    ("INV-4987", "duplicate", "blocked"),
])
def test_invoice_outcomes_unchanged_with_unique_ticket_ids(client, invoice_id, expected, final):
    res = _post(client, invoice_id=invoice_id).json()
    assert res["procurement"]["po"]["po_id"] != "DRAFT-PO-1001"
    assert res["reconciliation"]["status"] == expected
    assert res["final_status"] == final
    saved = client.get(f"/api/tickets/{res['ticket_id']}").json()
    assert saved["workflow_result"]["reconciliation"]["status"] == expected
    rec = client.post("/api/reconcile", json={"po": res["procurement"]["po"], "invoice_id": invoice_id}).json()
    assert rec["status"] == expected


def test_invoice_for_other_vendor_still_mismatches():
    from datetime import date
    from app.agents import billing
    from app.orchestrator import run_workflow

    po = run_workflow(S["happy_path"], date(2026, 10, 17), request_id="REQ-20261017-ABCDEF12").procurement.po
    inv = billing.get_invoice("INV-5001").model_copy(update={"vendor_id": "VEN-999"})
    inv, delivery, rebound = billing.bind_demo_documents(inv, billing.get_delivery(inv.delivery_id), po)
    assert rebound and inv.po_id == po.po_id
    rec = billing.reconcile(inv, po, delivery, billing.load_history(), billing_rules())
    assert rec.status == "mismatch"


def billing_rules():
    from app.agents import policy
    return policy.load_rules()
