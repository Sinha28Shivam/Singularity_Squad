"""Shared data contracts for WorkplaceOps AI. All data used with these models is synthetic."""
from __future__ import annotations

import datetime as dt
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

CheckStatus = Literal["pass", "warn", "fail", "uncertain"]
FinalStatus = Literal["pass", "warning", "blocked", "needs_clarification"]


class EventRequest(BaseModel):
    request_id: str = "REQ-1001"
    raw_text: str = ""
    category: str = "employee_event"
    service: str = "lunch"
    date: Optional[dt.date] = None
    attendees: Optional[int] = None
    budget_limit: Optional[float] = None
    currency: str = "INR"
    location: Optional[str] = None
    urgency: str = "normal"
    requirements: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    source: str  # e.g. "policy:EVT-4.2", "data:vendors.json#VEN-002", "calc"
    detail: str


class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    detail: str
    evidence: list[Evidence] = Field(default_factory=list)


class PolicyClause(BaseModel):
    clause_id: str
    title: str
    text: str
    source_file: str
    score: float = 0.0


class ApprovalTier(BaseModel):
    max_amount: Optional[float]  # None = no upper bound
    approver: str


class PolicyRules(BaseModel):
    """Structured mirror of data/policy_rules.json; each rule maps to a clause id."""
    per_head_catering_cap: float
    approved_vendor_only: bool
    min_lead_time_business_days: int
    max_attendees_without_facilities_review: int
    approval_tiers: list[ApprovalTier]
    invoice_amount_tolerance: float
    clause_refs: dict[str, str]


class PolicyReport(BaseModel):
    rules: PolicyRules
    clauses: list[PolicyClause] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    summary: str = ""


class Vendor(BaseModel):
    vendor_id: str
    name: str
    approved: bool
    services: list[str]
    rate_per_person: float
    delivery_setup_fee: float = 0.0
    gst_rate: float = 0.05
    capacity: int
    available_dates: list[date]
    rating: float = 0.0
    preferred: bool = False


class VendorEvaluation(BaseModel):
    vendor_id: str
    name: str
    eligible: bool
    reasons: list[str]  # why rejected, or why it fits
    estimated_total: Optional[float] = None


class VendorMatchResult(BaseModel):
    selected: Optional[Vendor] = None
    evaluations: list[VendorEvaluation] = Field(default_factory=list)
    replanned: bool = False  # True if the preferred/top-rated vendor was rejected
    explanation: str = ""


class LineItem(BaseModel):
    description: str
    quantity: float
    unit_price: float
    line_total: float


class DraftPO(BaseModel):
    po_id: str
    request_id: str
    vendor_id: str
    vendor_name: str
    status: Literal["draft"] = "draft"
    event_date: date
    line_items: list[LineItem]
    subtotal: float
    tax: float
    total: float
    currency: str = "INR"
    required_approver: str
    requires_human_approval: bool = True


class ProcurementResult(BaseModel):
    po: Optional[DraftPO] = None
    checks: list[CheckResult] = Field(default_factory=list)
    calculation_steps: list[str] = Field(default_factory=list)


class InvoiceLine(BaseModel):
    description: str
    quantity: float
    unit_price: float
    line_total: float


class Invoice(BaseModel):
    invoice_id: str
    po_id: str
    vendor_id: str
    invoice_date: date
    delivery_id: str
    line_items: list[InvoiceLine]
    subtotal: float
    tax: float
    total: float
    currency: str = "INR"


class DeliveryRecord(BaseModel):
    delivery_id: str
    po_id: str
    vendor_id: str
    delivered_on: date
    items: dict[str, float]  # description -> quantity delivered
    signed_by: str
    notes: str = ""


class ReconciliationReport(BaseModel):
    invoice_id: str
    status: Literal["match", "mismatch", "duplicate"]
    checks: list[CheckResult] = Field(default_factory=list)
    expected_total: float
    invoiced_total: float
    variance: float
    recommendation: str


class AuditEntry(BaseModel):
    step: int
    agent: str
    action: str
    status: CheckStatus | Literal["info"]
    detail: str
    evidence: list[Evidence] = Field(default_factory=list)
    llm_used: bool = False
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CriticReport(BaseModel):
    status: FinalStatus
    findings: list[CheckResult] = Field(default_factory=list)
    summary: str = ""


class WorkflowResult(BaseModel):
    ticket_id: Optional[str] = None  # set by the API layer when the run is persisted
    persisted: bool = False
    request: Optional[EventRequest] = None
    missing_fields: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    policy: Optional[PolicyReport] = None
    vendors: Optional[VendorMatchResult] = None
    procurement: Optional[ProcurementResult] = None
    reconciliation: Optional[ReconciliationReport] = None
    critic: Optional[CriticReport] = None
    audit_trail: list[AuditEntry] = Field(default_factory=list)
    final_status: FinalStatus = "needs_clarification"
    human_approval_required: bool = True
    disclaimer: str = (
        "Prototype using synthetic data. Draft PO only - no purchase, approval, or payment is executed."
    )
    extra: dict[str, Any] = Field(default_factory=dict)
