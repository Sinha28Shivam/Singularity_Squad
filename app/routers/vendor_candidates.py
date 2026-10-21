"""Admin-only candidate registry read/repair endpoints (paginated list, detail, audit events, outbox repair)."""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import require_admin
from app.db.errors import PersistenceError
from app.portal_schemas import UserPublic
from app.vendor_sourcing import service
from app.vendor_sourcing.repository import ALLOWED_TRANSITIONS
from app.vendor_sourcing.schemas import MAX_PAGE_LIMIT, MAX_PAGE_OFFSET, CandidateView

router = APIRouter(prefix="/api/vendor-sourcing", tags=["vendor sourcing"])

_UNAVAILABLE = "Vendor registry temporarily unavailable."
_CANDIDATE_ID_RE = re.compile(r"[A-Z0-9-]{3,40}")


def allowed_decisions(status: Optional[str]) -> list[str]:
    """Decisions currently allowed for a candidate in ``status`` (from the repository transition table)."""
    return sorted(ALLOWED_TRANSITIONS.get(status or "", set()))


class CandidateResponse(CandidateView):
    """CandidateView plus the decisions the registry will accept next (UI hint; the server re-validates)."""
    allowed_decisions: list[str] = []


def candidate_response(candidate: dict) -> dict:
    view = CandidateView.model_validate(candidate).model_dump(mode="json")
    view["allowed_decisions"] = allowed_decisions(view.get("status"))
    return view


@router.get("/candidates")
def list_candidates(q: str = Query("", max_length=120),
                    service_name: Optional[str] = Query(None, alias="service", max_length=80),
                    status: Optional[str] = Query(None, pattern="^(pending_review|approved|rejected|suspended)$"),
                    limit: int = Query(50, ge=1, le=MAX_PAGE_LIMIT),
                    offset: int = Query(0, ge=0, le=MAX_PAGE_OFFSET),
                    _: UserPublic = Depends(require_admin)):
    """Paginated candidate registry: ``{items, total, limit, offset, next_offset}``."""
    try:
        page = service.list_candidates(query=q, status=status, service=service_name, limit=limit, offset=offset)
    except PersistenceError as exc:
        raise HTTPException(503, _UNAVAILABLE) from exc
    page["items"] = [candidate_response(c) for c in page["items"]]
    return page


@router.get("/candidates/transitions")
def candidate_transitions(_: UserPublic = Depends(require_admin)):
    """Candidate status -> allowed decisions, e.g. ``{"pending_review": ["approve", "reject", "suspend"]}``."""
    return {status: allowed_decisions(status) for status in ALLOWED_TRANSITIONS}


@router.post("/candidates/repair-events")
def repair_candidate_events(limit: int = Query(500, ge=1, le=5000), _: UserPublic = Depends(require_admin)):
    """Deliver queued candidate audit events (idempotent; safe to call repeatedly)."""
    try:
        return service.repair_pending_events(limit)
    except PersistenceError as exc:
        raise HTTPException(503, _UNAVAILABLE) from exc


@router.get("/candidates/{candidate_id}", response_model=CandidateResponse)
def get_candidate(candidate_id: str, _: UserPublic = Depends(require_admin)):
    _check_id(candidate_id)
    try:
        candidate = service.get_candidate(candidate_id)
    except PersistenceError as exc:
        raise HTTPException(503, _UNAVAILABLE) from exc
    if candidate is None:
        raise HTTPException(404, "Vendor candidate not found.")
    return candidate_response(candidate)


@router.get("/candidates/{candidate_id}/events")
def get_candidate_events(candidate_id: str, limit: int = Query(200, ge=1, le=1000),
                         _: UserPublic = Depends(require_admin)):
    _check_id(candidate_id)
    try:
        events = service.candidate_events(candidate_id, limit)
    except PersistenceError as exc:
        raise HTTPException(503, _UNAVAILABLE) from exc
    if events is None:
        raise HTTPException(404, "Vendor candidate not found.")
    return {"candidate_id": candidate_id, "events": events}


def _check_id(candidate_id: str) -> None:
    if not _CANDIDATE_ID_RE.fullmatch(candidate_id or ""):
        raise HTTPException(404, "Vendor candidate not found.")
