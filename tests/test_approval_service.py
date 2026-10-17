"""Approval lifecycle service tests (mongomock via the autouse conftest fixture)."""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from app.approvals import errors as E
from app.approvals import service
from app.approvals.errors import ApprovalError
from app.approvals.policy import evaluate_approval_requirement
from app.approvals.schemas import DecisionIn
from app.db.indexes import ensure_indexes
from app.db.mongo import get_database
from app.orchestrator import run_workflow
from app.portal_schemas import UserPublic
from app.repositories import approval_repository as repo
from app.repositories.audit_repository import get_audit_entries
from app.repositories.purchase_order_repository import get_draft_pos_for_ticket, upsert_draft_po
from app.repositories.ticket_repository import create_ticket, get_ticket

HAPPY = "Team lunch for 100 people on 2026-10-23 in Bangalore, budget INR 100000"
BLOCKED = "Team lunch for 100 people on 2026-10-23, budget INR 50000"

REQUESTER = UserPublic(username="alice", name="Alice", email="alice@example.com", role="user")
APPROVER = UserPublic(username="bob", name="Bob", email="bob@example.com", role="approver")
APPROVER2 = UserPublic(username="carol", name="Carol", email="carol@example.com", role="approver")
ADMIN = UserPublic(username="admin", name="Admin", email="admin@example.com", role="admin")
OTHER_USER = UserPublic(username="mallory", name="Mallory", email="m@example.com", role="user")


def make_ticket(text=HAPPY, owner="alice", ticket_id=None, run_count=1):
    ensure_indexes()
    ticket_id = ticket_id or f"REQ-20261017-{uuid.uuid4().hex[:8].upper()}"
    wr = run_workflow(text, date(2026, 10, 17), "INV-5001", request_id=ticket_id).model_dump(mode="json")
    wr["ticket_id"] = ticket_id
    req = evaluate_approval_requirement(wr)
    create_ticket({"ticket_id": ticket_id, "status": "pending_approval" if req.required else "reviewed",
                   "owner_username": owner, "owner_name": owner.title(), "owner_email": f"{owner}@example.com",
                   "submission": None, "raw_text": text, "workflow_result": wr, "notes": [], "decision": None,
                   "run_count": run_count})
    po = (wr.get("procurement") or {}).get("po")
    if po:
        upsert_draft_po(ticket_id, po, datetime.now(timezone.utc))
    return get_ticket(ticket_id), wr, req


def make_approval(text=HAPPY, owner="alice", now=None):
    ticket, wr, req = make_ticket(text, owner)
    doc = service.on_workflow_completed(ticket, wr, req, now=now)
    return ticket, doc


def body(doc, action="approve", comment=None, key=None, **over):
    data = {"action": action, "comment": comment, "idempotency_key": key or uuid.uuid4().hex,
            "request_version": doc["request_version"], "workflow_snapshot_hash": doc["workflow_snapshot_hash"]}
    data.update(over)
    return DecisionIn(**data)


def actions(ticket_id):
    return [e["action"] for e in get_audit_entries(ticket_id)]


# ---------- creation ----------
def test_create_pending_request():
    ticket, doc = make_approval()
    assert doc["status"] == "pending" and doc["request_version"] == 1
    assert doc["approval_id"].startswith("APR-")
    assert doc["requester_id"] == "alice" and doc["required_role"] == "approver"
    assert "PO_REQUIRES_HUMAN_APPROVAL" in doc["reason_codes"] and "AMOUNT_THRESHOLD" in doc["reason_codes"]
    assert doc["amount"] == 76125.0 and doc["ticket_version"] == 1
    assert len(doc["workflow_snapshot_hash"]) == 64
    assert doc["expires_at"] - doc["created_at"] == timedelta(hours=72)
    assert "approval_requested" in actions(ticket["ticket_id"])
    stored = repo.get_approval(doc["approval_id"])
    assert stored["review_snapshot"] == doc["review_snapshot"]
    assert stored["created_at"].tzinfo is not None


