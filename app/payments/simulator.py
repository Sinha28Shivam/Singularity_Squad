"""Local, deterministic payment SIMULATOR. No network, no provider SDK, no randomness, no money moved.

Outcomes depend only on (payment_id, attempt, profile), so tests and demos are reproducible.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.payments.config import VALID_PROFILES

FAILURE_PROVIDER_UNAVAILABLE = "SIMULATED_PROVIDER_UNAVAILABLE"


@dataclass(frozen=True)
class SimulatorOutcome:
    status: str
    provider_reference: Optional[str]
    failure_code: Optional[str]


def provider_reference(payment_id: str, attempt: int) -> str:
    return "PAY-SIM-" + hashlib.sha256(f"{payment_id}:{attempt}".encode("utf-8")).hexdigest()[:6].upper()


def simulate(payment_id: str, amount: Decimal, currency: str, attempt: int, profile: str) -> SimulatorOutcome:
    """Return the fictional simulator outcome. Raises ValueError on invalid input (never a silent success)."""
    if not isinstance(payment_id, str) or not payment_id:
        raise ValueError("payment_id is required")
    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be a positive finite Decimal")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha() or not currency.isupper():
        raise ValueError("currency must be a 3-letter ISO code")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be an integer >= 1")
    if profile not in VALID_PROFILES:
        raise ValueError("unknown simulator profile")

    if profile == "processing":
        return SimulatorOutcome("processing_simulated", provider_reference(payment_id, attempt), None)
    if profile == "transient_failure" and attempt == 1:
        return SimulatorOutcome("failed_simulated", None, FAILURE_PROVIDER_UNAVAILABLE)
    return SimulatorOutcome("accepted_by_simulator", provider_reference(payment_id, attempt), None)
