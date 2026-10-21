"""Admin-only vendor discovery, candidate onboarding, RFQ, quotation and comparison endpoints.

Advisory sourcing only: no supplier is contacted, nothing is awarded and no purchase order is dispatched.
Error contract: every error body is ``{"detail": ...}``; 404 not found, 409 state conflict, 413 PDF too large,
415 wrong content type, 422 validation, 503 persistence unavailable. A committed write whose audit event could
not be delivered still succeeds and carries ``audit_pending: true`` (it is repaired on read / repair-events).
Optional ``X-Correlation-ID`` request header (``[A-Za-z0-9_.:-]{1,64}``) is recorded on audit events.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response

from app.auth.deps import require_admin
from app.db.errors import PersistenceError
from app.portal_schemas import UserPublic
from app.vendor_sourcing import metrics, rfq_service, service
from app.vendor_sourcing.rfq_schemas import (ComparisonDecision, QuoteRevision, QuoteSubmission,
                                             QuoteVerification, RFQCreate, RFQStateChange)
from app.routers.vendor_candidates import CandidateResponse, candidate_response
from app.vendor_sourcing.schemas import CandidateDecisionIn, CandidateIn

router = APIRouter(prefix="/api/vendor-sourcing", tags=["vendor sourcing"])

_REGISTRY_UNAVAILABLE = "Vendor registry temporarily unavailable."
_RFQ_UNAVAILABLE = "RFQ service temporarily unavailable."
_ID_RE = re.compile(r"[A-Z][A-Z0-9-]{2,63}")
_CORR_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def _corr(value: Optional[str]) -> Optional[str]:
    return value if value and _CORR_RE.fullmatch(value) else None


def _check_id(value: str, what: str) -> None:
    if not _ID_RE.fullmatch(value or ""):
        raise HTTPException(404, f"{what} not found.")


def _rfq_call(fn, *args, **kwargs):
    """Uniform RFQ error mapping (plan P1.15)."""
    try:
        return fn(*args, **kwargs)
    except HTTPException:
        raise
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except rfq_service.RFQConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(503, _RFQ_UNAVAILABLE) from exc


# ------------------------------------------------------------------------------------- discovery / candidates

@router.get("/vendors")
def search_vendors(q: str = Query("", max_length=120),
                   service_name: Optional[str] = Query(None, alias="service", max_length=80),
                   status: Optional[str] = Query(None, pattern="^(pending_review|approved|rejected|suspended)$"),
                   limit: int = Query(100, ge=1, le=200),
                   offset: int = Query(0, ge=0, le=10_000),
                   _: UserPublic = Depends(require_admin)):
    try:
        return service.search_vendors(q, service_name, status, limit, offset=offset)
    except PersistenceError as exc:
        raise HTTPException(503, _REGISTRY_UNAVAILABLE) from exc


@router.post("/candidates", response_model=CandidateResponse, status_code=201)
def create_candidate(body: CandidateIn, admin: UserPublic = Depends(require_admin),
                     x_correlation_id: Optional[str] = Header(None)):
    try:
        return candidate_response(service.submit_candidate(body, admin.username,
                                                           correlation_id=_corr(x_correlation_id)))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(503, _REGISTRY_UNAVAILABLE) from exc


@router.post("/candidates/{candidate_id}/decision", response_model=CandidateResponse)
def decide_candidate(candidate_id: str, body: CandidateDecisionIn,
                     admin: UserPublic = Depends(require_admin),
                     x_correlation_id: Optional[str] = Header(None)):
    try:
        result = service.decide_candidate(candidate_id, body.decision, body.reason, admin.username,
                                          correlation_id=_corr(x_correlation_id))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except PersistenceError as exc:
        raise HTTPException(503, _REGISTRY_UNAVAILABLE) from exc
    if result is None:
        raise HTTPException(404, "Vendor candidate not found.")
    return candidate_response(result)


@router.get("/metrics")
def get_metrics(_: UserPublic = Depends(require_admin)):
    """In-process counters (reset on restart): candidates, decisions, RFQs, quotes, verifications,
    ineligible reasons, comparisons and persistence failures."""
    return {"counters": metrics.snapshot(), "scope": "process", "note": "Counters reset when the API restarts."}


# ------------------------------------------------------------------------------------- RFQs

@router.post("/rfqs", status_code=201)
def create_rfq(body: RFQCreate, admin: UserPublic = Depends(require_admin),
               x_correlation_id: Optional[str] = Header(None)):
    """Create an ``open`` RFQ from a persisted pass/warning ticket. 201; 422 invalid/mismatched; 503."""
    return _rfq_call(rfq_service.create_rfq, body, admin.username, x_correlation_id)


@router.get("/rfqs")
def list_rfqs(status: Optional[str] = Query(None, pattern="^(open|closed|cancelled)$"),
              request_id: Optional[str] = Query(None, max_length=80),
              limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0, le=10_000),
              _: UserPublic = Depends(require_admin)):
    """Paginated RFQs ``{items, total, limit, offset, next_offset}``, newest first."""
    return _rfq_call(rfq_service.list_rfqs, status=status, request_id=request_id, limit=limit, offset=offset)


@router.post("/rfqs/repair-events")
def repair_rfq_events(_: UserPublic = Depends(require_admin)):
    """Deliver queued RFQ/quote/comparison audit events and heal interrupted revisions (idempotent)."""
    return _rfq_call(rfq_service.repair_pending_events)


@router.get("/rfqs/{rfq_id}")
def get_rfq(rfq_id: str, _: UserPublic = Depends(require_admin)):
    _check_id(rfq_id, "RFQ")

    def _load():
        rfq = rfq_service.get_rfq(rfq_id)
        if rfq is None:
            raise rfq_service.RFQNotFoundError("RFQ not found.")
        audit = rfq_service.list_events(rfq_id, 500)
        return {"rfq": rfq, "quotes": rfq_service.list_quotes(rfq_id), "events": audit["events"],
                "pending_events": audit["pending_events"],
                "notice": "RFQ is an internal sourcing record; no supplier has been contacted."}
    return _rfq_call(_load)


@router.post("/rfqs/{rfq_id}/close")
def close_rfq(rfq_id: str, body: RFQStateChange, admin: UserPublic = Depends(require_admin),
              x_correlation_id: Optional[str] = Header(None)):
    """open -> closed (no further quotes; comparison still allowed). 404; 409 if not open; 422 no reason."""
    _check_id(rfq_id, "RFQ")
    return _rfq_call(rfq_service.close_rfq, rfq_id, body.reason, admin.username, x_correlation_id)


@router.post("/rfqs/{rfq_id}/cancel")
def cancel_rfq(rfq_id: str, body: RFQStateChange, admin: UserPublic = Depends(require_admin),
               x_correlation_id: Optional[str] = Header(None)):
    """open -> cancelled (no quotes, verification, comparison or decision afterwards). 404; 409; 422."""
    _check_id(rfq_id, "RFQ")
    return _rfq_call(rfq_service.cancel_rfq, rfq_id, body.reason, admin.username, x_correlation_id)


@router.get("/rfqs/{rfq_id}/events")
def rfq_events(rfq_id: str, limit: int = Query(500, ge=1, le=1000), _: UserPublic = Depends(require_admin)):
    _check_id(rfq_id, "RFQ")
    return _rfq_call(rfq_service.list_events, rfq_id, limit)


# ------------------------------------------------------------------------------------- quotes

@router.post("/rfqs/{rfq_id}/quotes", status_code=201)
def submit_quote(rfq_id: str, body: QuoteSubmission, admin: UserPublic = Depends(require_admin),
                 x_correlation_id: Optional[str] = Header(None)):
    """Structured quote (stored immutably under ``submitted``). 201; 404; 409 RFQ not open/past deadline,
    vendor not invited/approved or duplicate active quote (use /revise); 422 invalid values."""
    _check_id(rfq_id, "RFQ")
    return _rfq_call(rfq_service.submit_structured_quote, rfq_id, body, admin.username, x_correlation_id)


_PDF_DESCRIPTION = (
    "Upload a quotation PDF as the **raw request body** (not multipart): `Content-Type: application/pdf`, "
    "vendor/reference in the query string. Limits: 5 MiB (413, enforced while streaming), `%PDF-` signature, "
    "not encrypted, 1-20 pages (422). Bytes go to private file storage; MongoDB keeps only storage key, sha256, "
    "size and page count. Extracted text is untrusted and the quote stays `pending_verification`.\n\n"
    "Example: `curl -X POST -H 'Authorization: Bearer …' -H 'Content-Type: application/pdf' "
    "--data-binary @quote.pdf '/api/vendor-sourcing/rfqs/RFQ-…/quotes/pdf?vendor_id=VEN-002&quote_reference=Q-1'`"
)


@router.post("/rfqs/{rfq_id}/quotes/pdf", status_code=201, description=_PDF_DESCRIPTION,
             openapi_extra={"requestBody": {"required": True, "content": {
                 "application/pdf": {"schema": {"type": "string", "format": "binary"}}}}})
async def submit_pdf_quote(rfq_id: str, request: Request,
                           vendor_id: str = Query(..., min_length=3, max_length=80),
                           quote_reference: str = Query(..., min_length=1, max_length=120),
                           filename: str = Query("quotation.pdf", max_length=200),
                           admin: UserPublic = Depends(require_admin),
                           x_correlation_id: Optional[str] = Header(None)):
    _check_id(rfq_id, "RFQ")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/pdf":
        raise HTTPException(415, "Content-Type must be application/pdf.")
    limit = rfq_service.MAX_PDF_BYTES
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "PDF exceeds the 5 MiB upload limit.")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(413, "PDF exceeds the 5 MiB upload limit.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    return _rfq_call(rfq_service.submit_pdf_quote, rfq_id, vendor_id, quote_reference, filename, raw,
                     admin.username, x_correlation_id)


@router.get("/rfqs/{rfq_id}/quotes/{quote_id}/pdf", response_class=Response,
            responses={200: {"content": {"application/pdf": {}}}})
def download_quote_pdf(rfq_id: str, quote_id: str, _: UserPublic = Depends(require_admin)):
    """Admin-only download of the original stored PDF (sha256 re-checked). 404 if not a PDF quote."""
    _check_id(rfq_id, "RFQ")
    _check_id(quote_id, "Quotation")
    data, filename = _rfq_call(rfq_service.read_quote_pdf, rfq_id, quote_id)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:120] or "quotation.pdf"
    return Response(content=data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{safe}"', "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store"})


@router.post("/rfqs/{rfq_id}/quotes/{quote_id}/revise", status_code=201)
def revise_quote(rfq_id: str, quote_id: str, body: QuoteRevision, admin: UserPublic = Depends(require_admin),
                 x_correlation_id: Optional[str] = Header(None)):
    """New structured revision (``revision`` n+1, ``revision_of``); previous quote becomes ``superseded``.
    201; 404; 409 RFQ not open, already superseded or concurrent revision; 422."""
    _check_id(rfq_id, "RFQ")
    _check_id(quote_id, "Quotation")
    return _rfq_call(rfq_service.revise_quote, rfq_id, quote_id, body, admin.username, x_correlation_id)


@router.post("/rfqs/{rfq_id}/quotes/{quote_id}/verify")
def verify_quote(rfq_id: str, quote_id: str, body: QuoteVerification,
                 admin: UserPublic = Depends(require_admin), x_correlation_id: Optional[str] = Header(None)):
    """Store reviewer-``verified`` terms beside the immutable ``submitted`` ones with ``verification.diff``.
    200; 404; 409 not pending/superseded/cancelled RFQ/concurrent; 422."""
    _check_id(rfq_id, "RFQ")
    _check_id(quote_id, "Quotation")
    return _rfq_call(rfq_service.verify_quote, rfq_id, quote_id, body, admin.username, x_correlation_id)


# ------------------------------------------------------------------------------------- comparison

@router.post("/rfqs/{rfq_id}/compare")
def compare_quotes(rfq_id: str, admin: UserPublic = Depends(require_admin),
                   x_correlation_id: Optional[str] = Header(None)):
    """Deterministic gate + lowest-cost ranking with an explanatory policy score; persisted with
    ``human_decision: pending``. 200 (open or closed RFQ); 404; 409 cancelled RFQ."""
    _check_id(rfq_id, "RFQ")
    return _rfq_call(rfq_service.compare_quotes, rfq_id, admin.username, x_correlation_id)


@router.get("/rfqs/{rfq_id}/comparisons")
def list_comparisons(rfq_id: str, limit: int = Query(50, ge=1, le=200), _: UserPublic = Depends(require_admin)):
    _check_id(rfq_id, "RFQ")

    def _load():
        if rfq_service.get_rfq(rfq_id) is None:
            raise rfq_service.RFQNotFoundError("RFQ not found.")
        return {"rfq_id": rfq_id, "items": rfq_service.list_comparisons(rfq_id, limit)}
    return _rfq_call(_load)


@router.get("/rfqs/{rfq_id}/comparisons/{comparison_id}")
def get_comparison(rfq_id: str, comparison_id: str, _: UserPublic = Depends(require_admin)):
    """One stored comparison, including each row's explanatory ``score`` (components, weights, raw). 404 if
    the RFQ or comparison is missing or the comparison belongs to another RFQ."""
    _check_id(rfq_id, "RFQ")
    _check_id(comparison_id, "Comparison")

    def _load():
        if rfq_service.get_rfq(rfq_id) is None:
            raise rfq_service.RFQNotFoundError("RFQ not found.")
        comparison = rfq_service.get_comparison(rfq_id, comparison_id)
        if comparison is None:
            raise rfq_service.RFQNotFoundError("Comparison not found.")
        return comparison
    return _rfq_call(_load)


@router.post("/rfqs/{rfq_id}/comparisons/{comparison_id}/decision")
def decide_comparison(rfq_id: str, comparison_id: str, body: ComparisonDecision,
                      admin: UserPublic = Depends(require_admin), x_correlation_id: Optional[str] = Header(None)):
    """Record the human accept/reject of a comparison. Records the selection only — no award, PO or dispatch.
    Accept closes an open RFQ. 200; 404; 409 already decided/stale/ineligible/cancelled; 422."""
    _check_id(rfq_id, "RFQ")
    _check_id(comparison_id, "Comparison")
    return _rfq_call(rfq_service.decide_comparison, rfq_id, comparison_id, body, admin.username, x_correlation_id)
