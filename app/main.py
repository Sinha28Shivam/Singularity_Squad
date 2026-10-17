"""FastAPI entrypoint. Run: python -m uvicorn app.main:app --reload"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app import llm
from app.agents import billing, policy
from app.data_loader import load_json
from app.orchestrator import run_workflow
from app.schemas import DraftPO, ReconciliationReport, WorkflowResult

app = FastAPI(title="WorkplaceOps AI", version="0.1.0",
              description="Multi-agent workplace operations copilot (synthetic demo, draft-only).")


class WorkflowIn(BaseModel):
    text: str
    today: Optional[date] = None
    invoice_id: Optional[str] = None


class ReconcileIn(BaseModel):
    po: DraftPO
    invoice_id: str


@app.get("/api/health")
def health():
    return {"status": "ok", "llm_enabled": llm.llm_enabled(), "model": llm.model_name()}


@app.get("/api/samples")
def samples():
    return load_json("sample_requests.json")


@app.get("/api/invoices")
def invoices():
    return [{"invoice_id": i.invoice_id, "vendor_id": i.vendor_id, "po_id": i.po_id,
             "total": i.total, "delivery_id": i.delivery_id} for i in billing.load_invoices()]


@app.post("/api/workflow", response_model=WorkflowResult)
def workflow(body: WorkflowIn):
    if not body.text.strip():
        raise HTTPException(400, "Request text is empty.")
    return run_workflow(body.text, body.today, body.invoice_id)


@app.post("/api/reconcile", response_model=ReconciliationReport)
def reconcile(body: ReconcileIn):
    inv = billing.get_invoice(body.invoice_id)
    if inv is None:
        raise HTTPException(404, f"Invoice {body.invoice_id} not found.")
    return billing.reconcile(inv, body.po, billing.get_delivery(inv.delivery_id),
                             billing.load_history(), policy.load_rules())
