"""Role-based ticket lifecycle: user submissions, admin runs/decisions, ownership and transitions."""
import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from app.agents.intake import deterministic_parse
from app.db import PersistenceError, get_database
from app.main import app
from app.portal_schemas import TicketSubmission
from app.repositories import set_status
from app.services import ticket_service

TODAY = "2026-10-17"
HAPPY = {"title": "Quarterly team lunch", "event_category": "team_lunch", "service": "lunch",
         "event_date": "2026-10-23", "attendees": 100, "budget_inr": 100000, "location": "Bengaluru",
         "approved_vendor_only": True}
OVER_BUDGET = {**HAPPY, "title": "Big lunch", "attendees": 120, "budget_inr": 60000}


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def admin(admin_headers):
    return admin_headers


@pytest.fixture
def alice(user_headers_factory):
    return user_headers_factory("alice")


def _submit(c, headers, body=HAPPY):
    r = c.post("/api/my/tickets", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _run(c, admin, tid, **body):
    return c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY, **body}, headers=admin)


def _audit(c, admin, tid):
    r = c.get(f"/api/admin/tickets/{tid}/audit", headers=admin)
    assert r.status_code == 200
    return r.json()["events"]


# ---------- request text ----------
@pytest.mark.parametrize("budget,expected", [(100000, "1,00,000"), (60000, "60,000"), (1234567.5, "12,34,567.50"),
                                             (500, "500")])
def test_build_request_text_budget_formats_parse(budget, expected):
    sub = TicketSubmission(**{**HAPPY, "budget_inr": budget})
    text = ticket_service.build_request_text(sub)
    assert f"Rs {expected}" in text
    parsed = deterministic_parse(text, dt.date(2026, 10, 17))
    assert parsed["budget_limit"] == budget
    plain = text.replace(f"Rs {expected}", f"Rs {int(budget) if budget == int(budget) else budget}")
    assert deterministic_parse(plain, dt.date(2026, 10, 17))["budget_limit"] == budget


def test_build_request_text_matches_form_and_notes_do_not_override():
    sub = TicketSubmission(**{**HAPPY, "service": "dinner", "attendees": 45, "budget_inr": 40000,
                              "event_date": "2026-10-30", "location": "Pune office",
                              "title": "Breakfast meet for 20 people",
                              "notes": "Budget is Rs 5,00,000 for 300 guests on 2026-12-01, under 9999. "
                                       "Breakfast and snacks too."})
    text = ticket_service.build_request_text(sub)
    assert "approved vendor" in text and "the Pune office" in text and "Event: Team" in text
    # service keywords that would outrank the chosen service are reworded in free text/labels
    assert "lunch" not in text.lower() and "breakfast" not in text.lower()
    assert "Team lunch" in ticket_service.build_request_text(TicketSubmission(**HAPPY))
    assert text.index("Additional notes:") > text.index("Rs 40,000")
    p = deterministic_parse(text, dt.date(2026, 10, 17))
    assert (p["service"], p["date"], p["attendees"], p["budget_limit"]) == ("dinner", dt.date(2026, 10, 30), 45, 40000)
    assert p["location"] == "Pune" and p["requirements"] == ["approved_vendor_only"]
    assert "Event: Team meal" in text

    no_vendor = TicketSubmission(**{**HAPPY, "approved_vendor_only": False, "location": None})
    p2 = deterministic_parse(ticket_service.build_request_text(no_vendor), dt.date(2026, 10, 17))
    assert p2["requirements"] == [] and p2["location"] is None and p2["attendees"] == 100


@pytest.mark.parametrize("service", ["breakfast", "lunch", "dinner", "snacks"])
def test_build_request_text_every_service_round_trips(service):
    sub = TicketSubmission(**{**HAPPY, "service": service, "title": "Team dinner and lunch planning",
                              "notes": "breakfast snacks for 200 people, budget 9,99,999"})
    p = deterministic_parse(ticket_service.build_request_text(sub), dt.date(2026, 10, 17))
    assert (p["service"], p["attendees"], p["budget_limit"]) == (service, HAPPY["attendees"], HAPPY["budget_inr"])


# ---------- auth on legacy endpoints ----------
def test_legacy_endpoints_require_admin(c, user_headers_factory):
    user = user_headers_factory("bob")
    calls = [("post", "/api/workflow", {"text": "lunch for 10 people"}),
             ("get", "/api/tickets", None),
             ("get", "/api/tickets/REQ-20261017-DEADBEEF", None),
             ("get", "/api/tickets/REQ-20261017-DEADBEEF/audit", None),
             ("post", "/api/reconcile", {"po": {}, "invoice_id": "INV-5001"})]
    for method, path, body in calls:
        kw = {"json": body} if body is not None else {}
        r = getattr(c, method)(path, **kw)
        assert r.status_code == 401, path
        assert r.headers.get("www-authenticate") == "Bearer"
        r = getattr(c, method)(path, headers=user, **kw)
        assert r.status_code == 403, path
        assert r.json()["detail"] == "Admin access required."
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/samples").status_code == 200
    assert c.get("/api/invoices").status_code == 200


