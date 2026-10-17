"""Phase 2 approval contracts. Approval state is separate from workflow final_status and PO status.

Approving records an internal human authorization only. It never places an order, sends a PO
to a vendor, approves an invoice for payment or moves money.
"""
from __future__ import annotations

import datetime as dt
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

ApprovalStatus = Literal["pending", "approved", "rejected", "changes_requested", "expired", "superseded"]
TERMINAL_APPROVAL_STATUSES = {"approved", "rejected", "changes_requested", "expired", "superseded"}
DecisionAction = Literal["approve", "reject", "request_changes"]
RiskLevel = Literal["low", "medium", "high"]

ACTION_TO_STATUS: dict[str, str] = {
    "approve": "approved",
    "reject": "rejected",
    "request_changes": "changes_requested",
}

APPROVAL_SAFETY_NOTICE = "Approval records internal authorization only. No order or payment is executed."


class ApprovalRequirement(BaseModel):
    """Output of the deterministic approval policy (never produced by an LLM)."""
    required: bool
    blocked: bool = False                     # hard policy block: no approval request, stays blocked
    needs_clarification: bool = False         # ask requester for info; no approval request yet
    reason_codes: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "low"
    policy_version: str
    required_role: str = "approver"
    amount: Optional[float] = None
    currency: str = "INR"
    facts: dict = Field(default_factory=dict)  # thresholds/amounts used by the rules


class DecisionRecord(BaseModel):
    decision_id: str
    action: DecisionAction
    actor_id: str                 # username from the authenticated session, never from the body
    actor_role: str               # role from the authenticated session
    comment: Optional[str] = None
    decided_at: dt.datetime
    idempotency_key: str
    request_version: int
    workflow_snapshot_hash: str


class ApprovalRequest(BaseModel):
    approval_id: str
    ticket_id: str
    request_version: int
    ticket_version: int           # ticket run_count at creation (which evaluated version)
    status: ApprovalStatus
    requester_id: str
    required_role: str
    assigned_approver_id: Optional[str] = None
    reason_codes: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = "low"
    amount: Optional[float] = None
    currency: str = "INR"
    policy_version: str
    workflow_snapshot_hash: str
    review_snapshot: dict = Field(default_factory=dict)   # the canonical fields covered by the hash
    created_at: dt.datetime
    expires_at: dt.datetime
    decided_at: Optional[dt.datetime] = None
    decisions: list[DecisionRecord] = Field(default_factory=list)
    reconciliation_required: bool = False
    schema_version: int = 1


class DecisionIn(BaseModel):
    """Body of POST /api/approvals/{approval_id}/decision. Identity/role come from the token only."""
    model_config = {"extra": "forbid"}  # reject actor_id / role / status smuggled in the body

    action: DecisionAction
    comment: Optional[str] = Field(default=None, max_length=2000)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    request_version: int = Field(ge=1)
    workflow_snapshot_hash: str = Field(min_length=16, max_length=128, pattern=r"^[0-9a-f]+$")

    @field_validator("comment")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


class ApprovalListItem(BaseModel):
    """Queue row: no request text, only a short safe summary."""
    approval_id: str
    ticket_id: str
    request_version: int
    status: ApprovalStatus
    requester_id: str
    summary: str                  # e.g. "Team lunch · 100 attendees · 2026-10-23"
    amount: Optional[float] = None
    currency: str = "INR"
    risk_level: RiskLevel
    reason_codes: list[str]
    created_at: dt.datetime
    expires_at: dt.datetime
    can_decide: bool = False      # computed server-side for the calling user (UI hint only)


class ApprovalView(ApprovalRequest):
    can_decide: bool = False
    cannot_decide_reason: Optional[str] = None
    safety_notice: str = APPROVAL_SAFETY_NOTICE


class TicketApprovalView(BaseModel):
    """GET /api/tickets/{ticket_id}/approval: current request + full version history."""
    ticket_id: str
    requirement: Optional[ApprovalRequirement] = None
    current: Optional[ApprovalView] = None
    history: list[ApprovalView] = Field(default_factory=list)
    safety_notice: str = APPROVAL_SAFETY_NOTICE
