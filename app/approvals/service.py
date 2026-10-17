"""Approval lifecycle: create versioned requests, enforce separation of duties, commit decisions
atomically and apply best-effort, idempotent secondary updates.

Approving records an internal human authorization only. This module never calls billing, procurement
or any external system: it never places an order, sends a PO or pays an invoice. The PO ``status``
always stays ``"draft"``; only its ``approval_status`` is recorded.

Multi-collection writes are not atomic. The single-document conditional update in
``approval_repository.decide_atomic`` is the authoritative commit; ticket / PO / note / audit updates
follow and, if any fails, the approval is flagged ``reconciliation_required`` and can be repaired
with ``reconcile_approval``.
"""
from __future__ import annotations

import json
import logging
import random
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from app.approvals import errors as E
from app.approvals.errors import ApprovalError
from app.approvals.policy import evaluate_approval_requirement, review_snapshot, snapshot_hash
from app.approvals.schemas import (
    ACTION_TO_STATUS,
    TERMINAL_APPROVAL_STATUSES,
    ApprovalListItem,
    ApprovalRequirement,
    ApprovalView,
    DecisionIn,
    DecisionRecord,
    TicketApprovalView,
)
from app.db.errors import AuditConflictError
from app.portal_schemas import EVENT_CATEGORY_LABELS, UserPublic
from app.repositories import approval_repository as repo
from app.repositories.audit_repository import append_audit_entries, get_audit_entries, get_last_sequence
from app.repositories.purchase_order_repository import set_po_approval
from app.repositories.ticket_repository import get_ticket, push_note, set_status
from app.schemas import AuditEntry

logger = logging.getLogger(__name__)

POLICY_PATH = Path(__file__).resolve().parents[2] / "data" / "approval_policy.json"
DECIDED_STATUSES = {"approved", "rejected", "changes_requested"}
TICKET_STATUS_FOR = {"approved": "approved", "rejected": "rejected",
                     "changes_requested": "changes_requested", "expired": "approval_expired"}
EVENT_FOR = {"approved": "approval_approved", "rejected": "approval_rejected",
             "changes_requested": "approval_changes_requested"}
PRIVILEGED_VIEWER_ROLES = {"approver", "admin"}
_AUDIT_ATTEMPTS = 6
_DENIAL_AUDIT_ATTEMPTS = 2


# ---------- config & ids ----------
@lru_cache(maxsize=1)
def _load_policy_cached() -> str:
    return POLICY_PATH.read_text(encoding="utf-8")


def load_policy() -> dict:
    """Approval policy config (cached). Returns a fresh copy so callers cannot mutate the cache."""
    return json.loads(_load_policy_cached())


def new_approval_id() -> str:
    return f"APR-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(4)}"


def new_decision_id() -> str:
    return f"DEC-{secrets.token_hex(6)}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return _now()
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


# ---------- audit ----------
def _append_audit_with_retry(ticket_id: str, agent: str, action: str, detail: str, status: str = "info",
                             attempts: int = _AUDIT_ATTEMPTS) -> None:
    """Append one audit event at last_sequence+1, retrying when a concurrent writer took the sequence."""
    for attempt in range(attempts):
        seq = get_last_sequence(ticket_id) + 1
        try:
            append_audit_entries(ticket_id, [AuditEntry(step=seq, agent=agent, action=action, status=status,
                                                        detail=detail)])
            return
        except AuditConflictError:
            if attempt == attempts - 1:
                raise
            time.sleep(random.uniform(0.001, 0.01) * (attempt + 1))


def _audit_denied(doc: Optional[dict], actor: UserPublic, category: str) -> None:
    """Best effort: never turns a denial into anything else, never raises."""
    if not doc:
        return
    try:
        _append_audit_with_retry(
            doc["ticket_id"], "Approval", "approval_denied",
            f"Decision attempt on {doc['approval_id']} v{doc.get('request_version')} by {actor.username} "
            f"({actor.role}) denied: {category}.", status="warn", attempts=_DENIAL_AUDIT_ATTEMPTS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not audit denied approval attempt %s (%s)", doc.get("approval_id"),
                       type(exc).__name__)


