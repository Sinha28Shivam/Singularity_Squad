"""Contracts for the role-based portal: users, sessions, ticket lifecycle, submissions and views.

The agent/workflow contracts live in app/schemas.py and are unchanged.
"""
from __future__ import annotations

import datetime as dt
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

Role = Literal["user", "approver", "admin"]

# Ticket lifecycle (tickets.status). Workflow outcome stays in workflow_result.final_status and the
# Phase 2 approval outcome lives in the approval_requests collection.
TicketStatus = Literal[
    "submitted",         # created by a user, waiting for an admin to run the workflow
    "processing",        # admin started a run
    "reviewed",          # workflow finished, no approval required or hard-blocked; admin may close it
    "needs_info",        # workflow needs clarification; back with the user for edit & resubmit
    "pending_approval",  # an approval request is pending for an eligible approver (Phase 2)
    "changes_requested", # approver asked for changes; requester edits & resubmits (Phase 2)
    "approval_expired",  # approval request expired without a decision (Phase 2)
    "approved",          # approved by an eligible human (final; PO stays draft, nothing purchased)
    "rejected",          # rejected (final)
    "failed",            # workflow raised
    "persistence_error", # a database write failed mid-way
]
FINAL_STATUSES = {"approved", "rejected"}
USER_EDITABLE_STATUSES = {"submitted", "needs_info", "changes_requested", "approval_expired"}
RUNNABLE_STATUSES = {"submitted", "reviewed", "needs_info", "failed", "persistence_error", "approval_expired"}

EventCategory = Literal[
    "team_lunch", "offsite", "celebration", "training_workshop", "town_hall", "client_visit", "other"
]
EVENT_CATEGORY_LABELS: dict[str, str] = {
    "team_lunch": "Team lunch",
    "offsite": "Team offsite",
    "celebration": "Celebration / party",
    "training_workshop": "Training / workshop",
    "town_hall": "Town hall / all-hands",
    "client_visit": "Client visit",
    "other": "Other event",
}
CateringService = Literal["lunch", "breakfast", "dinner", "snacks"]


# ---------- users & auth ----------
class SignupIn(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(max_length=254, pattern=EMAIL_PATTERN)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username", "email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name is required")
        return v


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserPublic(BaseModel):
    username: str
    name: str
    email: str
    role: Role


class AuthOut(BaseModel):
    token: str
    expires_at: dt.datetime
    user: UserPublic


# ---------- ticket submission ----------
class TicketSubmission(BaseModel):
    """Structured event request filled in by a user (plus optional free text)."""
    title: str = Field(min_length=3, max_length=120)
    event_category: EventCategory = "team_lunch"
    service: CateringService = "lunch"
    event_date: dt.date
    attendees: int = Field(ge=1, le=5000)
    budget_inr: float = Field(gt=0, le=10_000_000)
    location: Optional[str] = Field(default=None, max_length=120)
    approved_vendor_only: bool = True
    notes: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("title", "location", "notes")
    @classmethod
    def _strip(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        return v or None


class TicketNote(BaseModel):
    author: str          # username
    role: Role
    text: str
    created_at: dt.datetime


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class RunIn(BaseModel):
    invoice_id: Optional[str] = None
    today: Optional[dt.date] = None


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    note: Optional[str] = Field(default=None, max_length=2000)


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    by: str
    at: dt.datetime
    note: Optional[str] = None


# ---------- views ----------
class TicketListItem(BaseModel):
    ticket_id: str
    title: Optional[str] = None
    event_category: Optional[str] = None
    event_date: Optional[str] = None
    owner_username: Optional[str] = None   # admin list only
    owner_name: Optional[str] = None       # admin list only
    status: str
    final_status: Optional[str] = None
    run_count: int = 0
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None


class UserTicketView(BaseModel):
    """What a requester sees. Deliberately excludes agent internals (vendors, invoice checks, audit)."""
    ticket_id: str
    status: str
    submission: TicketSubmission
    outcome: str                                   # short human-readable outcome
    clarification_questions: list[str] = Field(default_factory=list)
    po_summary: Optional[dict] = None              # {po_id, vendor_name, total, currency, status, approval_status}
    notes: list[TicketNote] = Field(default_factory=list)
    decision: Optional[Decision] = None
    approval: Optional[dict] = None                # requester-safe approval summary (Phase 2)
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None


class AdminTicketView(BaseModel):
    """Full ticket for admins, including the latest workflow_result snapshot."""
    ticket_id: str
    status: str
    owner_username: Optional[str] = None
    owner_name: Optional[str] = None
    owner_email: Optional[str] = None
    submission: Optional[TicketSubmission] = None
    raw_text: Optional[str] = None
    workflow_result: Optional[dict] = None
    notes: list[TicketNote] = Field(default_factory=list)
    decision: Optional[Decision] = None
    run_count: int = 0
    last_run_by: Optional[str] = None
    error: Optional[str] = None
    approval: Optional[dict] = None                # current approval request + history (Phase 2)
    created_at: Optional[dt.datetime] = None
    updated_at: Optional[dt.datetime] = None
