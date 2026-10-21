"""Admin-only vendor discovery and candidate onboarding endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth.deps import require_admin
from app.db.errors import PersistenceError
from app.portal_schemas import UserPublic
from app.vendor_sourcing import service
from app.vendor_sourcing.schemas import CandidateDecisionIn, CandidateIn, CandidateView
from app.vendor_sourcing import rfq_service
from app.vendor_sourcing.rfq_schemas import RFQCreate, QuoteSubmission, QuoteVerification

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



@router.post("/rfqs", status_code=201)
def create_rfq(body: RFQCreate, admin: UserPublic = Depends(require_admin)):
    try:
        return rfq_service.create_rfq(body, admin.username)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(503, "RFQ service temporarily unavailable.") from exc


@router.get("/rfqs/{rfq_id}")
def get_rfq(rfq_id: str, admin: UserPublic = Depends(require_admin)):
    try:
        rfq = rfq_service.get_rfq(rfq_id)
        if rfq is None:
            raise HTTPException(404, "RFQ not found.")
        return {"rfq": rfq, "quotes": rfq_service.list_quotes(rfq_id),
                "notice": "RFQ is a draft sourcing record; no supplier has been contacted."}
    except PersistenceError as exc:
        raise HTTPException(503, "RFQ service temporarily unavailable.") from exc


@router.post("/rfqs/{rfq_id}/quotes", status_code=201)
def submit_quote(rfq_id: str, body: QuoteSubmission, admin: UserPublic = Depends(require_admin)):
    try:
        return rfq_service.submit_structured_quote(rfq_id, body, admin.username)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(503, "RFQ service temporarily unavailable.") from exc


@router.post("/rfqs/{rfq_id}/quotes/pdf", status_code=201)
async def submit_pdf_quote(rfq_id: str, request: Request,
                           vendor_id: str = Query(..., min_length=3, max_length=80),
                           quote_reference: str = Query(..., min_length=1, max_length=120),
                           filename: str = Query("quotation.pdf", max_length=200),
                           admin: UserPublic = Depends(require_admin)):
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/pdf":
        raise HTTPException(415, "Content-Type must be application/pdf.")
    try:
        raw = await request.body()
        return rfq_service.submit_pdf_quote(rfq_id, vendor_id, quote_reference, filename, raw, admin.username)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(503, "RFQ service temporarily unavailable.") from exc


@router.post("/rfqs/{rfq_id}/quotes/{quote_id}/verify")
def verify_quote(rfq_id: str, quote_id: str, body: QuoteVerification,
                 admin: UserPublic = Depends(require_admin)):
    try:
        return rfq_service.verify_quote(rfq_id, quote_id, body, admin.username)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(503, "RFQ service temporarily unavailable.") from exc


@router.post("/rfqs/{rfq_id}/compare")
def compare_quotes(rfq_id: str, admin: UserPublic = Depends(require_admin)):
    try:
        return rfq_service.compare_quotes(rfq_id, admin.username)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(503, "RFQ service temporarily unavailable.") from exc
