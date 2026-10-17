"""Policy RAG routes: search, ticket evaluation/evidence, rate cards and quote validation.

Identity and role always come from the bearer session. Users (requesters) cannot search the library and
only ever see a redacted view of their own ticket's evidence (finding summaries + citation labels).
Nothing here executes an order or payment.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel

from app.auth.deps import require_admin, require_approver_or_admin, require_user
from app.policy_rag import evaluation as policy_eval
from app.policy_rag import rate_cards, retrieval
from app.policy_rag.errors import PolicyRagError
from app.policy_rag.schemas import PolicySearchIn, PolicySearchResult, RateQuote, RateValidationResult
from app.portal_schemas import UserPublic
from app.repositories import evidence_snapshot_repository as snaps
from app.repositories import rate_card_repository
from app.repositories.ticket_repository import get_ticket

TICKET_ID_PATTERN = r"^REQ-[A-Za-z0-9-]{1,64}$"
RATE_ID_PATTERN = r"^rate_[a-f0-9]{8,40}$"
_NOT_FOUND = "Ticket not found."
_NO_EVIDENCE = "No policy evidence is stored for this ticket yet."

router = APIRouter(tags=["policy"])


class RateVerifyIn(BaseModel):
    model_config = {"extra": "forbid"}
    decision: Literal["verify", "reject"]


@router.post("/api/policy/search", response_model=PolicySearchResult)
def policy_search(body: PolicySearchIn, viewer: UserPublic = Depends(require_approver_or_admin)):
    return retrieval.search(body, viewer)


@router.post("/api/policy/validate-rate", response_model=RateValidationResult)
def validate_rate(body: RateQuote, viewer: UserPublic = Depends(require_approver_or_admin)):
    return rate_cards.validate_quote(body)


def _bound_hash(ticket_id: str) -> Optional[str]:
    from app.repositories import approval_repository
    pending = approval_repository.get_pending_for_ticket(ticket_id)
    if not pending:
        return None
    bound = (pending[-1].get("review_snapshot") or {}).get("policy_evaluation") or {}
    return bound.get("evaluation_hash")


@router.post("/api/requests/{ticket_id}/evaluate-policy")
def evaluate_policy(ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
                    admin: UserPublic = Depends(require_admin)):
    """Re-evaluate the current workflow_result without re-running the workflow. A snapshot is created only
    when none exists yet for the ticket's current run; pending approvals are never superseded here."""
    ticket = get_ticket(ticket_id)
    if ticket is None:
        raise PolicyRagError(404, _NOT_FOUND)
    if not isinstance(ticket.get("workflow_result"), dict) or not ticket.get("run_count"):
        raise PolicyRagError(409, "The workflow has not been run for this ticket yet.")
    result = policy_eval.evaluate_ticket(ticket, ticket["workflow_result"], admin)
    created = False
    if snaps.get_for_run(ticket_id, int(ticket.get("run_count") or 0)) is None:
        try:
            policy_eval.snapshot_evaluation(ticket, result)
            created = True
        except snaps.SnapshotConflictError:
            created = False
    bound = _bound_hash(ticket_id)
    return {"evaluation": result.model_dump(mode="json"), "snapshot_created": created,
            "stale_against_approval": bool(bound) and bound != result.evaluation_hash,
            "bound_evaluation_hash": bound}


@router.get("/api/requests/{ticket_id}/evidence")
def ticket_evidence(ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
                    viewer: UserPublic = Depends(require_user)):
    ticket = get_ticket(ticket_id)
    privileged = viewer.role in ("approver", "admin")
    if ticket is None or (not privileged and ticket.get("owner_username") != viewer.username):
        raise PolicyRagError(404, _NOT_FOUND)
    snap = snaps.get_latest(ticket_id)
    if snap is None:
        raise PolicyRagError(404, _NO_EVIDENCE)
    if privileged:
        return snap
    return policy_eval.redact(snap, ticket_id)


@router.get("/api/rate-cards")
def list_rate_cards(vendor_key: Optional[str] = Query(None, max_length=64),
                    item_or_service_code: Optional[str] = Query(None, max_length=64),
                    verification_status: Optional[Literal["needs_review", "verified", "rejected"]] = Query(None),
                    document_id: Optional[str] = Query(None, pattern=r"^doc_[a-f0-9]{8,32}$"),
                    limit: int = Query(500, ge=1, le=2000),
                    viewer: UserPublic = Depends(require_approver_or_admin)):
    rows = rate_card_repository.list_entries(vendor_key=vendor_key, item_or_service_code=item_or_service_code,
                                             verification_status=verification_status, document_id=document_id,
                                             limit=limit)
    return {"rate_cards": rows, "count": len(rows)}


@router.post("/api/rate-cards/{rate_id}/verify")
def verify_rate_card(body: RateVerifyIn, rate_id: str = Path(..., pattern=RATE_ID_PATTERN),
                     viewer: UserPublic = Depends(require_approver_or_admin)):
    return rate_cards.verify_entry(rate_id, viewer, approve=body.decision == "verify")
