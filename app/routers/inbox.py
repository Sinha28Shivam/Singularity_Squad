"""GET /api/inbox — the signed-in user's pending actions (role-specific, read-only)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.deps import current_user
from app.portal_schemas import UserPublic
from app.services.inbox_service import inbox_for

router = APIRouter(prefix="/api", tags=["inbox"])


@router.get("/inbox")
def inbox(user: UserPublic = Depends(current_user)) -> dict:
    return inbox_for(user)
