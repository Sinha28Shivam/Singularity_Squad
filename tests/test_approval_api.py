"""Phase 2 approval API: authorization matrix, HTTP lifecycle, idempotency, expiry and fail-closed paths."""
import datetime as dt
import re

import pytest
from fastapi.testclient import TestClient

from app.approvals import service as approval_service
from app.db import PersistenceError, get_database
from app.main import app
from app.repositories import approval_repository
from app.services import ticket_service
from tests.conftest import APPROVER_PASSWORD, USER_PASSWORD

TODAY = "2026-10-17"
HAPPY = {"title": "Quarterly team lunch", "event_category": "team_lunch", "service": "lunch",
         "event_date": "2026-10-23", "attendees": 100, "budget_inr": 100000, "location": "Bengaluru",
         "approved_vendor_only": True}
OVER_BUDGET = {**HAPPY, "title": "Big lunch", "attendees": 120, "budget_inr": 60000}
FORBIDDEN_CLAIMS = re.compile(r"\b(ordered|paid|purchased)\b", re.IGNORECASE)
WRONG_ROLE = "Only an eligible approver can decide this request."
SELF_APPROVAL = "You cannot decide your own request."


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


@pytest.fixture
def carol(approver_headers_factory):
    return approver_headers_factory("carol")


def _submit(c, headers, body=HAPPY):
    r = c.post("/api/my/tickets", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["ticket_id"]


def _run(c, admin, tid, **body):
    r = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY, **body}, headers=admin)
    return r


def _pending_ticket(c, admin, owner, body=HAPPY):
    tid = _submit(c, owner, body)
    r = _run(c, admin, tid)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    return tid, r.json()["approval"]["current"]


def _body(cur, action="approve", key="key-0000-0001", comment=None, **extra):
    b = {"action": action, "idempotency_key": key, "request_version": cur["request_version"],
         "workflow_snapshot_hash": cur["workflow_snapshot_hash"], **extra}
    if comment is not None:
        b["comment"] = comment
    return b


def _decide(c, headers, cur, **kw):
    return c.post(f"/api/approvals/{cur['approval_id']}/decision", json=_body(cur, **kw), headers=headers)


def _approval_doc(approval_id):
    return get_database().approval_requests.find_one({"approval_id": approval_id}, {"_id": 0})


def _ticket(tid):
    return get_database().tickets.find_one({"ticket_id": tid}, {"_id": 0})


def _events(tid):
    return list(get_database().agent_logs.find({"ticket_id": tid}, {"_id": 0}).sort("sequence", 1))


def _assert_safe(text: str):
    assert not FORBIDDEN_CLAIMS.search(text), text
    for secret in ("mongodb://", "password", "salt", "token_hash", USER_PASSWORD, APPROVER_PASSWORD,
                   "Traceback"):
        assert secret not in text


# ---------- approver accounts ----------
def test_approver_seeding_is_server_side_and_never_escalates(c, monkeypatch, admin_headers):
    from app.auth.service import seed_approvers_from_env

    assert c.post("/api/auth/signup", json={"username": "mallory", "name": "M", "email": "m@example.com",
                                            "password": USER_PASSWORD, "role": "approver"}).json()["user"][
        "role"] == "user"
    monkeypatch.setenv("APPROVER_ACCOUNTS", "mallory:approver-pass-999, admin:approver-pass-999, bad entry,"
                                            "dave:" + APPROVER_PASSWORD)
    assert seed_approvers_from_env() == 1
    users = get_database().users
    assert users.find_one({"username": "mallory"})["role"] == "user"
    assert users.find_one({"username": "admin"})["role"] == "admin"
    assert users.find_one({"username": "dave"})["role"] == "approver"
    assert c.post("/api/auth/login", json={"username": "mallory", "password": "approver-pass-999"}).status_code == 401
    assert seed_approvers_from_env() == 1  # idempotent refresh
    assert users.count_documents({"username": "dave"}) == 1
    monkeypatch.delenv("APPROVER_ACCOUNTS")
    assert seed_approvers_from_env() == 0


# ---------- authorization matrix ----------
def test_unauthenticated_requests_get_401(c):
    calls = [("get", "/api/approvals", None), ("get", "/api/approvals/APR-20261017-deadbeef", None),
             ("post", "/api/approvals/APR-20261017-deadbeef/decision", {"action": "approve"}),
             ("get", "/api/tickets/REQ-20261017-DEADBEEF/approval", None),
             ("post", "/api/admin/approvals/APR-20261017-deadbeef/reconcile", None)]
    for method, path, body in calls:
        kw = {"json": body} if body is not None else {}
        r = getattr(c, method)(path, **kw)
        assert r.status_code == 401, path
        assert r.headers.get("www-authenticate") == "Bearer"