def test_blocked_and_clarification_create_nothing():
    ticket, wr, req = make_ticket(BLOCKED)
    assert req.blocked
    assert service.on_workflow_completed(ticket, wr, req) is None
    assert repo.list_for_ticket(ticket["ticket_id"]) == []
    t2, wr2, req2 = make_ticket("Team lunch for 100 people")
    assert req2.needs_clarification and service.on_workflow_completed(t2, wr2, req2) is None


def test_indexes_created():
    ensure_indexes()
    names = set(get_database().approval_requests.index_information())
    assert {"approval_id_unique", "ticket_version_unique", "status_created", "requester_created"} <= names


def test_supersede_on_new_run():
    ticket, v1 = make_approval()
    wr = ticket["workflow_result"]
    ticket2 = {**ticket, "run_count": 2}
    v2 = service.on_workflow_completed(ticket2, wr, evaluate_approval_requirement(wr))
    assert v2["request_version"] == 2 and v2["status"] == "pending"
    assert v2["workflow_snapshot_hash"] != v1["workflow_snapshot_hash"]
    assert repo.get_approval(v1["approval_id"])["status"] == "superseded"
    assert [d["status"] for d in repo.list_for_ticket(ticket["ticket_id"])] == ["superseded", "pending"]
    assert "approval_superseded" in actions(ticket["ticket_id"])
    with pytest.raises(ApprovalError) as ei:
        service.decide(v1["approval_id"], APPROVER, body(v1))
    assert ei.value.status_code == 409


def test_second_pending_request_is_prevented():
    ticket, v1 = make_approval()
    dup = dict(v1, approval_id=service.new_approval_id(), request_version=2)
    with pytest.raises(repo.ApprovalConflictError):
        repo.create_approval(dup)


def test_new_run_not_required_supersedes_only():
    ticket, v1 = make_approval()
    blocked_wr = run_workflow(BLOCKED, date(2026, 10, 17), None, request_id=ticket["ticket_id"]).model_dump(mode="json")
    assert service.on_workflow_completed(ticket, blocked_wr, evaluate_approval_requirement(blocked_wr)) is None
    assert repo.get_approval(v1["approval_id"])["status"] == "superseded"


# ---------- decisions ----------
def test_approve_happy_path():
    ticket, doc = make_approval()
    after, replayed = service.decide(doc["approval_id"], APPROVER, body(doc, comment="  Looks fine.  "))
    assert not replayed and after["status"] == "approved" and not after["reconciliation_required"]
    assert len(after["decisions"]) == 1
    dec = after["decisions"][0]
    assert dec["actor_id"] == "bob" and dec["actor_role"] == "approver" and dec["comment"] == "Looks fine."
    assert dec["decision_id"].startswith("DEC-") and after["decided_at"] is not None
    t = get_ticket(ticket["ticket_id"])
    assert t["status"] == "approved"
    assert t["workflow_result"]["final_status"] == "pass"
    assert any(n.get("decision_id") == dec["decision_id"] for n in t["notes"])
    po = get_draft_pos_for_ticket(ticket["ticket_id"])[0]
    assert po["status"] == "draft" and po["approval_status"] == "approved" and po["approval_by"] == "bob"
    acts = actions(ticket["ticket_id"])
    assert acts.count("approval_approved") == 1


@pytest.mark.parametrize("action,status,ticket_status,po_status", [
    ("reject", "rejected", "rejected", "rejected"),
    ("request_changes", "changes_requested", "changes_requested", None),
])
def test_reject_and_request_changes(action, status, ticket_status, po_status):
    ticket, doc = make_approval()
    after, _ = service.decide(doc["approval_id"], APPROVER, body(doc, action, comment="Please reduce headcount"))
    assert after["status"] == status
    t = get_ticket(ticket["ticket_id"])
    assert t["status"] == ticket_status
    assert t["notes"][-1]["text"] == "Please reduce headcount"
    po = get_draft_pos_for_ticket(ticket["ticket_id"])[0]
    assert po["status"] == "draft" and po.get("approval_status") == po_status
    assert f"approval_{status}" in actions(ticket["ticket_id"])


