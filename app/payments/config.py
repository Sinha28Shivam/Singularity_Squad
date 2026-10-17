"""Server-side configuration for the payment SIMULATOR. There is no real-payment mode.

Fails closed: an unset/unknown ``PAYMENT_MODE``, an invalid value, a production environment or simulation not
explicitly enabled all make the simulator unavailable. Settings come only from the server environment, never
from request data. Error messages never echo secrets (only the variable names and short value summaries).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

VALID_MODE = "simulation"
VALID_PROFILES = frozenset({"normal", "transient_failure", "processing"})
ALLOWED_INITIATOR_ROLES = frozenset({"admin", "approver"})   # "user" is never allowed
DEFAULT_CURRENCIES = "INR"
DEFAULT_PROFILE = "normal"
DEFAULT_INITIATOR_ROLES = "admin"
PRODUCTION_ENVS = frozenset({"prod", "production"})
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


@dataclass(frozen=True)
class PaymentSettings:
    mode: Optional[str]
    enabled: bool
    allowed_currencies: frozenset[str]
    profile: str
    initiator_roles: frozenset[str]
    errors: tuple[str, ...]

    @property
    def available(self) -> bool:
        return self.mode == VALID_MODE and self.enabled and not self.errors


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return None if value is None else value.strip()


def load_settings() -> PaymentSettings:
    """Read and validate the payment-simulation environment (on every call; cheap and test-friendly)."""
    errors: list[str] = []

    raw_mode = _env("PAYMENT_MODE")
    mode = raw_mode.lower() if raw_mode else None
    if mode is None:
        errors.append("PAYMENT_MODE is not set (only 'simulation' is supported)")
    elif mode != VALID_MODE:
        errors.append("PAYMENT_MODE is invalid (only 'simulation' is supported)")
        mode = None

    raw_enabled = (_env("PAYMENT_SIMULATION_ENABLED") or "").lower()
    if raw_enabled in _TRUE:
        enabled = True
    else:
        enabled = False
        if raw_enabled not in _FALSE:
            errors.append("PAYMENT_SIMULATION_ENABLED is invalid (use true/false)")

    raw_cur = _env("PAYMENT_ALLOWED_CURRENCIES")
    currencies = [c.strip().upper() for c in (raw_cur if raw_cur is not None else DEFAULT_CURRENCIES).split(",")
                  if c.strip()]
    if not currencies:
        errors.append("PAYMENT_ALLOWED_CURRENCIES is empty")
    bad = [c for c in currencies if not _CURRENCY_RE.match(c)]
    if bad:
        errors.append("PAYMENT_ALLOWED_CURRENCIES contains invalid currency codes")
    allowed = frozenset(c for c in currencies if _CURRENCY_RE.match(c))

    profile = (_env("PAYMENT_SIMULATOR_PROFILE") or DEFAULT_PROFILE).lower()
    if profile not in VALID_PROFILES:
        errors.append("PAYMENT_SIMULATOR_PROFILE is invalid (normal|transient_failure|processing)")
        profile = DEFAULT_PROFILE

    raw_roles = _env("PAYMENT_INITIATOR_ROLES")
    roles = {r.strip().lower() for r in (raw_roles if raw_roles else DEFAULT_INITIATOR_ROLES).split(",")
             if r.strip()}
    if "user" in roles:
        errors.append("PAYMENT_INITIATOR_ROLES must not include 'user'")
    if roles - ALLOWED_INITIATOR_ROLES - {"user"}:
        errors.append("PAYMENT_INITIATOR_ROLES contains unknown roles")
    initiator_roles = frozenset(roles & ALLOWED_INITIATOR_ROLES)
    if not initiator_roles:
        errors.append("PAYMENT_INITIATOR_ROLES has no valid role")

    for var in ("APP_ENV", "ENVIRONMENT"):
        if (_env(var) or "").lower() in PRODUCTION_ENVS:
            errors.append("simulation disabled in production")
            break

    return PaymentSettings(mode=mode, enabled=enabled, allowed_currencies=allowed, profile=profile,
                           initiator_roles=initiator_roles, errors=tuple(errors))


def validate_at_startup() -> PaymentSettings:
    """Parse settings at startup and log problems (never values/secrets). Never raises."""
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - startup must never crash on payment config
        logger.error("Payment simulation configuration could not be read (%s); simulation unavailable",
                     type(exc).__name__)
        return PaymentSettings(mode=None, enabled=False, allowed_currencies=frozenset(), profile=DEFAULT_PROFILE,
                               initiator_roles=frozenset(), errors=("configuration unreadable",))
    if settings.errors:
        for err in settings.errors:
            logger.warning("Payment simulation config: %s", err)
        logger.warning("Payment simulation is UNAVAILABLE (fail closed). No real-payment mode exists.")
    elif not settings.enabled:
        logger.info("Payment simulation is disabled (PAYMENT_SIMULATION_ENABLED not true).")
    else:
        logger.info("Payment simulation enabled: SIMULATION ONLY, profile=%s, currencies=%s. "
                    "No money is ever transferred.", settings.profile, ",".join(sorted(settings.allowed_currencies)))
    return settings