def test_authz_matrix(c, admin, alice, carol, user_headers_factory):
    tid, cur = _pending_ticket(c, admin, alice)
    assert "PO_REQUIRES_HUMAN_APPROVAL" in cur["reason_codes"] and "AMOUNT_THRESHOLD" in cur["reason_codes"]
    assert cur["requester_id"] == "alice" and cur["required_role"] == "approver"
    bob = user_headers_factory("bob")

    # queue: approvers and admins only
    assert c.get("/api/approvals", headers=alice).status_code == 403
    q = c.get("/api/approvals", params={"status": "pending"}, headers=carol)
    assert q.status_code == 200
    assert [a["approval_id"] for a in q.json()["approvals"]] == [cur["approval_id"]]
    assert q.json()["approvals"][0]["can_decide"] is True
    assert q.json()["counts"] == {"pending": 1}
    assert c.get("/api/approvals", headers=admin).json()["approvals"][0]["can_decide"] is False
    assert c.get("/api/approvals", params={"status": "bogus"}, headers=carol).status_code == 422

    # detail: requester sees own; other users get 404
    own = c.get(f"/api/approvals/{cur['approval_id']}", headers=alice)
    assert own.status_code == 200 and own.json()["can_decide"] is False
    assert own.json()["cannot_decide_reason"] == WRONG_ROLE
    assert c.get(f"/api/approvals/{cur['approval_id']}", headers=bob).status_code == 404
    assert c.get(f"/api/tickets/{tid}/approval", headers=bob).status_code == 404
    assert c.get("/api/approvals/APR-20261017-00000000", headers=carol).status_code == 404
    assert c.get("/api/approvals/not-an-id", headers=carol).status_code == 422

    # decisions: requester 403, admin 403, other user 404, approver 200
    r = _decide(c, alice, cur, key="key-alice-0001")
    assert r.status_code == 403 and r.json()["detail"] == WRONG_ROLE
    r = _decide(c, admin, cur, key="key-admin-0001")
    assert r.status_code == 403 and r.json()["detail"] == WRONG_ROLE
    assert _decide(c, bob, cur, key="key-bob-00001").status_code == 404
    doc = _approval_doc(cur["approval_id"])
    assert doc["status"] == "pending" and doc["decisions"] == []
    assert _ticket(tid)["status"] == "pending_approval"
    assert "approval_denied" in [e["action"] for e in _events(tid)]

    r = _decide(c, carol, cur, comment="Within policy.")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approval"]["status"] == "approved" and body["replayed"] is False
    assert body["reconciliation_required"] is False
    assert body["approval"]["decisions"][0]["actor_id"] == "carol"
    assert body["approval"]["decisions"][0]["actor_role"] == "approver"
    assert body["approval"]["safety_notice"] == ("Approval records internal authorization only. "
                                                 "No order or payment is executed.")
    _assert_safe(r.text)

    t = _ticket(tid)
    assert t["status"] == "approved" and t["workflow_result"]["final_status"] == "pass"
    po = get_database().purchase_orders.find_one({"ticket_id": tid})
    assert po["status"] == "draft" and po["approval_status"] == "approved" and po["approval_by"] == "carol"

    # legacy ticket routes stay admin-only
    for h in (alice, carol):
        assert c.get(f"/api/tickets/{tid}", headers=h).status_code == 403
        assert c.get(f"/api/tickets/{tid}/audit", headers=h).status_code == 403
    assert c.get(f"/api/tickets/{tid}", headers=admin).status_code == 200
    assert c.post(f"/api/admin/approvals/{cur['approval_id']}/reconcile", headers=carol).status_code == 403
    assert c.post(f"/api/admin/approvals/{cur['approval_id']}/reconcile", headers=alice).status_code == 403


def test_approver_cannot_approve_own_ticket(c, admin, approver_headers_factory):
    carol = approver_headers_factory("carol")
    dave = approver_headers_factory("dave")
    tid, cur = _pending_ticket(c, admin, carol)
    assert cur["requester_id"] == "carol"
    view = c.get(f"/api/approvals/{cur['approval_id']}", headers=carol).json()
    assert view["can_decide"] is False and view["cannot_decide_reason"] == SELF_APPROVAL
    r = _decide(c, carol, cur)
    assert r.status_code == 403 and r.json()["detail"] == SELF_APPROVAL
    assert _approval_doc(cur["approval_id"])["status"] == "pending"
    assert _decide(c, dave, cur, key="key-dave-00001").status_code == 200
    assert _ticket(tid)["status"] == "approved"