def _audit_exists(ticket_id: str, action: str, marker: str) -> bool:
    return any(e.get("action") == action and marker in (e.get("detail") or "")
               for e in get_audit_entries(ticket_id))


# ---------- creation / supersede / expiry ----------
def supersede_pending(ticket_id: str, reason: str, now: Optional[datetime] = None) -> int:
    """Mark every pending request of the ticket ``superseded``. Returns how many were superseded."""
    now = _utc(now)
    count = 0
    for doc in repo.get_pending_for_ticket(ticket_id):
        if repo.transition_if_pending(doc["approval_id"], "superseded", now, {"superseded_reason": reason[:200]}):
            count += 1
            try:
                _append_audit_with_retry(ticket_id, "System", "approval_superseded",
                                         f"Approval {doc['approval_id']} v{doc['request_version']} superseded: "
                                         f"{reason[:200]}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Audit failed for superseded approval %s (%s)", doc["approval_id"],
                               type(exc).__name__)
                _flag_reconciliation(doc["approval_id"], ticket_id)
    return count


def on_workflow_completed(ticket: dict, workflow_result: dict, requirement: ApprovalRequirement,
                          now: Optional[datetime] = None) -> Optional[dict]:
    """Supersede any pending request, then create the next pending version if approval is required."""
    now = _utc(now)
    ticket_id = ticket["ticket_id"]
    supersede_pending(ticket_id, "A new workflow run replaced this version.", now)
    if not requirement.required or requirement.blocked or requirement.needs_clarification:
        return None

    policy = load_policy()
    snapshot = review_snapshot(workflow_result, ticket)
    digest = snapshot_hash(snapshot)
    doc = None
    for attempt in range(3):
        doc = {
            "approval_id": new_approval_id(),
            "ticket_id": ticket_id,
            "request_version": repo.next_version(ticket_id),
            "ticket_version": int(ticket.get("run_count") or 0),
            "status": "pending",
            "requester_id": ticket.get("owner_username") or "",
            "required_role": requirement.required_role,
            "assigned_approver_id": None,
            "reason_codes": list(requirement.reason_codes),
            "reasons": list(requirement.reasons),
            "risk_level": requirement.risk_level,
            "amount": requirement.amount,
            "currency": requirement.currency,
            "policy_version": requirement.policy_version,
            "workflow_snapshot_hash": digest,
            "review_snapshot": snapshot,
            "created_at": now,
            "expires_at": now + timedelta(hours=float(policy.get("expiry_hours", 72))),
            "decided_at": None,
            "decisions": [],
            "reconciliation_required": False,
        }
        try:
            doc = repo.create_approval(doc)
            break
        except repo.ApprovalConflictError:
            if attempt == 2:
                raise
            supersede_pending(ticket_id, "A new workflow run replaced this version.", now)
    try:
        _append_audit_with_retry(
            ticket_id, "Approval", "approval_requested",
            f"Approval {doc['approval_id']} v{doc['request_version']} requested for role "
            f"{doc['required_role']}; reasons {','.join(doc['reason_codes'])}; policy {doc['policy_version']}; "
            f"snapshot {digest[:12]}. Approval records internal authorization only.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audit failed for approval request %s (%s)", doc["approval_id"], type(exc).__name__)
        _flag_reconciliation(doc["approval_id"], ticket_id)
        doc["reconciliation_required"] = True
    return doc


