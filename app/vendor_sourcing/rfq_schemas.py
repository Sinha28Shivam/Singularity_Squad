"""Validated RFQ and quotation contracts. Monetary values use Decimal, never binary floats."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RFQCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=4, max_length=80, pattern=r"^REQ-[A-Za-z0-9-]{1,64}$")
    service: str = Field(min_length=2, max_length=80)
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    budget_limit: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    delivery_date: date
    delivery_location: str = Field(min_length=2, max_length=200)
    warranty_months_required: int = Field(default=0, ge=0, le=120)
    # Free-text scope; whether a quote's scope matches is a reviewer judgement (QuoteVerification.warranty_scope_accepted).
    warranty_scope_required: Optional[str] = Field(default=None, min_length=1, max_length=1000)
    compliance_requirements: list[str] = Field(default_factory=list, max_length=30)
    response_deadline: datetime
    candidate_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("service", "delivery_location")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("response_deadline")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("response_deadline must include a timezone, for example Z or +05:30")
        return value


class RFQStateChange(BaseModel):
    """Body for POST /rfqs/{id}/close and /cancel."""
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=5, max_length=1000)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        value = " ".join(value.split())
        if len(value) < 5:
            raise ValueError("reason must contain at least 5 non-blank characters")
        return value


class ComplianceEvidence(BaseModel):
    """Per-requirement compliance state. Only ``verified`` evidence that has not expired passes the gate."""
    model_config = ConfigDict(extra="forbid")

    status: Literal["verified", "declared", "missing"]
    evidence_ref: Optional[str] = Field(default=None, min_length=1, max_length=300)
    expires_on: Optional[date] = None

    @model_validator(mode="after")
    def verified_needs_evidence(self) -> "ComplianceEvidence":
        if self.status == "verified" and not self.evidence_ref:
            raise ValueError("verified compliance requires an evidence_ref")
        return self


ComplianceValue = Union[bool, ComplianceEvidence]


def _normalize_compliance(value: dict[str, Any]) -> dict[str, ComplianceEvidence]:
    """Legacy booleans are declarations, not proof: True -> declared, False -> missing."""
    out: dict[str, ComplianceEvidence] = {}
    for key, item in (value or {}).items():
        name = " ".join(str(key).split())
        if not name or len(name) > 80:
            raise ValueError("compliance requirement names must be 1-80 characters")
        if isinstance(item, bool):
            out[name] = ComplianceEvidence(status="declared" if item else "missing")
        else:
            out[name] = item
    if len(out) > 30:
        raise ValueError("at most 30 compliance entries are allowed")
    return out


class _QuoteTerms(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit_price: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    tax_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    delivery_fee: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    other_fees: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    delivery_date: date
    warranty_months: int = Field(ge=0, le=120)
    warranty_scope: str = Field(min_length=1, max_length=1000)
    compliance: dict[str, ComplianceValue] = Field(default_factory=dict)

    @field_validator("compliance")
    @classmethod
    def normalize_compliance(cls, value: dict) -> dict:
        return _normalize_compliance(value)


class QuoteSubmission(_QuoteTerms):
    vendor_id: str = Field(min_length=3, max_length=80)
    quote_reference: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=2000)


class QuoteRevision(_QuoteTerms):
    """A vendor's revised structured quote; the vendor is taken from the quote being revised."""
    quote_reference: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=2000)
    revision_reason: str = Field(min_length=5, max_length=1000)


class QuoteVerification(_QuoteTerms):
    reviewer_note: str = Field(min_length=5, max_length=1000)
    # Reviewer judgement that the quoted warranty scope satisfies RFQ.warranty_scope_required.
    warranty_scope_accepted: bool = False


class ComparisonDecision(BaseModel):
    """Human selection on a persisted comparison. Records the decision only: no award, PO or dispatch."""
    model_config = ConfigDict(extra="forbid")

    decision: Literal["accept", "reject"]
    quote_id: Optional[str] = Field(default=None, min_length=3, max_length=80)
    reason: str = Field(min_length=5, max_length=1000)

    @model_validator(mode="after")
    def accept_needs_quote(self) -> "ComparisonDecision":
        if self.decision == "accept" and not self.quote_id:
            raise ValueError("accept requires the quote_id being selected")
        return self


class QuoteView(BaseModel):
    quote_id: str
    rfq_id: str
    vendor_id: str
    quote_reference: str
    submission_type: str
    status: str
    currency: Optional[str] = None
    quantity: Decimal
    revision: int = 1
    revision_of: Optional[str] = None
    superseded_by: Optional[str] = None
    unit_price: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    delivery_fee: Optional[Decimal] = None
    other_fees: Optional[Decimal] = None
    delivery_date: Optional[date] = None
    warranty_months: Optional[int] = None
    warranty_scope: Optional[str] = None
    compliance: dict[str, Any] = Field(default_factory=dict)
    submitted: dict[str, Any] = Field(default_factory=dict)
    verified: Optional[dict[str, Any]] = None
    verification: Optional[dict[str, Any]] = None
    pdf_filename: Optional[str] = None
    pdf_sha256: Optional[str] = None
    pdf_size: Optional[int] = None
    pdf_page_count: Optional[int] = None
    extracted_text: Optional[str] = None
    reviewer_note: Optional[str] = None
    submitted_at: datetime
    verified_at: Optional[datetime] = None
    verified_by: Optional[str] = None
    audit_pending: bool = False