@pytest.mark.parametrize("extra", [{"actor_id": "carol"}, {"role": "approver"}, {"status": "approved"},
                                   {"requester_id": "x"}])
def test_body_cannot_smuggle_identity_or_status(c, admin, alice, extra):
    _, cur = _pending_ticket(c, admin, alice)
    r = _decide(c, alice, cur, **extra)
    assert r.status_code == 422
    assert _approval_doc(cur["approval_id"])["status"] == "pending"


def test_decision_body_validation(c, admin, alice, carol):
    _, cur = _pending_ticket(c, admin, alice)
    assert _decide(c, carol, cur, action="maybe").status_code == 422
    assert _decide(c, carol, cur, key="short").status_code == 422
    r = c.post(f"/api/approvals/{cur['approval_id']}/decision", headers=carol,
               json={**_body(cur), "workflow_snapshot_hash": "NOT-HEX!"})
    assert r.status_code == 422
    for action in ("reject", "request_changes"):
        r = _decide(c, carol, cur, action=action, comment="   ")
        assert r.status_code == 422
        assert r.json()["detail"] == "A comment is required to reject or request changes."
    assert _approval_doc(cur["approval_id"])["status"] == "pending"


# ---------- lifecycle ----------
def test_reject_by_approver(c, admin, alice, carol):
    tid, cur = _pending_ticket(c, admin, alice)
    r = _decide(c, carol, cur, action="reject", comment="Too expensive for this quarter.")
    assert r.status_code == 200 and r.json()["approval"]["status"] == "rejected"
    _assert_safe(r.text)
    assert _ticket(tid)["status"] == "rejected"
    po = get_database().purchase_orders.find_one({"ticket_id": tid})
    assert po["status"] == "draft" and po["approval_status"] == "rejected"
    uv = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    assert uv["status"] == "rejected"
    assert uv["outcome"] == "Rejected by an approver. Note: Too expensive for this quarter."
    assert uv["approval"]["current"]["decisions"][0]["comment"] == "Too expensive for this quarter."
    assert _run(c, admin, tid).status_code == 409
    assert c.put(f"/api/my/tickets/{tid}", json=HAPPY, headers=alice).status_code == 409


def test_request_changes_edit_rerun_creates_v2(c, admin, alice, carol):
    tid, v1 = _pending_ticket(c, admin, alice)
    assert c.put(f"/api/my/tickets/{tid}", json=HAPPY, headers=alice).status_code == 409  # not editable

    r = _decide(c, carol, v1, action="request_changes", comment="Please reduce headcount to 80.")
    assert r.status_code == 200 and r.json()["approval"]["status"] == "changes_requested"
    uv = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    assert uv["status"] == "changes_requested"
    assert uv["outcome"] == "Changes requested — please update your request."
    assert uv["approval"]["current"]["status"] == "changes_requested"

    r = c.put(f"/api/my/tickets/{tid}", json={**HAPPY, "attendees": 80}, headers=alice)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    assert _ticket(tid)["status"] == "submitted"

    r = _run(c, admin, tid)
    assert r.status_code == 200
    v = r.json()
    assert v["status"] == "pending_approval"
    hist = v["approval"]["history"]
    assert [(a["request_version"], a["status"]) for a in hist] == [(1, "changes_requested"), (2, "pending")]
    v2 = v["approval"]["current"]
    assert v2["workflow_snapshot_hash"] != v1["workflow_snapshot_hash"]
    assert v2["review_snapshot"]["request"]["attendees"] == 80

    # the old version can't be decided any more
    r = _decide(c, carol, v1, key="key-old-v1-0001")
    assert r.status_code == 409
    # stale hash against the current version
    stale = {**v2, "workflow_snapshot_hash": v1["workflow_snapshot_hash"]}
    r = _decide(c, carol, stale, key="key-stale-0001")
    assert r.status_code == 409
    assert r.json()["detail"] == "The request has changed; reload and review the latest version."
    assert _decide(c, carol, v2, key="key-v2-approve1").status_code == 200
    assert _ticket(tid)["status"] == "approved"
    events = _events(tid)
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))