def _flag_reconciliation(approval_id: str, ticket_id: str) -> None:
    try:
        repo.set_reconciliation(approval_id, True)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not flag reconciliation for approval %s ticket %s (%s)", approval_id, ticket_id,
                     type(exc).__name__)
        return
    try:
        _append_audit_with_retry(ticket_id, "System", "approval_reconciliation_required",
                                 f"Approval {approval_id}: a secondary update failed; reconciliation required.",
                                 status="warn", attempts=_DENIAL_AUDIT_ATTEMPTS)
    except Exception:  # noqa: BLE001
        pass


def _apply_expiry_secondary(doc: dict, check_audit: bool) -> bool:
    ok = True
    try:
        if not set_status(doc["ticket_id"], "approval_expired", expected={"pending_approval"}):
            current = get_ticket(doc["ticket_id"]) or {}
            ok = ok and _ticket_moved_on(doc, current, "approval_expired")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ticket update failed for expired approval %s (%s)", doc["approval_id"], type(exc).__name__)
        ok = False
    try:
        marker = f"Approval {doc['approval_id']} v{doc['request_version']} expired"
        if not (check_audit and _audit_exists(doc["ticket_id"], "approval_expired", marker)):
            _append_audit_with_retry(doc["ticket_id"], "System", "approval_expired",
                                     f"{marker} without a decision; it cannot be approved.", status="warn")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audit failed for expired approval %s (%s)", doc["approval_id"], type(exc).__name__)
        ok = False
    return ok


def expire_if_due(doc: dict, now: Optional[datetime] = None) -> dict:
    """Lazy server-side expiry. Never auto-approves. Returns the (possibly reloaded) document."""
    now = _utc(now)
    if doc.get("status") != "pending" or now < _utc(doc["expires_at"]):
        return doc
    if repo.transition_if_pending(doc["approval_id"], "expired", now):
        if not _apply_expiry_secondary(doc, check_audit=False):
            _flag_reconciliation(doc["approval_id"], doc["ticket_id"])
    return repo.get_approval(doc["approval_id"]) or doc


# ---------- decisions ----------
def _same_payload(dec: dict, actor: UserPublic, body: DecisionIn) -> bool:
    return (dec.get("actor_id") == actor.username and dec.get("action") == body.action
            and (dec.get("comment") or None) == body.comment
            and int(dec.get("request_version") or 0) == body.request_version
            and dec.get("workflow_snapshot_hash") == body.workflow_snapshot_hash)


def _check_idempotency(approval_id: str, actor: UserPublic, body: DecisionIn) -> Optional[dict]:
    """Returns the current doc for a replay, None if the key is unused; raises 409 on payload mismatch."""
    existing = repo.find_decision_by_key(approval_id, body.idempotency_key)
    if existing is None:
        return None
    if not _same_payload(existing, actor, body):
        raise ApprovalError(409, E.MSG_IDEMPOTENCY_CONFLICT)
    return repo.get_approval(approval_id)


def _conflict_for(doc: dict, actor: UserPublic, body: DecisionIn, now: datetime) -> ApprovalError:
    status = doc.get("status")
    if status == "expired":
        _audit_denied(doc, actor, "expired")
        return ApprovalError(409, E.MSG_EXPIRED)
    if status in TERMINAL_APPROVAL_STATUSES:
        _audit_denied(doc, actor, "terminal")
        return ApprovalError(409, E.MSG_TERMINAL)
    if (int(doc.get("request_version") or 0) != body.request_version
            or doc.get("workflow_snapshot_hash") != body.workflow_snapshot_hash):
        _audit_denied(doc, actor, "stale")
        return ApprovalError(409, E.MSG_STALE)
    _audit_denied(doc, actor, "terminal")
    return ApprovalError(409, E.MSG_TERMINAL)