@pytest.mark.parametrize("action", ["reject", "request_changes"])
@pytest.mark.parametrize("comment", [None, "   "])
def test_comment_required(action, comment):
    _, doc = make_approval()
    with pytest.raises(ApprovalError) as ei:
        service.decide(doc["approval_id"], APPROVER, body(doc, action, comment=comment))
    assert ei.value.status_code == 422 and ei.value.detail == E.MSG_COMMENT_REQUIRED
    assert repo.get_approval(doc["approval_id"])["status"] == "pending"


@pytest.mark.parametrize("actor", [ADMIN, REQUESTER, OTHER_USER])
def test_non_approvers_cannot_decide(actor):
    ticket, doc = make_approval()
    with pytest.raises(ApprovalError) as ei:
        service.decide(doc["approval_id"], actor, body(doc))
    # Users who cannot even see it get 404; the requester and admin get 403.
    assert ei.value.status_code == (404 if actor is OTHER_USER else 403)
    assert repo.get_approval(doc["approval_id"])["status"] == "pending"
    assert get_ticket(ticket["ticket_id"])["status"] == "pending_approval"


def test_self_approval_forbidden_even_for_approver():
    approver_requester = UserPublic(username="dave", name="Dave", email="d@example.com", role="approver")
    ticket, doc = make_approval(owner="dave")
    with pytest.raises(ApprovalError) as ei:
        service.decide(doc["approval_id"], approver_requester, body(doc))
    assert ei.value.status_code == 403 and ei.value.detail == E.MSG_SELF_APPROVAL
    assert repo.get_approval(doc["approval_id"])["decisions"] == []
    assert "approval_denied" in actions(ticket["ticket_id"])


def test_not_found():
    with pytest.raises(ApprovalError) as ei:
        service.decide("APR-nope", APPROVER, DecisionIn(action="approve", idempotency_key="k" * 10,
                                                        request_version=1, workflow_snapshot_hash="ab" * 16))
    assert ei.value.status_code == 404


def test_stale_hash_and_version():
    _, doc = make_approval()
    for over in ({"workflow_snapshot_hash": "0" * 64}, {"request_version": 2}):
        with pytest.raises(ApprovalError) as ei:
            service.decide(doc["approval_id"], APPROVER, body(doc, **over))
        assert ei.value.status_code == 409 and ei.value.detail == E.MSG_STALE
    assert repo.get_approval(doc["approval_id"])["status"] == "pending"


def test_terminal_cannot_be_redecided():
    _, doc = make_approval()
    service.decide(doc["approval_id"], APPROVER, body(doc, "reject", comment="No"))
    with pytest.raises(ApprovalError) as ei:
        service.decide(doc["approval_id"], APPROVER2, body(doc))
    assert ei.value.status_code == 409 and ei.value.detail == E.MSG_TERMINAL
    after = repo.get_approval(doc["approval_id"])
    assert after["status"] == "rejected" and len(after["decisions"]) == 1


def test_idempotent_replay_and_conflicts():
    ticket, doc = make_approval()
    b = body(doc, comment="ok", key="key-12345678")
    first, r1 = service.decide(doc["approval_id"], APPROVER, b)
    again, r2 = service.decide(doc["approval_id"], APPROVER, b)
    assert (r1, r2) == (False, True)
    assert again["decisions"] == first["decisions"] and len(again["decisions"]) == 1
    assert actions(ticket["ticket_id"]).count("approval_approved") == 1
    with pytest.raises(ApprovalError) as ei:
        service.decide(doc["approval_id"], APPROVER, body(doc, comment="different", key="key-12345678"))
    assert ei.value.status_code == 409 and ei.value.detail == E.MSG_IDEMPOTENCY_CONFLICT
    with pytest.raises(ApprovalError) as ei:
        service.decide(doc["approval_id"], APPROVER2, body(doc, comment="ok", key="key-12345678"))
    assert ei.value.status_code == 409 and ei.value.detail == E.MSG_IDEMPOTENCY_CONFLICT
    with pytest.raises(ApprovalError) as ei:
        service.decide(doc["approval_id"], APPROVER, body(doc, comment="ok", key="another-key-1"))
    assert ei.value.status_code == 409 and ei.value.detail == E.MSG_TERMINAL
    assert len(repo.get_approval(doc["approval_id"])["decisions"]) == 1


