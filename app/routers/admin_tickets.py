"""/api/admin/* routes: admins triage, run the workflow, close (reject) reviewed tickets and comment.
Approving is reserved for eligible approvers (/api/approvals)."""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from app.auth.deps import require_admin
from app.portal_schemas import AdminTicketView, DecisionIn, NoteIn, RunIn, TicketStatus, UserPublic
from app.repositories import get_audit_entries
from app.services import ticket_service as svc

TICKET_ID_PATTERN = r"^REQ-[A-Za-z0-9-]{1,64}$"

router = APIRouter(prefix="/api/admin", tags=["admin tickets"])


@router.get("/tickets")
def all_tickets(status: Optional[TicketStatus] = Query(None), limit: int = Query(100, ge=1, le=200),
                offset: int = Query(0, ge=0, le=100_000), q: Optional[str] = Query(None, max_length=120),
                _: UserPublic = Depends(require_admin)):
    docs, total, next_offset, counts = svc.search_admin_tickets(q, status, limit, offset)
    return {"tickets": [svc.to_list_item(d, admin=True) for d in docs], "counts": counts,
            "total": total, "next_offset": next_offset}


@router.get("/tickets/{ticket_id}", response_model=AdminTicketView)
def admin_ticket(ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
                 admin: UserPublic = Depends(require_admin)) -> AdminTicketView:
    return svc.to_admin_view(svc.get_admin_ticket(ticket_id), admin)


@router.post("/tickets/{ticket_id}/run", response_model=AdminTicketView)
def run(body: Optional[RunIn] = None, ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
        admin: UserPublic = Depends(require_admin)) -> AdminTicketView:
    body = body or RunIn()
    return svc.to_admin_view(svc.run_ticket(ticket_id, admin, body.invoice_id, body.today), admin)


class InvoiceMatchIn(BaseModel):
    model_config = {"extra": "forbid"}
    invoice_id: str = Field(pattern=r"^INV-[A-Za-z0-9-]{1,32}$")


@router.post("/tickets/{ticket_id}/invoice-match", response_model=AdminTicketView)
def match_invoice(body: InvoiceMatchIn, ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
                  admin: UserPublic = Depends(require_admin)) -> AdminTicketView:
    """Match an invoice that arrived after approval against the approved draft PO (no re-approval, no payment)."""
    return svc.to_admin_view(svc.match_invoice_after_approval(ticket_id, admin, body.invoice_id), admin)


class InvoiceExceptionIn(BaseModel):
    model_config = {"extra": "forbid"}
    action: Literal["hold", "dispute", "vendor_response", "release", "reject"]
    note: str = Field(min_length=5, max_length=1000)


@router.post("/tickets/{ticket_id}/invoice-exception", response_model=AdminTicketView)
def invoice_exception(body: InvoiceExceptionIn, ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
                      admin: UserPublic = Depends(require_admin)) -> AdminTicketView:
    """Hold / dispute / record vendor response / release / reject the post-approval invoice.
    Record-keeping only: nothing is sent to the vendor and nothing is paid."""
    return svc.to_admin_view(svc.invoice_exception_action(ticket_id, admin, body.action, body.note), admin)


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
