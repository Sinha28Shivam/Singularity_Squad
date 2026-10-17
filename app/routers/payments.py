"""Dummy payment initiator routes — SIMULATION ONLY. No bank or gateway is contacted; no money is transferred.

Identity/role come from the bearer session. Initiation (create/retry/reconcile, eligibility, metrics) is
admin-only (operations); approvers approve elsewhere and can only read here; users get 403. Request bodies
forbid extra fields, so the client can never set ``mode``, ``money_transferred``, provider or account fields.
Every response carries ``mode="SIMULATION"`` and ``money_transferred=false``.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, Path, Query
from fastapi.responses import JSONResponse

from app.auth.deps import require_admin, require_approver_or_admin
from app.payments import metrics
from app.payments import service
from app.payments.schemas import SIMULATION_DISCLAIMER, PaymentRetryIn, PaymentSimulationIn
from app.portal_schemas import UserPublic

PREFIX = "/api/payments/simulation"
PAYMENT_ID_PATTERN = r"^psim_[a-f0-9]{8,40}$"
TICKET_ID_PATTERN = r"^REQ-[A-Za-z0-9-]{1,64}$"
INVOICE_ID_PATTERN = r"^INV-[A-Za-z0-9-]{1,32}$"

router = APIRouter(prefix=PREFIX, tags=["payments (simulation)"])


def _json(model) -> dict:
    return model.model_dump(mode="json")


@router.get("/eligibility/{invoice_id}")
def eligibility(invoice_id: str = Path(..., pattern=INVOICE_ID_PATTERN),
                ticket_id: str = Query(..., pattern=TICKET_ID_PATTERN),
                admin: UserPublic = Depends(require_admin)):
    return _json(service.get_eligibility(ticket_id, invoice_id, admin))


@router.get("/metrics")
def payment_metrics(_: UserPublic = Depends(require_admin)):
    return metrics.snapshot()


@router.post("")
def create(body: PaymentSimulationIn, admin: UserPublic = Depends(require_admin),
           x_correlation_id: Optional[str] = Header(None)):
    result, status_code = service.create_simulation(body, admin, x_correlation_id)
    return JSONResponse(status_code=status_code, content=_json(result))


@router.get("")
def list_payments(ticket_id: Optional[str] = Query(None, pattern=TICKET_ID_PATTERN),
                  limit: int = Query(100, ge=1, le=200),
                  _: UserPublic = Depends(require_approver_or_admin)):
    items = service.list_payments(ticket_id, limit)
    return {"mode": "SIMULATION", "money_transferred": False, "disclaimer": SIMULATION_DISCLAIMER,
            "payments": [_json(p) for p in items]}


@router.get("/{payment_id}")
def get_payment(payment_id: str = Path(..., pattern=PAYMENT_ID_PATTERN),
                _: UserPublic = Depends(require_approver_or_admin)):
    return _json(service.get_payment(payment_id))


@router.get("/{payment_id}/events")
def payment_events(payment_id: str = Path(..., pattern=PAYMENT_ID_PATTERN),
                   _: UserPublic = Depends(require_approver_or_admin)):
    events = service.list_events(payment_id)
    return {"payment_id": payment_id, "mode": "SIMULATION", "money_transferred": False,
            "events": [_json(e) for e in events]}


@router.post("/{payment_id}/retry")
def retry(body: PaymentRetryIn, payment_id: str = Path(..., pattern=PAYMENT_ID_PATTERN),
          admin: UserPublic = Depends(require_admin), x_correlation_id: Optional[str] = Header(None)):
    return _json(service.retry(payment_id, admin, x_correlation_id))


@router.post("/{payment_id}/reconcile")
def reconcile(payment_id: str = Path(..., pattern=PAYMENT_ID_PATTERN), _: UserPublic = Depends(require_admin)):
    payment = service.reconcile_audit(payment_id)
    return {"payment": _json(payment), "mode": "SIMULATION", "money_transferred": False,
            "message": "Audit trail reconciled. No money was transferred."}