def test_portal_endpoints_require_auth(c, alice):
    assert c.get("/api/my/tickets").status_code == 401
    assert c.post("/api/my/tickets", json=HAPPY).status_code == 401
    assert c.get("/api/admin/tickets").status_code == 401
    assert c.get("/api/admin/tickets", headers=alice).status_code == 403
    assert c.post("/api/admin/tickets/REQ-X/run", json={}, headers=alice).status_code == 403


# ---------- submission & ownership ----------
def test_submit_creates_submitted_ticket_without_run(c, alice):
    view = _submit(c, alice)
    tid = view["ticket_id"]
    assert view["status"] == "submitted"
    assert view["outcome"] == "Waiting for an admin to review your request."
    assert view["po_summary"] is None
    assert view["submission"]["attendees"] == 100
    db = get_database()
    doc = db.tickets.find_one({"ticket_id": tid})
    assert doc["workflow_result"] is None and doc["run_count"] == 0 and doc["owner_username"] == "alice"
    assert db.purchase_orders.count_documents({"ticket_id": tid}) == 0
    events = list(db.agent_logs.find({"ticket_id": tid}))
    assert [(e["sequence"], e["agent"], e["action"]) for e in events] == [(1, "User", "submit_ticket")]


def test_submit_validation(c, alice):
    assert c.post("/api/my/tickets", json={**HAPPY, "attendees": 0}, headers=alice).status_code == 422
    assert c.post("/api/my/tickets", json={**HAPPY, "budget_inr": -5}, headers=alice).status_code == 422
    assert c.post("/api/my/tickets", json={**HAPPY, "title": "x"}, headers=alice).status_code == 422


def test_users_see_only_own_tickets(c, user_headers_factory):
    alice, bob = user_headers_factory("alice"), user_headers_factory("bob")
    a1 = _submit(c, alice)["ticket_id"]
    a2 = _submit(c, alice, {**HAPPY, "title": "Second lunch"})["ticket_id"]
    b1 = _submit(c, bob)["ticket_id"]
    mine = c.get("/api/my/tickets", headers=alice).json()["tickets"]
    assert {t["ticket_id"] for t in mine} == {a1, a2}
    assert all(t["owner_username"] is None for t in mine)
    assert {t["ticket_id"] for t in c.get("/api/my/tickets", headers=bob).json()["tickets"]} == {b1}

    assert c.get(f"/api/my/tickets/{a1}", headers=bob).status_code == 404
    assert c.put(f"/api/my/tickets/{a1}", json=HAPPY, headers=bob).status_code == 404
    assert c.post(f"/api/my/tickets/{a1}/notes", json={"text": "hi"}, headers=bob).status_code == 404
    assert c.get("/api/my/tickets/REQ-20261017-DEADBEEF", headers=alice).status_code == 404
    assert c.get("/api/my/tickets/not-a-ticket", headers=alice).status_code == 422
    assert c.get("/api/admin/tickets/REQ-bad$id", headers=alice).status_code in (403, 422)


def test_admin_list_counts_and_filter(c, admin, user_headers_factory):
    alice, bob = user_headers_factory("alice"), user_headers_factory("bob")
    t1 = _submit(c, alice)["ticket_id"]
    t2 = _submit(c, bob)["ticket_id"]
    assert _run(c, admin, t1).status_code == 200
    body = c.get("/api/admin/tickets", headers=admin).json()
    assert {t["ticket_id"] for t in body["tickets"]} == {t1, t2}
    owners = {t["ticket_id"]: t["owner_username"] for t in body["tickets"]}
    assert owners == {t1: "alice", t2: "bob"}
    assert body["counts"] == {"pending_approval": 1, "submitted": 1}
    reviewed = c.get("/api/admin/tickets", params={"status": "pending_approval"}, headers=admin).json()["tickets"]
    assert [t["ticket_id"] for t in reviewed] == [t1]
    assert reviewed[0]["final_status"] == "pass" and reviewed[0]["run_count"] == 1
    assert c.get("/api/admin/tickets", params={"status": "bogus"}, headers=admin).status_code == 422
    assert c.get("/api/admin/tickets/REQ-20261017-DEADBEEF", headers=admin).status_code == 404
    assert c.get("/api/admin/tickets/not-a-ticket", headers=admin).status_code == 422