def decide(approval_id: str, actor: UserPublic, body: DecisionIn,
           now: Optional[datetime] = None) -> tuple[dict, bool]:
    """Record a human decision. Returns (approval_doc_after, replayed).

    Raises ApprovalError (403/404/409/422) or PersistenceError (503). Identity and role come only from
    ``actor`` (the authenticated session), never from the body.
    """
    now = _utc(now)
    doc = repo.get_approval(approval_id)
    if doc is None:
        raise ApprovalError(404, E.MSG_NOT_FOUND)
    if actor.role not in PRIVILEGED_VIEWER_ROLES and actor.username != doc.get("requester_id"):
        raise ApprovalError(404, E.MSG_NOT_FOUND)  # do not reveal other users' approvals
    doc = expire_if_due(doc, now)

    if actor.role != doc.get("required_role"):
        _audit_denied(doc, actor, "wrong_role")
        raise ApprovalError(403, E.MSG_WRONG_ROLE)
    if actor.username == doc.get("requester_id"):
        _audit_denied(doc, actor, "self_approval")
        raise ApprovalError(403, E.MSG_SELF_APPROVAL)
    policy = load_policy()
    if body.action in set(policy.get("comment_required_for") or []) and not body.comment:
        raise ApprovalError(422, E.MSG_COMMENT_REQUIRED)

    replay = _check_idempotency(approval_id, actor, body)
    if replay is not None:
        return replay, True

    if doc.get("status") != "pending":
        raise _conflict_for(doc, actor, body, now)
    if (int(doc.get("request_version") or 0) != body.request_version
            or doc.get("workflow_snapshot_hash") != body.workflow_snapshot_hash):
        raise _conflict_for(doc, actor, body, now)
    snap_status = (doc.get("review_snapshot") or {}).get("final_status")
    if body.action == "approve" and snap_status not in ("pass", "warning"):
        _audit_denied(doc, actor, "blocked")
        raise ApprovalError(409, E.MSG_BLOCKED)

    new_status = ACTION_TO_STATUS[body.action]
    record = DecisionRecord(
        decision_id=new_decision_id(), action=body.action, actor_id=actor.username, actor_role=actor.role,
        comment=body.comment, decided_at=now, idempotency_key=body.idempotency_key,
        request_version=body.request_version, workflow_snapshot_hash=body.workflow_snapshot_hash,
    ).model_dump()
    after = repo.decide_atomic(approval_id, body.request_version, body.workflow_snapshot_hash, record,
                               new_status, now)
    if after is None:
        replay = _check_idempotency(approval_id, actor, body)  # a duplicate click may have won the race
        if replay is not None:
            return replay, True
        current = repo.get_approval(approval_id)
        if current is None:
            raise ApprovalError(404, E.MSG_NOT_FOUND)
        current = expire_if_due(current, now)
        raise _conflict_for(current, actor, body, now)

    if not _apply_decision_secondary(after, record, check_existing=False):
        _flag_reconciliation(approval_id, after["ticket_id"])
        after["reconciliation_required"] = True
    return after, False


def _ticket_moved_on(doc: dict, ticket: dict, target: str) -> bool:
    """True when the ticket is already in the target state (idempotent re-apply) or a newer approval
    version owns it (this version must not drive the ticket any more)."""
    if ticket.get("status") == target:
        return True
    current = repo.get_current_for_ticket(doc["ticket_id"])
    return bool(current and current.get("approval_id") != doc["approval_id"])


