"""Dummy Payment Initiator contracts — SIMULATION ONLY. No money is ever transferred.

Every record, response and event carries ``mode="SIMULATION"`` and ``money_transferred=False``. Those
fields are server constants: the client can never set them (request models forbid extra fields and the
response models pin them with ``Literal`` types).
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

SIMULATION_DISCLAIMER = "SIMULATED — NO MONEY TRANSFERRED"
SIMULATION_MODE = "SIMULATION"

PaymentStatus = Literal[
    "submitted_to_simulator",   # record created, simulator called
    "accepted_by_simulator",    # simulator accepted the instruction (fictional)
    "processing_simulated",     # simulator reports async processing (fictional)
    "failed_simulated",         # simulator transient failure (retryable)
]
TERMINAL_PAYMENT_STATUSES = {"accepted_by_simulator"}
RETRYABLE_PAYMENT_STATUSES = {"failed_simulated"}
ALLOWED_TRANSITIONS: dict[Optional[str], set[str]] = {
    None: {"submitted_to_simulator"},
    "submitted_to_simulator": {"accepted_by_simulator", "processing_simulated", "failed_simulated"},
    "processing_simulated": {"accepted_by_simulator", "failed_simulated"},
    "failed_simulated": {"submitted_to_simulator"},          # explicit retry only
    "accepted_by_simulator": set(),
}

ReasonCode = Literal[
    "INVOICE_NOT_FOUND", "PO_NOT_FOUND", "INVOICE_PO_MISMATCH", "INVOICE_MATCH_FAILED", "DUPLICATE_INVOICE",
    "POLICY_BLOCK", "CLARIFICATION_REQUIRED", "APPROVAL_MISSING", "APPROVAL_STALE", "AMOUNT_MISMATCH",
    "CURRENCY_NOT_ALLOWED", "CALLER_NOT_AUTHORIZED", "SIMULATION_DISABLED", "AUDIT_UNAVAILABLE",
    "TICKET_NOT_APPROVED", "RECONCILIATION_REQUIRED", "ALREADY_SIMULATED",
]
SimulatorProfile = Literal["normal", "transient_failure", "processing"]
SimulatedMethod = Literal["SIMULATED_NEFT"]


def parse_money(value) -> Decimal:
    """Strict Decimal parse: positive, finite, at most 2 decimal places. Floats are rejected."""
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("amount must be a decimal string, not a float")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValueError("amount must be a decimal number") from None
    if not d.is_finite():
        raise ValueError("amount must be finite")
    if d <= 0:
        raise ValueError("amount must be positive")
    if d.as_tuple().exponent < -2:
        raise ValueError("amount must have at most 2 decimal places")
    if d >= Decimal("1e12"):
        raise ValueError("amount is out of range")
    try:
        return d.quantize(Decimal("0.01"))
    except InvalidOperation:
        raise ValueError("amount is out of range") from None


def money_str(d: Decimal) -> str:
    return format(d.quantize(Decimal("0.01")), "f")


class PaymentSimulationIn(BaseModel):
    """POST /api/payments/simulation. Amount/currency are CHECKED against canonical records, never trusted."""
    model_config = {"extra": "forbid"}   # mode / money_transferred / provider / account fields are rejected

    ticket_id: str = Field(pattern=r"^REQ-[A-Za-z0-9-]{1,64}$")
    invoice_id: str = Field(pattern=r"^INV-[A-Za-z0-9-]{1,32}$")
    purchase_order_id: str = Field(pattern=r"^DRAFT-PO-[A-Za-z0-9-]{1,64}$")
    amount: str
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    simulation_only: Literal[True]
    confirmed_by_user: Literal[True]
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")

    @field_validator("amount")
    @classmethod
    def _amount(cls, v: str) -> str:
        return money_str(parse_money(v))


class PaymentRetryIn(BaseModel):
    model_config = {"extra": "forbid"}
    confirmed_by_user: Literal[True]


class EligibilityReason(BaseModel):
    code: ReasonCode
    message: str


class PaymentEligibility(BaseModel):
    mode: Literal["SIMULATION"] = SIMULATION_MODE
    money_transferred: Literal[False] = False
    disclaimer: str = SIMULATION_DISCLAIMER
    eligible: bool
    ticket_id: str
    invoice_id: Optional[str] = None
    purchase_order_id: Optional[str] = None
    amount: Optional[str] = None          # canonical (from the persisted reconciliation/PO), Decimal string
    currency: Optional[str] = None
    invoice_match_status: Optional[str] = None
    approval_id: Optional[str] = None
    approval_status: Optional[str] = None
    approved_by: Optional[str] = None
    approval_request_version: Optional[int] = None
    policy_set_version: Optional[int] = None
    reasons: list[EligibilityReason] = Field(default_factory=list)
    existing_payment_id: Optional[str] = None
    checked_at: dt.datetime
    eligibility_fingerprint: Optional[str] = None   # sha256 over the canonical facts approved


class PaymentEvent(BaseModel):
    event_id: str
    payment_id: Optional[str] = None
    event_type: str
    status: Optional[str] = None
    actor: str
    at: dt.datetime
    reason_codes: list[str] = Field(default_factory=list)
    detail: Optional[str] = None
    correlation_id: Optional[str] = None
    mode: Literal["SIMULATION"] = SIMULATION_MODE
    money_transferred: Literal[False] = False


class PaymentSimulation(BaseModel):
    mode: Literal["SIMULATION"] = SIMULATION_MODE
    money_transferred: Literal[False] = False
    disclaimer: str = SIMULATION_DISCLAIMER
    payment_id: str                         # "psim_<hex>"
    provider_reference: Optional[str] = None  # "PAY-SIM-<HEX>"
    ticket_id: str
    invoice_id: str
    purchase_order_id: str
    amount: str
    currency: str
    method: SimulatedMethod = "SIMULATED_NEFT"
    status: PaymentStatus
    failure_code: Optional[str] = None      # e.g. SIMULATED_PROVIDER_UNAVAILABLE
    attempts: int = 1
    approval_id: str
    approval_request_version: int
    policy_set_version: Optional[int] = None
    eligibility_fingerprint: str
    request_fingerprint: str
    idempotency_key_hash: str
    simulator_profile: SimulatorProfile
    correlation_id: str
    created_by: str
    created_at: dt.datetime
    updated_at: dt.datetime
    audit_complete: bool = True
    message: str = "Simulation accepted. No money was transferred."
    schema_version: int = 1


class PaymentSimulationOut(BaseModel):
    payment: PaymentSimulation
    replayed: bool = False
    mode: Literal["SIMULATION"] = SIMULATION_MODE
    money_transferred: Literal[False] = False
    message: str


class PaymentError(BaseModel):
    error: str
    message: str
    reasons: list[EligibilityReason] = Field(default_factory=list)
    mode: Literal["SIMULATION"] = SIMULATION_MODE
    money_transferred: Literal[False] = False