# ---------- run / re-run / decide ----------
def _sequences_contiguous(events):
    return [e["sequence"] for e in events] == list(range(1, len(events) + 1))


def _approve_as_approver(c, approver, tid, comment="Looks good", key="key-approve-0001"):
    cur = c.get(f"/api/tickets/{tid}/approval", headers=approver).json()["current"]
    return c.post(f"/api/approvals/{cur['approval_id']}/decision", headers=approver, json={
        "action": "approve", "comment": comment, "idempotency_key": key,
        "request_version": cur["request_version"], "workflow_snapshot_hash": cur["workflow_snapshot_hash"]})


def test_run_rerun_approve_flow(c, admin, alice, approver_headers_factory):
    tid = _submit(c, alice)["ticket_id"]
    db = get_database()

    r = _run(c, admin, tid)
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["status"] == "pending_approval" and v["run_count"] == 1 and v["last_run_by"] == "admin"
    wr = v["workflow_result"]
    assert wr["persisted"] is True and wr["ticket_id"] == tid and wr["final_status"] == "pass"
    assert v["approval"]["current"]["status"] == "pending" and v["approval"]["current"]["request_version"] == 1
    po = db.purchase_orders.find_one({"ticket_id": tid})
    assert po["status"] == "draft" and po["po_id"] == "DRAFT-PO-" + tid[4:]
    events = _audit(c, admin, tid)
    assert _sequences_contiguous(events)
    assert events[0]["action"] == "submit_ticket" and events[1]["action"] == "start"
    n1 = len(wr["audit_trail"]) + 1  # submit + workflow events; approval events follow them
    assert [e["step"] for e in wr["audit_trail"]] == list(range(2, n1 + 1))
    assert [e["action"] for e in wr["audit_trail"]] == [e["action"] for e in events[1:n1]]
    assert [e["action"] for e in events[n1:]] == ["approval_requested"]

    r = _run(c, admin, tid)  # re-run while pending: v1 superseded, v2 pending
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["run_count"] == 2 and v["status"] == "pending_approval"
    assert [a["status"] for a in v["approval"]["history"]] == ["superseded", "pending"]
    events = _audit(c, admin, tid)
    assert _sequences_contiguous(events)
    steps = [e["step"] for e in v["workflow_result"]["audit_trail"]]
    # v1 is superseded as soon as the re-run claims the ticket, before the new workflow events
    assert events[n1 + 1]["action"] == "approval_superseded"
    assert steps == list(range(n1 + 3, n1 + 3 + len(steps)))
    assert events[-1]["action"] == "approval_requested"
    assert db.purchase_orders.count_documents({"ticket_id": tid}) == 1

    user_view = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    assert user_view["outcome"] == "Waiting for approval by an approver."
    assert user_view["po_summary"]["approval_status"] == "pending"

    # admins cannot approve; approval is reserved for an eligible approver
    r = c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "approve", "note": "Looks good"},
               headers=admin)
    assert r.status_code == 403
    assert r.json()["detail"] == "Approvals must be decided by an eligible approver via /api/approvals."
    assert db.tickets.find_one({"ticket_id": tid})["status"] == "pending_approval"

    approver = approver_headers_factory("carol")
    r = _approve_as_approver(c, approver, tid)
    assert r.status_code == 200, r.text
    assert r.json()["approval"]["status"] == "approved"
    v = c.get(f"/api/admin/tickets/{tid}", headers=admin).json()
    assert v["status"] == "approved"
    assert v["workflow_result"]["final_status"] == "pass"  # workflow outcome is never rewritten
    po = db.purchase_orders.find_one({"ticket_id": tid})
    assert po["approval_status"] == "approved" and po["status"] == "draft" and po["approval_by"] == "carol"
    events = _audit(c, admin, tid)
    assert events[-1]["agent"] == "Approver" and events[-1]["action"] == "approval_approved"
    assert _sequences_contiguous(events)

    user_view = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    assert user_view["status"] == "approved"
    assert user_view["outcome"] == (f"Approved — draft PO {po['po_id']} for ₹{ticket_service._inr(po['total'])} "
                                    "recorded. No purchase has been made yet.")
    assert user_view["po_summary"] == {"po_id": po["po_id"], "vendor_name": po["vendor_name"], "total": po["total"],
                                       "currency": "INR", "status": "draft", "approval_status": "approved"}
    assert [n["text"] for n in user_view["notes"]] == ["Looks good"]

    assert _run(c, admin, tid).status_code == 409
    assert c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "reject"}, headers=admin).status_code == 409
    assert c.put(f"/api/my/tickets/{tid}", json=HAPPY, headers=alice).status_code == 409