def test_rerun_supersedes_pending(c, admin, alice, carol):
    tid, v1 = _pending_ticket(c, admin, alice)
    r = _run(c, admin, tid)
    assert r.status_code == 200
    v2 = r.json()["approval"]["current"]
    assert v2["request_version"] == 2 and v2["status"] == "pending"
    assert _approval_doc(v1["approval_id"])["status"] == "superseded"
    assert get_database().approval_requests.count_documents({"ticket_id": tid, "status": "pending"}) == 1
    r = _decide(c, carol, v1)
    assert r.status_code == 409
    assert r.json()["detail"] == "This approval has already been decided or the request has changed."
    assert "approval_superseded" in [e["action"] for e in _events(tid)]


def test_expiry_blocks_approval_and_allows_rerun(c, admin, alice, carol):
    tid, cur = _pending_ticket(c, admin, alice)
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    get_database().approval_requests.update_one({"approval_id": cur["approval_id"]}, {"$set": {"expires_at": past}})

    r = _decide(c, carol, cur)
    assert r.status_code == 409 and r.json()["detail"] == "Approval request has expired."
    doc = _approval_doc(cur["approval_id"])
    assert doc["status"] == "expired" and doc["decisions"] == []
    assert _ticket(tid)["status"] == "approval_expired"
    assert "approval_expired" in [e["action"] for e in _events(tid)]
    uv = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    assert uv["outcome"] == "Approval request expired — an admin will re-run it."

    r = _run(c, admin, tid)
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    assert r.json()["approval"]["current"]["request_version"] == 2


def test_lazy_expiry_on_read_with_monkeypatched_clock(c, admin, alice, carol, monkeypatch):
    tid, cur = _pending_ticket(c, admin, alice)
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=73)
    monkeypatch.setattr(approval_service, "_now", lambda: future)
    view = c.get(f"/api/approvals/{cur['approval_id']}", headers=carol).json()
    assert view["status"] == "expired" and view["can_decide"] is False
    assert _ticket(tid)["status"] == "approval_expired"
    assert _decide(c, carol, cur).status_code == 409


def test_hard_blocked_run_has_no_approval_and_admin_can_reject(c, admin, alice, carol):
    tid = _submit(c, alice, OVER_BUDGET)
    v = _run(c, admin, tid).json()
    assert v["status"] == "reviewed" and v["workflow_result"]["final_status"] == "blocked"
    assert v["approval"] is None
    assert get_database().approval_requests.count_documents({"ticket_id": tid}) == 0
    ta = c.get(f"/api/tickets/{tid}/approval", headers=carol).json()
    assert ta["current"] is None and ta["history"] == []
    assert ta["requirement"]["blocked"] is True and ta["requirement"]["required"] is False
    assert c.get("/api/approvals", headers=carol).json()["approvals"] == []
    assert c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "approve"}, headers=admin).status_code == 403
    r = c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "reject", "note": "Over budget"},
               headers=admin)
    assert r.status_code == 200 and r.json()["status"] == "rejected"


# ---------- idempotency ----------
def test_idempotent_replay_and_conflicts(c, admin, alice, approver_headers_factory):
    carol = approver_headers_factory("carol")
    dave = approver_headers_factory("dave")
    tid, cur = _pending_ticket(c, admin, alice)
    first = _decide(c, carol, cur, key="key-replay-0001", comment="ok")
    assert first.status_code == 200
    n_events = len(_events(tid))

    again = _decide(c, carol, cur, key="key-replay-0001", comment="ok")
    assert again.status_code == 200 and again.json()["replayed"] is True
    assert again.json()["approval"]["decisions"] == first.json()["approval"]["decisions"]
    assert len(_approval_doc(cur["approval_id"])["decisions"]) == 1
    assert len(_events(tid)) == n_events

    r = _decide(c, carol, cur, key="key-replay-0001", comment="different")
    assert r.status_code == 409
    assert r.json()["detail"] == "Idempotency key was already used with a different request."
    r = _decide(c, carol, cur, key="key-replay-0002", action="reject", comment="changed my mind")
    assert r.status_code == 409
    assert r.json()["detail"] == "This approval has already been decided or the request has changed."
    assert _decide(c, dave, cur, key="key-dave-00001").status_code == 409
    doc = _approval_doc(cur["approval_id"])
    assert doc["status"] == "approved" and len(doc["decisions"]) == 1
    assert _ticket(tid)["status"] == "approved"


