"""FastAPI entrypoint. Run: python -m uvicorn app.main:app --reload"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import llm
from app.agents import billing, policy
from app.auth.deps import require_admin
from app.auth.errors import AuthError
from app.auth.service import seed_admin_from_env
from app.data_loader import load_json
from app.db import PersistenceError, close_client, ensure_indexes, persistence_mode, ping_database
from app.db.mongo import db_status
from app.repositories import get_audit_entries, get_ticket, list_recent_tickets
from app.routers import admin_tickets as admin_tickets_router
from app.routers import auth as auth_router
from app.routers import my_tickets as my_tickets_router
from app.schemas import DraftPO, ReconciliationReport, WorkflowResult
from app.services import ticket_service
from app.services.errors import TicketError

logger = logging.getLogger(__name__)

TICKET_ID_PATTERN = r"^REQ-[A-Za-z0-9-]{1,64}$"
_DISABLED_MSG = "Persistence is disabled"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if persistence_mode() == "required":
        try:
            ping_database()
            ensure_indexes()
            try:
                seed_admin_from_env()
            except Exception as seed_exc:  # noqa: BLE001 - never block startup on admin seeding
                logger.warning("Admin seeding failed (%s).", type(seed_exc).__name__)
        except Exception as exc:  # noqa: BLE001 - start anyway; health reports unavailable
            logger.warning("MongoDB not reachable at startup (%s); persistence endpoints will return 503.",
                           type(exc).__name__)
    try:
        yield
    finally:
        close_client()


app = FastAPI(title="WorkplaceOps AI", version="0.1.0", lifespan=lifespan,
              description="Multi-agent workplace operations copilot (synthetic demo, draft-only).")


@app.exception_handler(PersistenceError)
async def _persistence_error_handler(_: Request, exc: PersistenceError):
    logger.error("Persistence error (%s)", type(exc).__name__)
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(AuthError)
async def _auth_error_handler(_: Request, exc: AuthError):
    headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=headers)


app.include_router(auth_router.router)


@app.exception_handler(TicketError)
async def _ticket_error_handler(_: Request, exc: TicketError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(my_tickets_router.router)
app.include_router(admin_tickets_router.router)
_ADMIN_ONLY = [Depends(require_admin)]


class WorkflowIn(BaseModel):
    text: str
    today: Optional[date] = None
    invoice_id: Optional[str] = None


class ReconcileIn(BaseModel):
    po: DraftPO
    invoice_id: str


def _require_persistence() -> None:
    if persistence_mode() == "disabled":
        raise HTTPException(503, _DISABLED_MSG)


@app.get("/api/health")
def health():
    return {"status": "ok", "llm_enabled": llm.llm_enabled(), "model": llm.model_name(), **db_status()}


@app.get("/api/samples")
def samples():
    return load_json("sample_requests.json")


@app.get("/api/invoices")
def invoices():
    return [{"invoice_id": i.invoice_id, "vendor_id": i.vendor_id, "po_id": i.po_id,
             "total": i.total, "delivery_id": i.delivery_id} for i in billing.load_invoices()]


@app.post("/api/workflow", response_model=WorkflowResult, dependencies=_ADMIN_ONLY)
def workflow(body: WorkflowIn):
    if not body.text.strip():
        raise HTTPException(400, "Request text is empty.")
    try:
        return ticket_service.run_and_persist(body.text, body.today, body.invoice_id)
    except PersistenceError as exc:
        raise HTTPException(503, str(exc)) from None


@app.get("/api/tickets", dependencies=_ADMIN_ONLY)
def tickets(limit: int = Query(20, ge=1, le=100)):
    _require_persistence()
    return {"tickets": list_recent_tickets(limit)}


@app.get("/api/tickets/{ticket_id}", dependencies=_ADMIN_ONLY)
def ticket(ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN)):
    _require_persistence()
    doc = get_ticket(ticket_id)
    if doc is None:
        raise HTTPException(404, f"Ticket {ticket_id} not found.")
    return doc


@app.get("/api/tickets/{ticket_id}/audit", dependencies=_ADMIN_ONLY)
def ticket_audit(ticket_id: str = Path(..., pattern=TICKET_ID_PATTERN),
                 limit: int = Query(500, ge=1, le=500)):
    _require_persistence()
    if get_ticket(ticket_id) is None:
        raise HTTPException(404, f"Ticket {ticket_id} not found.")
    return {"ticket_id": ticket_id, "events": get_audit_entries(ticket_id, limit)}


@app.post("/api/reconcile", response_model=ReconciliationReport, dependencies=_ADMIN_ONLY)
def reconcile(body: ReconcileIn):
    inv = billing.get_invoice(body.invoice_id)
    if inv is None:
        raise HTTPException(404, f"Invoice {body.invoice_id} not found.")
    inv, delivery, _ = billing.bind_demo_documents(inv, billing.get_delivery(inv.delivery_id), body.po)
    return billing.reconcile(inv, body.po, delivery, billing.load_history(), policy.load_rules())