def test_decision_requires_reviewed(c, admin, alice):
    tid = _submit(c, alice)["ticket_id"]
    r = c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "reject"}, headers=admin)
    assert r.status_code == 409
    assert c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "approve"}, headers=admin).status_code == 403
    assert c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "maybe"}, headers=admin).status_code == 422
    assert get_database().tickets.find_one({"ticket_id": tid})["status"] == "submitted"


def test_admin_cannot_reject_pending_approval_via_legacy_decision(c, admin, alice):
    tid = _submit(c, alice)["ticket_id"]
    assert _run(c, admin, tid).json()["status"] == "pending_approval"
    r = c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "reject", "note": "no"}, headers=admin)
    assert r.status_code == 409
    assert get_database().tickets.find_one({"ticket_id": tid})["status"] == "pending_approval"


def test_blocked_cannot_be_approved_but_can_be_rejected(c, admin, alice):
    tid = _submit(c, alice, OVER_BUDGET)["ticket_id"]
    v = _run(c, admin, tid).json()
    assert v["status"] == "reviewed" and v["workflow_result"]["final_status"] == "blocked"
    assert v["approval"] is None
    assert get_database().purchase_orders.count_documents({"ticket_id": tid}) == 0
    assert get_database().approval_requests.count_documents({"ticket_id": tid}) == 0
    r = c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "approve"}, headers=admin)
    assert r.status_code == 403
    assert get_database().tickets.find_one({"ticket_id": tid})["status"] == "reviewed"
    r = c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "reject", "note": "Over budget"},
               headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    uv = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    assert uv["outcome"] == "Rejected by admin. Note: Over budget"
    assert uv["po_summary"] is None
    assert _audit(c, admin, tid)[-1]["action"] == "reject"


def test_needs_info_then_edit_and_resubmit(c, admin, alice, monkeypatch):
    real = ticket_service.build_request_text
    monkeypatch.setattr(ticket_service, "build_request_text",
                        lambda sub: real(sub).replace(f" on {sub.event_date.isoformat()}", ""))
    tid = _submit(c, alice)["ticket_id"]
    monkeypatch.setattr(ticket_service, "build_request_text", real)

    v = _run(c, admin, tid).json()
    assert v["status"] == "needs_info"
    assert v["workflow_result"]["final_status"] == "needs_clarification"
    assert v["approval"] is None
    assert c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "approve"}, headers=admin).status_code == 403
    assert c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "reject"}, headers=admin).status_code == 409

    uv = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    assert uv["outcome"] == "More information needed — please update your request."
    assert uv["clarification_questions"] and "date" in uv["clarification_questions"][0].lower()

    r = c.put(f"/api/my/tickets/{tid}", json={**HAPPY, "title": "Quarterly team lunch v2"}, headers=alice)
    assert r.status_code == 200, r.text
    uv = r.json()
    assert uv["status"] == "submitted" and uv["clarification_questions"] == []
    assert uv["submission"]["title"] == "Quarterly team lunch v2"
    events = _audit(c, admin, tid)
    assert events[-1]["action"] == "resubmit_ticket"

    v = _run(c, admin, tid).json()
    assert v["status"] == "pending_approval" and v["run_count"] == 2
    events = _audit(c, admin, tid)
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert c.put(f"/api/my/tickets/{tid}", json=HAPPY, headers=alice).status_code == 409


def test_user_can_edit_while_submitted(c, alice):
    tid = _submit(c, alice)["ticket_id"]
    r = c.put(f"/api/my/tickets/{tid}", json={**HAPPY, "attendees": 80}, headers=alice)
    assert r.status_code == 200 and r.json()["submission"]["attendees"] == 80
    doc = get_database().tickets.find_one({"ticket_id": tid})
    assert "80 employees" in doc["raw_text"]


