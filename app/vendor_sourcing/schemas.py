"""Contracts for vendor discovery and onboarding. Submission never implies approval."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

CandidateStatus = Literal["pending_review", "approved", "rejected", "suspended"]
CandidateSource = Literal["manual", "external_discovery"]


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


class VendorSearchResult(BaseModel):
    internal_vendors: list[dict]
    candidates: list[CandidateView]
    external_discovery_enabled: bool = False
    note: str = "Internal catalog and candidate registry searched first; no external search was run."