# ---------- expiry ----------
def test_lazy_expiry_on_decide():
    created = datetime.now(timezone.utc) - timedelta(hours=73)
    ticket, doc = make_approval(now=created)
    with pytest.raises(ApprovalError) as ei:
        service.decide(doc["approval_id"], APPROVER, body(doc))
    assert ei.value.status_code == 409 and ei.value.detail == E.MSG_EXPIRED
    stored = repo.get_approval(doc["approval_id"])
    assert stored["status"] == "expired" and stored["decisions"] == []
    assert get_ticket(ticket["ticket_id"])["status"] == "approval_expired"
    assert actions(ticket["ticket_id"]).count("approval_expired") == 1
    # never auto-approved; repeated reads don't re-expire
    service.get_view(doc["approval_id"], APPROVER)
    assert actions(ticket["ticket_id"]).count("approval_expired") == 1


def test_expiry_boundary_with_injected_now():
    ticket, doc = make_approval()
    just_before = doc["expires_at"] - timedelta(seconds=1)
    view = service.get_view(doc["approval_id"], APPROVER, now=just_before)
    assert view.status == "pending" and view.can_decide
    view = service.get_view(doc["approval_id"], APPROVER, now=doc["expires_at"])
    assert view.status == "expired" and not view.can_decide


def test_rerun_after_expiry_creates_new_version():
    created = datetime.now(timezone.utc) - timedelta(hours=80)
    ticket, doc = make_approval(now=created)
    service.get_view(doc["approval_id"], APPROVER)
    wr = ticket["workflow_result"]
    v2 = service.on_workflow_completed({**ticket, "run_count": 2}, wr, evaluate_approval_requirement(wr))
    assert v2["request_version"] == 2
    assert [d["status"] for d in repo.list_for_ticket(ticket["ticket_id"])] == ["expired", "pending"]


# ---------- hard block ----------
def test_hard_blocked_snapshot_cannot_be_approved():
    ticket, doc = make_approval()
    get_database().approval_requests.update_one({"approval_id": doc["approval_id"]},
                                                {"$set": {"review_snapshot.final_status": "blocked"}})
    with pytest.raises(ApprovalError) as ei:
        service.decide(doc["approval_id"], APPROVER, body(doc))
    assert ei.value.status_code == 409 and ei.value.detail == E.MSG_BLOCKED
    assert repo.get_approval(doc["approval_id"])["status"] == "pending"


# ---------- reconciliation ----------
def test_secondary_failure_flags_reconciliation_and_reconcile_repairs(monkeypatch):
    from app.db.errors import PersistenceError
    ticket, doc = make_approval()

    def boom(*a, **k):
        raise PersistenceError()

    monkeypatch.setattr(service, "set_status", boom)
    monkeypatch.setattr(service, "set_po_approval", boom)
    after, replayed = service.decide(doc["approval_id"], APPROVER, body(doc, comment="ok"))
    assert after["status"] == "approved" and after["reconciliation_required"] is True and not replayed
    stored = repo.get_approval(doc["approval_id"])
    assert stored["status"] == "approved" and stored["reconciliation_required"] is True
    assert get_ticket(ticket["ticket_id"])["status"] == "pending_approval"
    assert "approval_reconciliation_required" in actions(ticket["ticket_id"])

    monkeypatch.undo()
    fixed = service.reconcile_approval(doc["approval_id"])
    assert fixed["reconciliation_required"] is False
    t = get_ticket(ticket["ticket_id"])
    assert t["status"] == "approved"
    assert sum(1 for n in t["notes"] if n.get("approval_id") == doc["approval_id"]) == 1
    assert actions(ticket["ticket_id"]).count("approval_approved") == 1
    po = get_draft_pos_for_ticket(ticket["ticket_id"])[0]
    assert po["status"] == "draft" and po["approval_status"] == "approved"
    # idempotent
    service.reconcile_approval(doc["approval_id"])
    assert actions(ticket["ticket_id"]).count("approval_approved") == 1


