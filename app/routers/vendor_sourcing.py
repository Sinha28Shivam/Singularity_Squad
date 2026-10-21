"""Admin-only vendor discovery and candidate onboarding endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import require_admin
from app.db.errors import PersistenceError
from app.portal_schemas import UserPublic
from app.vendor_sourcing import service
from app.vendor_sourcing.schemas import CandidateDecisionIn, CandidateIn, CandidateView

router = APIRouter(prefix="/api/vendor-sourcing", tags=["vendor sourcing"])


@router.get("/vendors")
def search_vendors(q: str = Query("", max_length=120),
                   service_name: Optional[str] = Query(None, alias="service", max_length=80),
                   status: Optional[str] = Query(None, pattern="^(pending_review|approved|rejected|suspended)$"),
                   limit: int = Query(100, ge=1, le=200),
                   _: UserPublic = Depends(require_admin)):
    try:
        return service.search_vendors(q, service_name, status, limit)
    except PersistenceError as exc:
        raise HTTPException(503, "Vendor registry temporarily unavailable.") from exc


@router.post("/candidates", response_model=CandidateView, status_code=201)
def create_candidate(body: CandidateIn, admin: UserPublic = Depends(require_admin)):
    try:
        return service.submit_candidate(body, admin.username)
    except PersistenceError as exc:
        raise HTTPException(503, "Vendor registry temporarily unavailable.") from exc


@router.post("/candidates/{candidate_id}/decision", response_model=CandidateView)
def decide_candidate(candidate_id: str, body: CandidateDecisionIn,
                     admin: UserPublic = Depends(require_admin)):
    try:
        result = service.decide_candidate(candidate_id, body.decision, body.reason, admin.username)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(503, "Vendor registry temporarily unavailable.") from exc
    if result is None:
        raise HTTPException(404, "Vendor candidate not found.")
    return result
