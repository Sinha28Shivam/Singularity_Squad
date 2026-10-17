"""Phase 2 approval routes.

Identity and role always come from the bearer session; the decision body cannot carry actor/role/status
(DecisionIn forbids extra fields). Role, self-approval, expiry, version/hash and idempotency rules are
enforced by app.approvals.service. Approving records internal authorization only: no order is placed,
no PO is sent and nothing is paid.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query

from app.approvals import service as approval_service
from app.approvals.schemas import APPROVAL_SAFETY_NOTICE, ApprovalStatus, ApprovalView, DecisionIn
from app.auth.deps import require_admin, require_approver_or_admin, require_user
from app.portal_schemas import UserPublic
from app.services import ticket_service as tsvc

APPROVAL_ID_PATTERN = r"^APR-[A-Za-z0-9-]{1,64}$"
TICKET_ID_PATTERN = r"^REQ-[A-Za-z0-9-]{1,64}$"

_MESSAGES = {
    "approved": "Approval recorded for internal authorization only. The PO remains a draft; no order or "
                "payment was executed.",
    "rejected": "Rejection recorded. The draft PO was not sent to anyone.",
    "changes_requested": "Change request recorded. The requester must update and resubmit the ticket.",
}

router = APIRouter(tags=["approvals"])


def _dump(model) -> dict:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else dict(model)


@router.get("/api/approvals")
def list_approvals(status: Optional[ApprovalStatus] = Query(None), limit: int = Query(100, ge=1, le=200),
                   viewer: UserPublic = Depends(require_approver_or_admin)):
    items = approval_service.list_view(viewer, status, limit)
    return {"approvals": [_dump(i) for i in items], "counts": approval_service.approval_counts(viewer)}


@router.get("/api/approvals/{approval_id}", response_model=ApprovalView)
def get_approval(approval_id: str = Path(..., pattern=APPROVAL_ID_PATTERN),
                 viewer: UserPublic = Depends(require_user)):
    return approval_service.get_view(approval_id, viewer)


@router.post("/api/approvals/{approval_id}/decision")
def decide(body: DecisionIn, approval_id: str = Path(..., pattern=APPROVAL_ID_PATTERN),
           actor: UserPublic = Depends(require_user)):
    doc, replayed = approval_service.decide(approval_id, actor, body)
    view = approval_service.get_view(approval_id, actor)
    data = _dump(view)
    status = data.get("status")
    reconciliation = bool(data.get("reconciliation_required") or (doc or {}).get("reconciliation_required"))
    message = _MESSAGES.get(status, "Decision recorded.")
    if replayed:
        message = "This decision was already recorded; returning the stored result. " + message
    if reconciliation:
        message += " Some follow-up records are pending reconciliation by an admin."
    return {"approval": data, "replayed": bool(replayed), "reconciliation_required": reconciliation,
            "message": message}


@router.get("/api/tickets/{ticket_id}/approval")
def ticket_approval(ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
                    viewer: UserPublic = Depends(require_user)):
    """Owner, approvers and admins only (others get 404). Only this sub-path is open to non-admins; the
    legacy GET /api/tickets/{ticket_id} stays admin-only."""
    data = _dump(approval_service.ticket_approval_view(ticket_id, viewer))
    if viewer.role == "user":
        # requesters get the same safe projection as in their ticket view (no snapshot/hash internals)
        safe = tsvc.requester_approval(data) or {"current": None, "history": [],
                                                 "safety_notice": APPROVAL_SAFETY_NOTICE}
        return {"ticket_id": ticket_id, "requirement": None, **safe}
    return data


@router.post("/api/admin/approvals/{approval_id}/reconcile", response_model=ApprovalView)
def reconcile(approval_id: str = Path(..., pattern=APPROVAL_ID_PATTERN),
              admin: UserPublic = Depends(require_admin)):
    approval_service.reconcile_approval(approval_id)
    return approval_service.get_view(approval_id, admin)