# ---------- fail closed ----------
def test_db_failure_on_decision_returns_503_without_success(c, admin, alice, carol, monkeypatch):
    tid, cur = _pending_ticket(c, admin, alice)

    def boom(*a, **k):
        raise PersistenceError()

    monkeypatch.setattr(approval_repository, "decide_atomic", boom)
    r = _decide(c, carol, cur)
    assert r.status_code == 503
    assert "approved" not in r.text.lower() and "mongodb://" not in r.text
    monkeypatch.undo()
    assert _approval_doc(cur["approval_id"])["status"] == "pending"
    assert _ticket(tid)["status"] == "pending_approval"

    monkeypatch.setattr(approval_repository, "get_approval", boom)
    assert c.get(f"/api/approvals/{cur['approval_id']}", headers=carol).status_code == 503
    assert c.get("/api/approvals", headers=carol).status_code in (200, 503)


def test_policy_failure_fails_closed(c, admin, alice, monkeypatch):
    tid = _submit(c, alice)

    def broken(*a, **k):
        raise RuntimeError("policy config missing")

    monkeypatch.setattr(ticket_service, "evaluate_approval_requirement", broken)
    r = _run(c, admin, tid)
    assert r.status_code == 503 and "policy config missing" not in r.text
    t = _ticket(tid)
    assert t["status"] == "reviewed" and t["status"] != "approved"
    assert get_database().approval_requests.count_documents({"ticket_id": tid}) == 0
    assert c.post(f"/api/admin/tickets/{tid}/decision", json={"decision": "approve"}, headers=admin).status_code == 403
    monkeypatch.undo()
    r = _run(c, admin, tid)
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"


def test_approval_creation_failure_does_not_leave_ticket_runnable_as_approved(c, admin, alice, monkeypatch):
    tid = _submit(c, alice)

    def boom(*a, **k):
        raise PersistenceError()

    monkeypatch.setattr(approval_repository, "create_approval", boom)
    r = _run(c, admin, tid)
    assert r.status_code == 503
    assert _ticket(tid)["status"] == "persistence_error"
    assert get_database().approval_requests.count_documents({"ticket_id": tid, "status": "pending"}) == 0
    monkeypatch.undo()
    assert _run(c, admin, tid).json()["status"] == "pending_approval"


def test_secondary_failure_flags_reconciliation_and_admin_reconciles(c, admin, alice, carol, monkeypatch):
    tid, cur = _pending_ticket(c, admin, alice)

    def boom(*a, **k):
        raise PersistenceError()

    monkeypatch.setattr(approval_service, "set_po_approval", boom)
    r = _decide(c, carol, cur)
    assert r.status_code == 200
    body = r.json()
    assert body["approval"]["status"] == "approved" and body["reconciliation_required"] is True
    _assert_safe(r.text)
    admin_view = c.get(f"/api/admin/tickets/{tid}", headers=admin).json()
    assert admin_view["approval"]["current"]["reconciliation_required"] is True
    monkeypatch.undo()

    r = c.post(f"/api/admin/approvals/{cur['approval_id']}/reconcile", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["reconciliation_required"] is False and r.json()["status"] == "approved"
    po = get_database().purchase_orders.find_one({"ticket_id": tid})
    assert po["status"] == "draft" and po["approval_status"] == "approved"
    assert len(_approval_doc(cur["approval_id"])["decisions"]) == 1


# ---------- views ----------
def test_requester_views_hide_internals(c, admin, alice, carol):
    tid, cur = _pending_ticket(c, admin, alice)
    _decide(c, carol, cur, comment="Approved for the offsite.")
    for path in (f"/api/my/tickets/{tid}", f"/api/tickets/{tid}/approval"):
        r = c.get(path, headers=alice)
        assert r.status_code == 200
        _assert_safe(r.text)
        for internal in ("review_snapshot", "workflow_snapshot_hash", "idempotency_key", "line_items",
                         "vendor_id", "audit_trail"):
            assert internal not in r.text, (path, internal)
    approval = c.get(f"/api/my/tickets/{tid}", headers=alice).json()["approval"]
    assert approval["safety_notice"] == ("Approval records internal authorization only. "
                                         "No order or payment is executed.")
    assert approval["current"]["status"] == "approved" and approval["current"]["reasons"]
    assert approval["current"]["decisions"][0]["comment"] == "Approved for the offsite."
    full = c.get(f"/api/tickets/{tid}/approval", headers=carol).json()
    assert full["current"]["review_snapshot"]["po"]["po_id"] == "DRAFT-PO-" + tid[4:]
    assert full["current"]["workflow_snapshot_hash"] == cur["workflow_snapshot_hash"]
    _assert_safe(c.get(f"/api/approvals/{cur['approval_id']}", headers=carol).text)
