"""/api/my/* routes: requesters submit, view, edit and comment on their own tickets."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query

from app.auth.deps import require_user
from app.portal_schemas import NoteIn, TicketStatus, TicketSubmission, UserPublic, UserTicketView
from app.services import ticket_service as svc

TICKET_ID_PATTERN = r"^REQ-[A-Za-z0-9-]{1,64}$"

router = APIRouter(prefix="/api/my", tags=["my tickets"])


@router.post("/tickets", response_model=UserTicketView, status_code=201)
def submit(body: TicketSubmission, user: UserPublic = Depends(require_user)) -> UserTicketView:
    return svc.to_user_view(svc.submit_ticket(user, body))


@router.get("/tickets")
def my_tickets(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=100_000),
               q: Optional[str] = Query(None, max_length=120), status: Optional[TicketStatus] = Query(None),
               user: UserPublic = Depends(require_user)):
    docs, total, next_offset = svc.search_user_tickets(user, q, status, limit, offset)
    return {"tickets": [svc.to_list_item(d, admin=False) for d in docs], "total": total,
            "next_offset": next_offset}


@router.get("/tickets/{ticket_id}", response_model=UserTicketView)
def my_ticket(ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
              user: UserPublic = Depends(require_user)) -> UserTicketView:
    return svc.to_user_view(svc.get_user_ticket(ticket_id, user))


@router.put("/tickets/{ticket_id}", response_model=UserTicketView)
def update_ticket(body: TicketSubmission, ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
                  user: UserPublic = Depends(require_user)) -> UserTicketView:
    return svc.to_user_view(svc.user_update_ticket(ticket_id, user, body))


@router.post("/tickets/{ticket_id}/notes", response_model=UserTicketView)
def add_note(body: NoteIn, ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
             user: UserPublic = Depends(require_user)) -> UserTicketView:
    return svc.to_user_view(svc.add_note(ticket_id, user, body.text, require_owner=True))
