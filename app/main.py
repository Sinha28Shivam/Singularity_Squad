"""FastAPI entrypoint. Run: python -m uvicorn app.main:app --reload"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import llm
from app.agents import billing, policy
from app.auth.deps import require_admin
from app.auth.errors import AuthError
from app.approvals.errors import ApprovalError
from app.auth.service import seed_admin_from_env, seed_approvers_from_env
from app.data_loader import load_json
from app.orchestrator import AnalysisBranchError
from app.db import PersistenceError, close_client, ensure_indexes, persistence_mode, ping_database
from app.db.mongo import db_status
from app.repositories import get_audit_entries, get_ticket, list_recent_tickets
from app.routers import admin_tickets as admin_tickets_router
from app.routers import ai_ops as ai_ops_router
from app.routers import approvals as approvals_router
from app.routers import auth as auth_router
from app.routers import my_tickets as my_tickets_router
from app.routers import payments as payments_router
from app.routers import vendor_candidates as vendor_candidates_router
from app.routers import vendor_sourcing as vendor_sourcing_router
from app.payments.config import validate_at_startup as validate_payment_settings
from app.payments.service import PaymentServiceError
from app.routers import policy_documents as policy_documents_router
from app.routers import policy_search as policy_search_router
from app.routers import policy_rules as policy_rules_router
from app.policy_rag.errors import PolicyRagError
from app.policy_rules.registry import RuleRegistryError
from app.schemas import DraftPO, ReconciliationReport, WorkflowResult
from app.services import ticket_service
from app.services.errors import TicketError

logger = logging.getLogger(__name__)

TICKET_ID_PATTERN = r"^REQ-[A-Za-z0-9-]{1,64}$"
_DISABLED_MSG = "Persistence is disabled"


def _seed_policy_library() -> None:
    """Ingest bundled policy PDFs not yet in the library (POLICY_RAG_SEED=0 disables). Only logs on failure."""
    if os.getenv("POLICY_RAG_SEED", "1").strip() == "0":
        return
    try:
        from app.policy_rag.ingestion import seed_from_directory
        results = seed_from_directory("data/policies", uploaded_by="system")
        if results:
            logger.info("Policy library seeding: %d new file(s), %d ok.", len(results),
                        sum(1 for r in results if r.ok))
    except Exception as exc:  # noqa: BLE001 - never block startup on policy seeding
        logger.warning("Policy library seeding failed (%s).", type(exc).__name__)


def _bootstrap_policy_rules() -> None:
    """Create + auto-approve the legacy_config seed rules as system:bootstrap (POLICY_RULES_BOOTSTRAP=0
    disables). Idempotent; only logs on failure."""
    if os.getenv("POLICY_RULES_BOOTSTRAP", "1").strip() == "0":
        return
    try:
        from app.policy_rules.registry import bootstrap_from_seed
        approved = bootstrap_from_seed(os.getenv("POLICY_RULES_SEED_FILE") or None)
        if approved:
            logger.info("Policy rule bootstrap: %d legacy rule(s) approved.", approved)
    except Exception as exc:  # noqa: BLE001 - never block startup on rule bootstrap
        logger.warning("Policy rule bootstrap failed (%s).", type(exc).__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        validate_payment_settings()  # logs problems; never raises (simulation fails closed)
    except Exception as exc:  # noqa: BLE001 - never block startup on payment config
        logger.warning("Payment simulation config check failed (%s).", type(exc).__name__)
    if persistence_mode() == "required":
        try:
            ping_database()
            ensure_indexes()
            try:
                seed_admin_from_env()
            except Exception as seed_exc:  # noqa: BLE001 - never block startup on admin seeding
                logger.warning("Admin seeding failed (%s).", type(seed_exc).__name__)
            try:
                seed_approvers_from_env()
            except Exception as seed_exc:  # noqa: BLE001 - never block startup on approver seeding
                logger.warning("Approver seeding failed (%s).", type(seed_exc).__name__)
            _seed_policy_library()
            _bootstrap_policy_rules()
        except Exception as exc:  # noqa: BLE001 - start anyway; health reports unavailable
            logger.warning("MongoDB not reachable at startup (%s); persistence endpoints will return 503.",
                           type(exc).__name__)
    try:
        yield
    finally:
        close_client()


app = FastAPI(title="WorkplaceOps AI", version="0.1.0", lifespan=lifespan,
              description="Multi-agent workplace operations copilot (synthetic demo, draft-only).")


def _cors_origins() -> list[str]:
    """Explicit allow-list for the React UI (CORS_ALLOWED_ORIGINS, comma-separated). '*' is ignored."""
    raw = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip() and o.strip() != "*"]


app.add_middleware(CORSMiddleware, allow_origins=_cors_origins(), allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT", "OPTIONS"],
                   allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
                   expose_headers=["Content-Disposition"])


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


@app.exception_handler(ApprovalError)
async def _approval_error_handler(_: Request, exc: ApprovalError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(my_tickets_router.router)
app.include_router(admin_tickets_router.router)
app.include_router(ai_ops_router.router)
# Registered before the legacy /api/tickets/{ticket_id} routes; only /api/tickets/{id}/approval is opened
# to owners/approvers, the legacy ticket routes stay admin-only.
app.include_router(approvals_router.router)


@app.exception_handler(PolicyRagError)
async def _policy_rag_error_handler(_: Request, exc: PolicyRagError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(policy_documents_router.router)
app.include_router(policy_search_router.router)


@app.exception_handler(RuleRegistryError)
async def _rule_registry_error_handler(_: Request, exc: RuleRegistryError):
    return JSONResponse(status_code=exc.status_code, content=exc.body())


app.include_router(policy_rules_router.router)


@app.exception_handler(PaymentServiceError)
async def _payment_error_handler(_: Request, exc: PaymentServiceError):
    return JSONResponse(status_code=exc.status_code, content=exc.body())


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    """Payment-simulation routes get a safe PaymentError body (field names only, no echoed input/internals);
    every other route keeps FastAPI's default 422 response."""
    if not request.url.path.startswith(payments_router.PREFIX):
        return await request_validation_exception_handler(request, exc)
    fields = sorted({(".".join(str(p) for p in (e.get("loc") or ())[1:]) or "body")[:64]
                     for e in exc.errors()})[:20]
    return JSONResponse(status_code=422, content={
        "error": "INVALID_REQUEST", "message": "The request is invalid. Rejected fields: " + ", ".join(fields)
        + ". Note: mode, money_transferred, provider and account fields are server-controlled.",
        "reasons": [], "mode": "SIMULATION", "money_transferred": False, "fields": fields})


app.include_router(payments_router.router)
app.include_router(vendor_candidates_router.router)
app.include_router(vendor_sourcing_router.router)
_ADMIN_ONLY = [Depends(require_admin)]


_WORKFLOW_ANALYSIS_FAILED_MSG = "Workflow analysis failed; the run was stopped and marked as failed."


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
    try:  # non-secret feature flag so UIs can hide the page; the payments API still enforces it
        from app.payments.config import load_settings
        payments_enabled = bool(load_settings().available)
    except Exception:  # noqa: BLE001
        payments_enabled = False
    return {"status": "ok", "llm_enabled": llm.llm_enabled(), "model": llm.model_name(),
            "payments_simulation_enabled": payments_enabled, **db_status()}


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
    except AnalysisBranchError:
        # The ticket is already marked failed and audited; never echo the branch exception to the client.
        raise HTTPException(500, _WORKFLOW_ANALYSIS_FAILED_MSG) from None


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