def _apply_decision_secondary(doc: dict, record: dict, check_existing: bool) -> bool:
    """Ticket status, PO approval_status, ticket note and audit. Idempotent; returns False on any failure."""
    ok = True
    ticket_id, approval_id = doc["ticket_id"], doc["approval_id"]
    status = doc["status"]
    target = TICKET_STATUS_FOR[status]
    actor_id = record.get("actor_id")
    decided_at = _utc(record.get("decided_at"))
    decision_id = record.get("decision_id")

    try:
        if not set_status(ticket_id, target, expected={"pending_approval"}):
            ok = _ticket_moved_on(doc, get_ticket(ticket_id) or {}, target) and ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("Ticket update failed for approval %s ticket %s (%s)", approval_id, ticket_id,
                       type(exc).__name__)
        ok = False

    po = (doc.get("review_snapshot") or {}).get("po")
    if po and status in ("approved", "rejected"):
        try:
            if not set_po_approval(po["po_id"], status, actor_id, decided_at):
                logger.warning("Draft PO missing for approval %s ticket %s", approval_id, ticket_id)
                ok = False
        except Exception as exc:  # noqa: BLE001
            logger.warning("PO update failed for approval %s (%s)", approval_id, type(exc).__name__)
            ok = False

    if record.get("comment"):
        try:
            ticket = get_ticket(ticket_id) if check_existing else None
            exists = bool(ticket) and any(n.get("decision_id") == decision_id for n in ticket.get("notes") or [])
            if not exists:
                push_note(ticket_id, {"author": actor_id, "role": record.get("actor_role"),
                                      "text": record["comment"], "created_at": decided_at,
                                      "approval_id": approval_id, "decision_id": decision_id})
        except Exception as exc:  # noqa: BLE001
            logger.warning("Note write failed for approval %s (%s)", approval_id, type(exc).__name__)
            ok = False

    event = EVENT_FOR[status]
    try:
        if not (check_existing and _audit_exists(ticket_id, event, decision_id)):
            comment = f" Comment: {record['comment'][:500]}" if record.get("comment") else ""
            extra = " Draft PO remains a draft; no order or payment is executed." if status == "approved" else ""
            _append_audit_with_retry(
                ticket_id, "Approver", event,
                f"Approval {approval_id} v{doc['request_version']} {status.replace('_', ' ')} by {actor_id} "
                f"({record.get('actor_role')}); decision {decision_id}; snapshot "
                f"{(record.get('workflow_snapshot_hash') or '')[:12]}.{comment}{extra}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audit failed for approval %s (%s)", approval_id, type(exc).__name__)
        ok = False
    return ok


def reconcile_approval(approval_id: str, now: Optional[datetime] = None) -> dict:
    """Re-apply secondary updates idempotently; clears the flag when everything succeeded."""
    doc = repo.get_approval(approval_id)
    if doc is None:
        raise ApprovalError(404, E.MSG_NOT_FOUND)
    doc = expire_if_due(doc, now)
    status = doc.get("status")
    ok = True
    if status in DECIDED_STATUSES and doc.get("decisions"):
        ok = _apply_decision_secondary(doc, doc["decisions"][-1], check_existing=True)
    elif status == "expired":
        ok = _apply_expiry_secondary(doc, check_audit=True)
    if ok and doc.get("reconciliation_required"):
        repo.set_reconciliation(approval_id, False)
    return repo.get_approval(approval_id) or doc


# ---------- views ----------
def _visible(doc: dict, viewer: UserPublic) -> bool:
    return viewer.role in PRIVILEGED_VIEWER_ROLES or viewer.username == doc.get("requester_id")


def can_decide(doc: dict, viewer: UserPublic, now: Optional[datetime] = None) -> tuple[bool, Optional[str]]:
    now = _utc(now)
    if doc.get("status") != "pending":
        return False, "This approval request is no longer pending."
    if now >= _utc(doc["expires_at"]):
        return False, E.MSG_EXPIRED
    if viewer.role != doc.get("required_role"):
        return False, E.MSG_WRONG_ROLE
    if viewer.username == doc.get("requester_id"):
        return False, E.MSG_SELF_APPROVAL
    return True, None


def _to_view(doc: dict, viewer: UserPublic, now: Optional[datetime] = None) -> ApprovalView:
    ok, reason = can_decide(doc, viewer, now)
    return ApprovalView.model_validate({**doc, "can_decide": ok, "cannot_decide_reason": reason})


def get_view(approval_id: str, viewer: UserPublic, now: Optional[datetime] = None) -> ApprovalView:
    doc = repo.get_approval(approval_id)
    if doc is None or not _visible(doc, viewer):
        raise ApprovalError(404, E.MSG_NOT_FOUND)
    return _to_view(expire_if_due(doc, now), viewer, now)


