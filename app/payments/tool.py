"""Narrow agent tool ``create_simulated_payment`` — SIMULATION ONLY, no money is ever transferred.

The tool accepts only references (ticket, invoice, PO) and an idempotency key. The actor is injected by the
backend (never supplied by an LLM); the amount and currency come from the canonical eligibility result; there
is no url / provider / account / mode input. It calls the same deterministic service as the API, so the
backend eligibility gate and role checks always apply. It is not wired to any LLM toolset (the Critic stays
read-only).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.payments import service
from app.payments.schemas import PaymentSimulationIn, PaymentSimulationOut
from app.portal_schemas import UserPublic

TOOL_NAME = "create_simulated_payment"


class CreateSimulatedPaymentToolInput(BaseModel):
    model_config = {"extra": "forbid"}

    ticket_id: str = Field(pattern=r"^REQ-[A-Za-z0-9-]{1,64}$")
    invoice_id: str = Field(pattern=r"^INV-[A-Za-z0-9-]{1,32}$")
    purchase_order_id: str = Field(pattern=r"^DRAFT-PO-[A-Za-z0-9-]{1,64}$")
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


def create_simulated_payment(tool_input: CreateSimulatedPaymentToolInput, *, actor: UserPublic,
                             confirmed_by_user: bool) -> PaymentSimulationOut:
    """Run one simulated payment through the backend gate. Raises ``service.PaymentServiceError`` on any block."""
    if not isinstance(tool_input, CreateSimulatedPaymentToolInput):
        tool_input = CreateSimulatedPaymentToolInput.model_validate(tool_input)
    if not isinstance(actor, UserPublic):
        raise service.PaymentServiceError(403, "CALLER_NOT_AUTHORIZED", "An authenticated backend actor is required.")
    if confirmed_by_user is not True:
        raise service.PaymentServiceError(422, "CONFIRMATION_REQUIRED",
                                          "Explicit user confirmation is required before a simulated payment.")
    eligibility = service.get_eligibility(tool_input.ticket_id, tool_input.invoice_id, actor)
    if eligibility.amount is None or eligibility.currency is None:
        raise service.PaymentServiceError(422, "PAYMENT_NOT_ELIGIBLE",
                                          "The invoice is not eligible for payment simulation.", eligibility.reasons)
    body = PaymentSimulationIn(ticket_id=tool_input.ticket_id, invoice_id=tool_input.invoice_id,
                               purchase_order_id=tool_input.purchase_order_id, amount=eligibility.amount,
                               currency=eligibility.currency, simulation_only=True, confirmed_by_user=True,
                               idempotency_key=tool_input.idempotency_key)
    result, _status = service.create_simulation(body, actor, correlation_id=None)
    return result