def test_audit_failure_after_commit_still_truthful(monkeypatch):
    from app.db.errors import PersistenceError
    ticket, doc = make_approval()
    real = service._append_audit_with_retry

    def flaky(ticket_id, agent, action, *a, **k):
        if action == "approval_approved":
            raise PersistenceError()
        return real(ticket_id, agent, action, *a, **k)

    monkeypatch.setattr(service, "_append_audit_with_retry", flaky)
    after, _ = service.decide(doc["approval_id"], APPROVER, body(doc))
    assert after["status"] == "approved" and after["reconciliation_required"] is True
    assert get_ticket(ticket["ticket_id"])["status"] == "approved"
    monkeypatch.undo()
    service.reconcile_approval(doc["approval_id"])
    assert actions(ticket["ticket_id"]).count("approval_approved") == 1
    assert repo.get_approval(doc["approval_id"])["reconciliation_required"] is False


def test_db_failure_before_commit_raises_persistence_error(monkeypatch):
    from app.db.errors import PersistenceError
    _, doc = make_approval()

    def down(*a, **k):
        raise PersistenceError()

    monkeypatch.setattr(service.repo, "decide_atomic", down)
    with pytest.raises(PersistenceError):
        service.decide(doc["approval_id"], APPROVER, body(doc))
    monkeypatch.undo()
    assert repo.get_approval(doc["approval_id"])["status"] == "pending"


def test_audit_retry_on_sequence_collision(monkeypatch):
    ticket, _ = make_approval()
    calls = {"n": 0}
    real = service.get_last_sequence

    def stale_seq(tid):
        calls["n"] += 1
        return real(tid) - 1 if calls["n"] == 1 else real(tid)

    monkeypatch.setattr(service, "get_last_sequence", stale_seq)
    service._append_audit_with_retry(ticket["ticket_id"], "System", "probe", "retry probe")
    assert calls["n"] >= 2 and "probe" in actions(ticket["ticket_id"])


# ---------- views ----------
def test_views_and_visibility():
    ticket, doc = make_approval()
    v = service.get_view(doc["approval_id"], APPROVER)
    assert v.can_decide and v.cannot_decide_reason is None
    assert "No order or payment is executed" in v.safety_notice
    v = service.get_view(doc["approval_id"], REQUESTER)
    assert not v.can_decide and v.cannot_decide_reason
    v = service.get_view(doc["approval_id"], ADMIN)
    assert not v.can_decide
    with pytest.raises(ApprovalError) as ei:
        service.get_view(doc["approval_id"], OTHER_USER)
    assert ei.value.status_code == 404

    assert [i.approval_id for i in service.list_view(APPROVER, "pending")] == [doc["approval_id"]]
    item = service.list_view(APPROVER)[0]
    assert item.can_decide and item.amount == 76125.0 and "attendees" in item.summary
    assert len(service.list_view(ADMIN)) == 1 and not service.list_view(ADMIN)[0].can_decide
    assert len(service.list_view(REQUESTER)) == 1
    assert service.list_view(OTHER_USER) == []
    assert service.approval_counts(APPROVER) == {"pending": 1}

    tv = service.ticket_approval_view(ticket["ticket_id"], REQUESTER)
    assert tv.current.approval_id == doc["approval_id"] and len(tv.history) == 1
    assert tv.requirement.required
    with pytest.raises(ApprovalError) as ei:
        service.ticket_approval_view(ticket["ticket_id"], OTHER_USER)
    assert ei.value.status_code == 404


def test_list_view_lazily_expires():
    created = datetime.now(timezone.utc) - timedelta(hours=100)
    ticket, doc = make_approval(now=created)
    assert service.list_view(APPROVER, "pending") == []
    assert repo.get_approval(doc["approval_id"])["status"] == "expired"


def test_ids_format():
    import re
    assert re.fullmatch(r"APR-\d{8}-[0-9a-f]{8}", service.new_approval_id())
    assert re.fullmatch(r"DEC-[0-9a-f]{12}", service.new_decision_id())