def _summary(doc: dict) -> str:
    snap = doc.get("review_snapshot") or {}
    sub = snap.get("submission") or {}
    req = snap.get("request") or {}
    label = EVENT_CATEGORY_LABELS.get(sub.get("event_category") or "") or \
        str(req.get("service") or "Event request").replace("_", " ").capitalize()
    parts = [label]
    attendees = sub.get("attendees") or req.get("attendees")
    if attendees:
        parts.append(f"{attendees} attendees")
    when = sub.get("event_date") or req.get("date")
    if when:
        parts.append(str(when))
    return " · ".join(parts)


def list_view(viewer: UserPublic, status: Optional[str] = None, limit: int = 100,
              now: Optional[datetime] = None) -> list[ApprovalListItem]:
    """Approvers/admins see all requests; other users only their own."""
    requester = None if viewer.role in PRIVILEGED_VIEWER_ROLES else viewer.username
    docs = [expire_if_due(d, now) for d in repo.list_approvals(status, limit, requester_id=requester)]
    if status:
        docs = [d for d in docs if d.get("status") == status]
    items = []
    for d in docs:
        ok, _ = can_decide(d, viewer, now)
        items.append(ApprovalListItem(
            approval_id=d["approval_id"], ticket_id=d["ticket_id"], request_version=d["request_version"],
            status=d["status"], requester_id=d.get("requester_id") or "", summary=_summary(d),
            amount=d.get("amount"), currency=d.get("currency") or "INR", risk_level=d.get("risk_level") or "low",
            reason_codes=list(d.get("reason_codes") or []), created_at=d["created_at"],
            expires_at=d["expires_at"], can_decide=ok))
    return items


def approval_counts(viewer: UserPublic) -> dict[str, int]:
    requester = None if viewer.role in PRIVILEGED_VIEWER_ROLES else viewer.username
    return repo.count_approvals_by_status(requester_id=requester)


def _policy_evaluation_for(ticket: dict) -> Optional[dict]:
    """Evidence snapshot of the ticket's current run, so the displayed requirement matches the one the
    approval request was created from. Missing snapshot (RAG disabled / legacy run) -> None."""
    run_count = int(ticket.get("run_count") or 0)
    if not run_count:
        return None
    from app.repositories import evidence_snapshot_repository
    snap = evidence_snapshot_repository.get_for_run(ticket["ticket_id"], run_count)
    return snap.get("evaluation") if snap and isinstance(snap.get("evaluation"), dict) else None


def ticket_approval_view(ticket_id: str, viewer: UserPublic, now: Optional[datetime] = None,
                         ticket: Optional[dict] = None) -> TicketApprovalView:
    """Current request + version history. Visible to the ticket owner, approvers and admins (else 404)."""
    ticket = ticket if ticket is not None else get_ticket(ticket_id)
    if ticket is None or (viewer.role not in PRIVILEGED_VIEWER_ROLES
                          and ticket.get("owner_username") != viewer.username):
        raise ApprovalError(404, E.MSG_TICKET_NOT_FOUND)
    history = [_to_view(expire_if_due(d, now), viewer, now) for d in repo.list_for_ticket(ticket_id)]
    requirement = None
    if ticket.get("workflow_result"):
        try:
            requirement = evaluate_approval_requirement(ticket["workflow_result"],
                                                        policy_evaluation=_policy_evaluation_for(ticket))
        except Exception as exc:  # noqa: BLE001 - view only; never treated as "approval not needed"
            logger.warning("Approval policy evaluation failed for ticket %s (%s)", ticket_id, type(exc).__name__)
    return TicketApprovalView(ticket_id=ticket_id, requirement=requirement,
                              current=history[-1] if history else None, history=history)
