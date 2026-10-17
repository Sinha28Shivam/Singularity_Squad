"""/api/admin/* routes: admins triage, run the workflow, close (reject) reviewed tickets and comment.
Approving is reserved for eligible approvers (/api/approvals)."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query

from app.auth.deps import require_admin
from app.portal_schemas import AdminTicketView, DecisionIn, NoteIn, RunIn, TicketStatus, UserPublic
from app.repositories import get_audit_entries
from app.services import ticket_service as svc

TICKET_ID_PATTERN = r"^REQ-[A-Za-z0-9-]{1,64}$"

router = APIRouter(prefix="/api/admin", tags=["admin tickets"])


@router.get("/tickets")
def all_tickets(status: Optional[TicketStatus] = Query(None), limit: int = Query(100, ge=1, le=200),
                _: UserPublic = Depends(require_admin)):
    docs, counts = svc.list_admin_tickets(status, limit)
    return {"tickets": [svc.to_list_item(d, admin=True) for d in docs], "counts": counts}


@router.get("/tickets/{ticket_id}", response_model=AdminTicketView)
def admin_ticket(ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
                 admin: UserPublic = Depends(require_admin)) -> AdminTicketView:
    return svc.to_admin_view(svc.get_admin_ticket(ticket_id), admin)


@router.post("/tickets/{ticket_id}/run", response_model=AdminTicketView)
def run(body: Optional[RunIn] = None, ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
        admin: UserPublic = Depends(require_admin)) -> AdminTicketView:
    body = body or RunIn()
    return svc.to_admin_view(svc.run_ticket(ticket_id, admin, body.invoice_id, body.today), admin)


@router.post("/tickets/{ticket_id}/decision", response_model=AdminTicketView)
def decide(body: DecisionIn, ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
           admin: UserPublic = Depends(require_admin)) -> AdminTicketView:
    return svc.to_admin_view(svc.decide_ticket(ticket_id, admin, body.decision, body.note), admin)


@router.post("/tickets/{ticket_id}/notes", response_model=AdminTicketView)
def add_note(body: NoteIn, ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
             admin: UserPublic = Depends(require_admin)) -> AdminTicketView:
    return svc.to_admin_view(svc.add_note(ticket_id, admin, body.text), admin)


@router.get("/tickets/{ticket_id}/audit")
def audit(ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN), limit: int = Query(500, ge=1, le=500),
          _: UserPublic = Depends(require_admin)):
    svc.get_admin_ticket(ticket_id)
    return {"ticket_id": ticket_id, "events": get_audit_entries(ticket_id, limit)}