def test_notes_and_user_view_hides_internals(c, admin, alice):
    tid = _submit(c, alice)["ticket_id"]
    r = c.post(f"/api/my/tickets/{tid}/notes", json={"text": "Vegetarian options please"}, headers=alice)
    assert r.status_code == 200
    assert c.post(f"/api/my/tickets/{tid}/notes", json={"text": ""}, headers=alice).status_code == 422
    assert _run(c, admin, tid, invoice_id="INV-5001").status_code == 200
    r = c.post(f"/api/admin/tickets/{tid}/notes", json={"text": "Checked with facilities"}, headers=admin)
    assert r.status_code == 200
    assert [n["role"] for n in r.json()["notes"]] == ["user", "admin"]

    raw = c.get(f"/api/my/tickets/{tid}", headers=alice)
    uv = raw.json()
    assert [n["text"] for n in uv["notes"]] == ["Vegetarian options please", "Checked with facilities"]
    assert [n["author"] for n in uv["notes"]] == ["alice", "admin"]

    def keys(o):
        if isinstance(o, dict):
            for k, val in o.items():
                yield k
                yield from keys(val)
        elif isinstance(o, list):
            for i in o:
                yield from keys(i)

    all_keys = set(keys(uv))
    for forbidden in ("vendors", "reconciliation", "audit_trail", "policy", "workflow_result", "raw_text",
                      "evaluations", "line_items", "critic"):
        assert forbidden not in all_keys
    assert "INV-5001" not in raw.text
    assert set(uv["po_summary"]) == {"po_id", "vendor_name", "total", "currency", "status", "approval_status"}


def test_invoice_run_admin_view(c, admin, alice):
    tid = _submit(c, alice)["ticket_id"]
    v = _run(c, admin, tid, invoice_id="INV-5001").json()
    assert v["workflow_result"]["final_status"] == "pass"
    assert v["workflow_result"]["reconciliation"]["status"] == "match"
    assert v["owner_username"] == "alice" and v["owner_email"] == "alice@example.com"
    assert "Bengaluru office" in v["raw_text"]
    assert c.get(f"/api/admin/tickets/{tid}", headers=admin).json() == v


def test_concurrent_run_guard(c, admin, alice):
    tid = _submit(c, alice)["ticket_id"]
    assert set_status(tid, "processing", expected={"submitted"}) is True
    r = _run(c, admin, tid)
    assert r.status_code == 409
    assert get_database().tickets.find_one({"ticket_id": tid})["status"] == "processing"
    assert set_status(tid, "processing", expected={"submitted"}) is False
    uv = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    assert uv["outcome"] == "Being processed."


def test_atomic_claim_loses_race(c, admin, alice, monkeypatch):
    """Status read says runnable, but another run claims it first: the conditional update fails -> 409."""
    tid = _submit(c, alice)["ticket_id"]
    real_set = ticket_service.set_status

    def racing(ticket_id, status, **kw):
        real_set(ticket_id, "processing", expected={"submitted"})  # the other request wins
        return real_set(ticket_id, status, **kw)

    monkeypatch.setattr(ticket_service, "set_status", racing)
    assert _run(c, admin, tid).status_code == 409


def test_run_persistence_failure_returns_503_and_retry_works(c, admin, alice, monkeypatch):
    tid = _submit(c, alice)["ticket_id"]

    def boom(*a, **k):
        raise PersistenceError()

    monkeypatch.setattr(ticket_service, "save_run_result", boom)
    r = _run(c, admin, tid)
    assert r.status_code == 503 and "mongodb://" not in r.text
    doc = get_database().tickets.find_one({"ticket_id": tid})
    assert doc["status"] == "persistence_error" and doc["workflow_result"] is None and doc["run_count"] == 0
    uv = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    assert uv["outcome"] == "Processing failed; an admin will retry."

    monkeypatch.undo()
    v = _run(c, admin, tid).json()
    assert v["status"] == "pending_approval" and v["run_count"] == 1 and v["error"] is None
    events = _audit(c, admin, tid)
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert get_database().purchase_orders.count_documents({"ticket_id": tid}) == 1


def test_workflow_exception_marks_failed(c, admin, alice, monkeypatch, caplog):
    tid = _submit(c, alice)["ticket_id"]

    def explode(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ticket_service, "run_workflow", explode)
    r = _run(c, admin, tid)
    assert r.status_code == 500 and "kaboom" not in r.text
    assert get_database().tickets.find_one({"ticket_id": tid})["status"] == "failed"
    monkeypatch.undo()
    assert _run(c, admin, tid).json()["status"] == "pending_approval"


def test_db_failure_on_submit_returns_503(c, alice, monkeypatch):
    def boom(*a, **k):
        raise PersistenceError()

    monkeypatch.setattr(ticket_service, "create_submitted_ticket", boom)
    r = c.post("/api/my/tickets", json=HAPPY, headers=alice)
    assert r.status_code == 503 and "detail" in r.json()


def test_request_text_not_logged(c, admin, alice, caplog):
    import logging
    secret_note = "Confidential note XYZZY-4242"
    with caplog.at_level(logging.DEBUG):
        tid = _submit(c, alice, {**HAPPY, "notes": secret_note})["ticket_id"]
        _run(c, admin, tid)
    assert secret_note not in caplog.text
    assert json.dumps(HAPPY["title"]) not in caplog.text
