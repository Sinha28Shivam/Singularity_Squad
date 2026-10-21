"""Typed state contract for the LangGraph PoC.

Inputs are set once by the caller. Every other key is written by exactly one node, except ``audit``
(append-only via ``operator.add``) and ``branch_errors`` (both analysis branches may append in the same
superstep, so it needs a reducer too). The parallel branches write disjoint keys (``policy`` / ``vendors``);
only ``join_and_gate`` turns them into audit entries, which keeps audit order deterministic.
"""
from __future__ import annotations

import operator
from datetime import date
from typing import Annotated, Optional, TypedDict

from app.schemas import (AuditEntry, CriticReport, EventRequest, PolicyReport, PolicyRules, ProcurementResult,
                         ReconciliationReport, VendorMatchResult)


class GraphInput(TypedDict):
    text: str
    today: date
    invoice_id: Optional[str]
    request_id: str


class BranchError(TypedDict):
    branch: str  # "policy" | "vendor"
    error_type: str
    message: str


class GraphOutput(TypedDict, total=False):
    request: Optional[EventRequest]
    missing_fields: list[str]
    clarification_questions: list[str]
    policy: Optional[PolicyReport]
    vendors: Optional[VendorMatchResult]
    procurement: Optional[ProcurementResult]
    reconciliation: Optional[ReconciliationReport]
    critic: Optional[CriticReport]
    final_status: str
    approver: str
    human_approval_required: bool
    human_note: Optional[str]
    audit: Annotated[list[AuditEntry], operator.add]
    branch_errors: Annotated[list[BranchError], operator.add]


class WorkflowState(GraphInput, GraphOutput, total=False):
    rules: Optional[PolicyRules]  # internal: loaded once after the clarification gate
    policy_blocked: bool          # internal: deterministic policy gate computed at the join
