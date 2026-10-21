"""Contracts for vendor discovery and onboarding. Submission never implies approval."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

CandidateStatus = Literal["pending_review", "approved", "rejected", "suspended"]
CandidateSource = Literal["manual", "external_discovery"]
ACTIVE_STATUSES = ("pending_review", "approved", "suspended")

MAX_PAGE_LIMIT = 200
MAX_PAGE_OFFSET = 10_000


class ComplianceEvidenceIn(BaseModel):
    """Evidence *metadata* only (no file). A submitted reference is never treated as verified proof."""
    model_config = ConfigDict(extra="forbid")

    document_type: str = Field(min_length=2, max_length=120)
    reference: str = Field(min_length=1, max_length=500)
    expires_on: Optional[date] = None
    verified: bool = False

    @field_validator("document_type", "reference")
    @classmethod
    def collapse_ws(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("verified")
    @classmethod
    def never_verified_on_submit(cls, value: bool) -> bool:
        if value:
            raise ValueError("evidence cannot be submitted as verified; verification is a separate reviewer action")
        return False


class ComplianceEvidenceView(BaseModel):
    document_type: str
    reference: str
    expires_on: Optional[date] = None
    verified: bool = False
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = None


class StatusHistoryEntry(BaseModel):
    from_status: Optional[str] = None
    to_status: str
    actor: str
    reason: Optional[str] = None
    at: datetime
    correlation_id: Optional[str] = None


class CandidateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=160)
    services: list[str] = Field(min_length=1, max_length=30)
    regions: list[str] = Field(default_factory=list, max_length=30)
    contact_email: Optional[str] = Field(default=None, max_length=254)
    source: CandidateSource = "manual"
    source_reference: Optional[str] = Field(default=None, max_length=500)
    notes: str = Field(default="", max_length=2000)
    compliance_documents: list[str] = Field(default_factory=list, max_length=30)
    compliance_evidence: list[ComplianceEvidenceIn] = Field(default_factory=list, max_length=30)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("name cannot be blank")
        return value

    @field_validator("services", "regions", "compliance_documents")
    @classmethod
    def normalize_list(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(" ".join(v.split()) for v in values if v.strip()))


class CandidateDecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject", "suspend"]
    reason: str = Field(min_length=5, max_length=1000)


class CandidateView(BaseModel):
    candidate_id: str
    name: str
    services: list[str]
    regions: list[str]
    contact_email: Optional[str] = None
    source: str
    source_reference: Optional[str] = None
    notes: str = ""
    compliance_documents: list[str] = Field(default_factory=list)
    status: CandidateStatus
    created_by: str
    created_at: datetime
    updated_at: datetime
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_reason: Optional[str] = None
    compliance_evidence: list[ComplianceEvidenceView] = Field(default_factory=list)
    status_history: list[StatusHistoryEntry] = Field(default_factory=list)
    # True when the state change is committed but its audit event is not yet in vendor_candidate_events
    # (it stays queued on the candidate document and is delivered by repair_pending_events()).
    audit_pending: bool = False


class VendorSearchResult(BaseModel):
    internal_vendors: list[dict]
    candidates: list[CandidateView]
    external_discovery_enabled: bool = False
    note: str = "Internal catalog and candidate registry searched first; no external search was run."
