"""Dummy payment initiator service — SIMULATION ONLY. No bank, gateway or network host is ever contacted and
no money is ever transferred.

Flow (see Docs/WORKPLACEOPS_DUMMY_PAYMENT_INITIATOR_IMPLEMENTATION.md):
config gate -> role gate -> idempotency lookup -> eligibility re-check -> unique insert -> local simulator ->
compare-and-set status -> audit events (outbox on the record, delivered to ``payment_simulation_events`` with
deterministic event ids) -> ``audit_complete``. Any audit write failure leaves ``audit_complete=False`` and the
caller gets 503 AUDIT_UNAVAILABLE (never a success); ``reconcile_audit`` re-delivers the outbox idempotently.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from app.db.errors import PersistenceError
from app.payments import metrics
from app.payments.config import PaymentSettings, load_settings
from app.payments.eligibility import EligibilityDecision, evaluate_eligibility
from app.payments.schemas import (
    SIMULATION_MODE,
    EligibilityReason,
    PaymentEligibility,
    PaymentError,
    PaymentEvent,
    PaymentSimulation,
    PaymentSimulationIn,
    PaymentSimulationOut,
)
from app.payments.simulator import simulate
from app.portal_schemas import UserPublic
from app.repositories import payment_repository as repo

logger = logging.getLogger(__name__)

AUDIT_AGENT = "Payments (simulation)"
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
STATUS_MESSAGES = {
    "submitted_to_simulator": "Submitted to the simulator. No money was transferred.",
    "accepted_by_simulator": "Simulation accepted. No money was transferred.",
    "processing_simulated": "Simulated processing. No money was transferred.",
    "failed_simulated": "Simulation failed (retryable). No money was transferred.",
}
_AUDIT_INCOMPLETE = ("The simulated payment was recorded but its audit trail is incomplete; an admin must "
                     "reconcile it. No money was transferred.")


class PaymentServiceError(Exception):
    """Safe, structured error. The body is a ``PaymentError`` (always mode=SIMULATION, money_transferred=false)."""

    def __init__(self, status_code: int, error: str, message: str,
                 reasons: Optional[list[EligibilityReason]] = None, payment_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message
        self.reasons = list(reasons or [])
        self.payment_id = payment_id

    def body(self) -> dict:
        data = PaymentError(error=self.error, message=self.message, reasons=self.reasons).model_dump(mode="json")
        if self.payment_id:
            data["payment_id"] = self.payment_id
        return data


# ---------- helpers ----------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def idempotency_key_hash(username: str, key: str) -> str:
    return _sha(f"{username}|{key}")


def request_fingerprint(ticket_id: str, invoice_id: str, po_id: str, amount: str, currency: str) -> str:
    return _sha(json.dumps({"ticket_id": ticket_id, "invoice_id": invoice_id, "po_id": po_id, "amount": amount,
                            "currency": currency}, sort_keys=True, separators=(",", ":")))


def intent_key(ticket_id: str, invoice_id: str) -> str:
    return f"{ticket_id}|{invoice_id}"


def correlation_id_or_new(value: Optional[str]) -> str:
    return value if value and _CORRELATION_RE.match(value) else f"corr-{uuid.uuid4().hex[:16]}"


def _reason(code: str, message: str) -> EligibilityReason:
    return EligibilityReason(code=code, message=message)


def _log(event: str, **fields: Any) -> None:
    """Structured key=value log line. Only ids/codes/statuses — never secrets, invoices or vendor data."""
    parts = " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
    logger.info("payment_simulation event=%s mode=%s money_transferred=false %s", event, SIMULATION_MODE, parts)


def _settings_or_disabled() -> PaymentSettings:
    try:
        settings = load_settings()
    except Exception as exc:  # noqa: BLE001 - unreadable config => fail closed
        logger.error("Payment settings unreadable (%s)", type(exc).__name__)
        settings = None
    if settings is None or not settings.available:
        metrics.inc_blocked(["SIMULATION_DISABLED"])
        raise PaymentServiceError(503, "SIMULATION_DISABLED",
                                  "Payment simulation is disabled by server configuration.",
                                  [_reason("SIMULATION_DISABLED",
                                           "Payment simulation is disabled by server configuration.")])
    return settings


def _require_initiator(actor: UserPublic, settings: PaymentSettings) -> None:
    if actor.role == "user" or actor.role not in settings.initiator_roles:
        metrics.inc_blocked(["CALLER_NOT_AUTHORIZED"])
        msg = "You are not authorized to initiate a simulated payment."
        raise PaymentServiceError(403, "CALLER_NOT_AUTHORIZED", msg, [_reason("CALLER_NOT_AUTHORIZED", msg)])


def _audit_unavailable(payment_id: Optional[str] = None, message: Optional[str] = None) -> PaymentServiceError:
    msg = message or "The payment audit store is unavailable; nothing was simulated. Please try again later."
    return PaymentServiceError(503, "AUDIT_UNAVAILABLE", msg, [_reason("AUDIT_UNAVAILABLE", msg)],
                               payment_id=payment_id)


def _event(payment: dict, event_type: str, actor: str, at: datetime, *, attempt: int, status: Optional[str] = None,
           detail: Optional[str] = None, reason_codes: Optional[list[str]] = None, seq: int = 0,
           deterministic: bool = True) -> dict:
    pid = payment["payment_id"]
    event_id = (f"{pid}:a{attempt}:{event_type}" if deterministic
                else f"{pid}:{event_type}:{uuid.uuid4().hex[:12]}")
    return {"event_id": event_id, "payment_id": pid, "ticket_id": payment["ticket_id"],
            "invoice_id": payment["invoice_id"], "event_type": event_type, "status": status, "actor": actor,
            "at": at, "seq": seq, "attempt": attempt, "reason_codes": list(reason_codes or []), "detail": detail,
            "correlation_id": payment.get("correlation_id"), "mode": SIMULATION_MODE, "money_transferred": False}


def _ticket_audit_item(payment: dict, attempt: int, status: str, actor: str) -> dict:
    """Outbox item for one summary line on the ticket audit trail (agent_logs)."""
    pid = payment["payment_id"]
    detail = (f"Simulated payment {pid} (attempt {attempt}) for invoice {payment['invoice_id']} / "
              f"{payment['purchase_order_id']}: {status} by the local simulator, {payment['amount']} "
              f"{payment['currency']}, initiated by {actor}. SIMULATION ONLY - no money was transferred.")
    return {"event_id": f"{pid}:a{attempt}:ticket_audit", "kind": "ticket_audit", "ticket_id": payment["ticket_id"],
            "action": f"payment_{status}", "status": "warn" if status == "failed_simulated" else "info",
            "detail": detail}


def _status_events(payment: dict, new_status: str, outcome, actor: str, attempt: int, at: datetime,
                   seq_start: int) -> list[dict]:
    events = [_event(payment, "payment_simulation_status_changed", actor, at, attempt=attempt, status=new_status,
                     detail=f"submitted_to_simulator -> {new_status}. No money was transferred.", seq=seq_start)]
    if new_status == "failed_simulated":
        events.append(_event(payment, "payment_simulation_failed", actor, at, attempt=attempt, status=new_status,
                             reason_codes=[outcome.failure_code] if outcome.failure_code else [],
                             detail="Simulated transient failure; retry is allowed.", seq=seq_start + 1))
    events.append(_ticket_audit_item(payment, attempt, new_status, actor))
    return events


def _write_ticket_audit(item: dict) -> None:
    from app.db.errors import AuditConflictError
    from app.repositories.audit_repository import append_audit_entries, get_audit_entries, get_last_sequence
    from app.schemas import AuditEntry

    # Idempotent: the detail embeds the payment id + attempt, so an already-written line is skipped.
    if any(e.get("detail") == item["detail"] for e in get_audit_entries(item["ticket_id"])):
        return
    for attempt in range(5):
        seq = get_last_sequence(item["ticket_id"]) + 1
        try:
            append_audit_entries(item["ticket_id"], [AuditEntry(step=seq, agent=AUDIT_AGENT, action=item["action"],
                                                                status=item["status"], detail=item["detail"])])
            return
        except AuditConflictError:
            if attempt == 4:
                raise
            time.sleep(0.005 * (attempt + 1))


def _deliver_outbox(payment_id: str) -> bool:
    """Write every pending outbox event (idempotently). Returns True when the audit trail is complete."""
    record = repo.get_by_payment_id(payment_id)
    if record is None:
        return False
    delivered: list[str] = []
    ok = True
    for item in record.get("outbox") or []:
        try:
            if item.get("kind") == "ticket_audit":
                _write_ticket_audit(item)
            else:
                ev = {k: v for k, v in item.items() if k != "kind"}
                repo.insert_event(ev)
            delivered.append(item["event_id"])
        except Exception as exc:  # noqa: BLE001 - any audit failure keeps the outbox for reconciliation
            ok = False
            logger.error("payment_simulation audit write failed payment_id=%s event_id=%s (%s)", payment_id,
                         item.get("event_id"), type(exc).__name__)
    try:
        complete = repo.clear_outbox(payment_id, delivered)
    except PersistenceError as exc:
        logger.error("payment_simulation outbox update failed payment_id=%s (%s)", payment_id, type(exc).__name__)
        return False
    return ok and complete


def _public(record: dict) -> PaymentSimulation:
    data = {k: v for k, v in record.items() if k in PaymentSimulation.model_fields}
    data["mode"] = SIMULATION_MODE
    data["money_transferred"] = False
    data["message"] = STATUS_MESSAGES.get(record.get("status"), STATUS_MESSAGES["submitted_to_simulator"])
    return PaymentSimulation(**data)


def _out(record: dict, *, replayed: bool = False) -> PaymentSimulationOut:
    payment = _public(record)
    message = payment.message
    if replayed:
        message = "This simulated payment was already requested; returning the stored result. " + message
    return PaymentSimulationOut(payment=payment, replayed=replayed, message=message)


def _record_blocked(body_ticket: str, invoice_id: str, actor: UserPublic, codes: list[str],
                    correlation_id: str) -> None:
    """Best-effort audit of a blocked request (no payment record exists, payment_id is None)."""
    try:
        repo.insert_event({
            "event_id": f"blocked:{uuid.uuid4().hex}", "payment_id": None, "ticket_id": body_ticket,
            "invoice_id": invoice_id, "event_type": "payment_simulation_blocked", "status": None,
            "actor": actor.username, "at": _now(), "seq": 0, "attempt": 0, "reason_codes": codes,
            "detail": "Payment simulation blocked by the eligibility gate. Nothing was simulated.",
            "correlation_id": correlation_id})
    except Exception as exc:  # noqa: BLE001
        logger.warning("payment_simulation blocked-event write failed (%s)", type(exc).__name__)


def _evaluate(ticket_id: str, invoice_id: str, actor: UserPublic, settings: Optional[PaymentSettings], **kw
              ) -> EligibilityDecision:
    try:
        return evaluate_eligibility(ticket_id, invoice_id, actor, settings=settings, **kw)
    except PersistenceError:
        raise _audit_unavailable(message="Eligibility could not be verified (records unavailable); nothing was "
                                         "simulated. Please try again later.") from None


# ---------- public API ----------
def get_eligibility(ticket_id: str, invoice_id: str, actor: UserPublic) -> PaymentEligibility:
    decision = _evaluate(ticket_id, invoice_id, actor, None)
    view = decision.view
    try:
        existing = repo.get_by_intent_key(intent_key(ticket_id, invoice_id))
    except PersistenceError:
        raise _audit_unavailable() from None
    if existing:
        reasons = list(view.reasons) + [_reason("ALREADY_SIMULATED",
                                                "A simulated payment already exists for this invoice.")]
        view = view.model_copy(update={"existing_payment_id": existing["payment_id"], "eligible": False,
                                       "reasons": reasons})
    return view


def _replay_or_conflict(existing: dict, fingerprint: str, actor: UserPublic, correlation_id: str
                        ) -> tuple[PaymentSimulationOut, int]:
    if existing.get("request_fingerprint") != fingerprint:
        metrics.inc_blocked(["IDEMPOTENCY_CONFLICT"])
        _log("idempotency_conflict", payment_id=existing.get("payment_id"), correlation_id=correlation_id)
        raise PaymentServiceError(409, "IDEMPOTENCY_CONFLICT",
                                  "This idempotency key was already used for a different payment request.",
                                  payment_id=existing.get("payment_id"))
    pid = existing["payment_id"]
    metrics.inc(metrics.REPLAYS)
    try:
        repo.insert_event(_event(existing, "payment_simulation_idempotent_replay", actor.username, _now(),
                                 attempt=int(existing.get("attempts") or 1), status=existing.get("status"),
                                 detail="Duplicate request; stored result returned.", deterministic=False))
    except Exception as exc:  # noqa: BLE001 - the replay itself is safe; the stored result is unchanged
        logger.warning("payment_simulation replay-event write failed payment_id=%s (%s)", pid, type(exc).__name__)
    if not existing.get("audit_complete", True) and not _deliver_outbox(pid):
        metrics.inc(metrics.RECONCILIATION_FAILURES)
        raise _audit_unavailable(pid, _AUDIT_INCOMPLETE)
    record = repo.get_by_payment_id(pid) or existing
    _log("idempotent_replay", payment_id=pid, ticket_id=record.get("ticket_id"), correlation_id=correlation_id)
    return _out(record, replayed=True), 200


def create_simulation(body: PaymentSimulationIn, actor: UserPublic, correlation_id: Optional[str] = None
                      ) -> tuple[PaymentSimulationOut, int]:
    """Create one simulated payment. Returns (result, 201) or (stored result, 200) on an idempotent replay."""
    started = time.perf_counter()
    correlation_id = correlation_id_or_new(correlation_id)
    metrics.inc(metrics.REQUESTS)
    try:
        return _create(body, actor, correlation_id)
    except PersistenceError as exc:
        logger.error("payment_simulation persistence failure (%s)", type(exc).__name__)
        raise _audit_unavailable() from None
    finally:
        metrics.observe_latency((time.perf_counter() - started) * 1000.0)


def _create(body: PaymentSimulationIn, actor: UserPublic, correlation_id: str) -> tuple[PaymentSimulationOut, int]:
    settings = _settings_or_disabled()
    _require_initiator(actor, settings)

    key_hash = idempotency_key_hash(actor.username, body.idempotency_key)
    fingerprint = request_fingerprint(body.ticket_id, body.invoice_id, body.purchase_order_id, body.amount,
                                      body.currency)
    existing = repo.get_by_idempotency_hash(key_hash)
    if existing:
        return _replay_or_conflict(existing, fingerprint, actor, correlation_id)

    decision = _evaluate(body.ticket_id, body.invoice_id, actor, settings, purchase_order_id=body.purchase_order_id,
                         amount=body.amount, currency=body.currency)
    if not decision.eligible:
        codes = [r.code for r in decision.view.reasons]
        metrics.inc_blocked(codes)
        _record_blocked(body.ticket_id, body.invoice_id, actor, codes, correlation_id)
        _log("blocked", ticket_id=body.ticket_id, invoice_id=body.invoice_id, reasons=",".join(codes),
             correlation_id=correlation_id)
        raise PaymentServiceError(422, "PAYMENT_NOT_ELIGIBLE",
                                  "The invoice is not eligible for payment simulation.", decision.view.reasons)

    intent = intent_key(body.ticket_id, body.invoice_id)
    prior = repo.get_by_intent_key(intent)
    if prior:
        raise _already_simulated(prior)

    view = decision.view
    now = _now()
    pid = f"psim_{uuid.uuid4().hex[:20]}"
    record: dict[str, Any] = {
        "payment_id": pid, "provider_reference": None, "ticket_id": body.ticket_id, "invoice_id": body.invoice_id,
        "purchase_order_id": view.purchase_order_id, "amount": view.amount, "currency": view.currency,
        "method": "SIMULATED_NEFT", "status": "submitted_to_simulator", "failure_code": None, "attempts": 1,
        "approval_id": view.approval_id, "approval_request_version": view.approval_request_version,
        "policy_set_version": view.policy_set_version, "eligibility_fingerprint": view.eligibility_fingerprint,
        "request_fingerprint": fingerprint, "idempotency_key_hash": key_hash, "intent_key": intent,
        "simulator_profile": settings.profile, "correlation_id": correlation_id, "created_by": actor.username,
        "created_at": now, "updated_at": now, "audit_complete": False,
        "message": STATUS_MESSAGES["submitted_to_simulator"], "schema_version": 1,
    }
    record["outbox"] = [
        _event(record, "payment_simulation_requested", actor.username, now, attempt=1, seq=1,
               detail="Simulated payment requested after explicit confirmation."),
        _event(record, "payment_simulation_eligibility_passed", actor.username, now, attempt=1, seq=2,
               detail=f"Eligibility re-checked ({view.eligibility_fingerprint})."),
        _event(record, "payment_simulation_created", actor.username, now, attempt=1, seq=3,
               status="submitted_to_simulator", detail="Simulation record created. No money was transferred."),
    ]
    PaymentSimulation(**{k: v for k, v in record.items() if k in PaymentSimulation.model_fields})  # validate
    try:
        repo.insert_simulation(record)
    except repo.PaymentDuplicateError:
        again = repo.get_by_idempotency_hash(key_hash)
        if again:
            return _replay_or_conflict(again, fingerprint, actor, correlation_id)
        prior = repo.get_by_intent_key(intent)
        if prior:
            raise _already_simulated(prior) from None
        raise PaymentServiceError(409, "ALREADY_SIMULATED", "A simulated payment for this request already exists."
                                  ) from None
    metrics.inc(metrics.CREATED)
    _log("created", payment_id=pid, ticket_id=body.ticket_id, invoice_id=body.invoice_id,
         approval_id=view.approval_id, correlation_id=correlation_id)
    final = _run_simulator(record, actor, attempt=1, amount=decision.canonical_amount, profile=settings.profile)
    return _out(final), 201


def _already_simulated(prior: dict) -> PaymentServiceError:
    metrics.inc_blocked(["ALREADY_SIMULATED"])
    msg = "A simulated payment already exists for this ticket and invoice."
    return PaymentServiceError(409, "ALREADY_SIMULATED", msg, [_reason("ALREADY_SIMULATED", msg)],
                               payment_id=prior.get("payment_id"))


def _run_simulator(record: dict, actor: UserPublic, *, attempt: int, amount: Optional[Decimal], profile: str
                   ) -> dict:
    pid = record["payment_id"]
    if amount is None:
        amount = Decimal(record["amount"])
    outcome = simulate(pid, amount, record["currency"], attempt, profile)
    now = _now()
    events = _status_events(record, outcome.status, outcome, actor.username, attempt, now, seq_start=10 * attempt)
    updated = repo.transition_status(
        pid, "submitted_to_simulator", outcome.status, now,
        set_fields={"provider_reference": outcome.provider_reference, "failure_code": outcome.failure_code,
                    "message": STATUS_MESSAGES[outcome.status]},
        push_outbox=events)
    if updated is None:  # a concurrent writer moved it; return what is stored
        updated = repo.get_by_payment_id(pid) or record
    if outcome.status == "failed_simulated":
        metrics.inc(metrics.FAILURES)
    _log("status_changed", payment_id=pid, status=outcome.status, attempt=attempt,
         provider_reference=outcome.provider_reference, failure_code=outcome.failure_code)
    if not _deliver_outbox(pid):
        metrics.inc(metrics.RECONCILIATION_FAILURES)
        raise _audit_unavailable(pid, _AUDIT_INCOMPLETE)
    return repo.get_by_payment_id(pid) or updated


def retry(payment_id: str, actor: UserPublic, correlation_id: Optional[str] = None) -> PaymentSimulationOut:
    """Retry a ``failed_simulated`` payment: same payment_id / idempotency key, attempts+1, never a new record."""
    started = time.perf_counter()
    try:
        return _retry(payment_id, actor, correlation_id_or_new(correlation_id))
    except PersistenceError as exc:
        logger.error("payment_simulation persistence failure on retry (%s)", type(exc).__name__)
        raise _audit_unavailable(payment_id) from None
    finally:
        metrics.observe_latency((time.perf_counter() - started) * 1000.0)


def _retry(payment_id: str, actor: UserPublic, correlation_id: str) -> PaymentSimulationOut:
    settings = _settings_or_disabled()
    _require_initiator(actor, settings)
    record = repo.get_by_payment_id(payment_id)
    if record is None:
        raise PaymentServiceError(404, "PAYMENT_NOT_FOUND", "Simulated payment not found.")
    if record.get("status") != "failed_simulated":
        raise PaymentServiceError(409, "ILLEGAL_TRANSITION",
                                  f"Only a failed simulation can be retried (current status: {record.get('status')}).",
                                  payment_id=payment_id)
    decision = _evaluate(record["ticket_id"], record["invoice_id"], actor, settings,
                         purchase_order_id=record["purchase_order_id"], amount=record["amount"],
                         currency=record["currency"])
    reasons = list(decision.view.reasons)
    if decision.eligible and decision.view.eligibility_fingerprint != record.get("eligibility_fingerprint"):
        reasons.append(_reason("APPROVAL_STALE", "The approved facts changed since the original request."))
    if reasons:
        codes = [r.code for r in reasons]
        metrics.inc_blocked(codes)
        _record_blocked(record["ticket_id"], record["invoice_id"], actor, codes, correlation_id)
        raise PaymentServiceError(422, "PAYMENT_NOT_ELIGIBLE", "The invoice is no longer eligible for payment "
                                  "simulation.", reasons, payment_id=payment_id)
    attempt = int(record.get("attempts") or 1) + 1
    now = _now()
    retry_event = _event(record, "payment_simulation_retry_requested", actor.username, now, attempt=attempt,
                         seq=10 * attempt - 1, status="submitted_to_simulator",
                         detail=f"Retry requested (attempt {attempt}). Same payment, same idempotency key.")
    updated = repo.transition_status(payment_id, "failed_simulated", "submitted_to_simulator", now,
                                     set_fields={"failure_code": None, "correlation_id": correlation_id,
                                                 "message": STATUS_MESSAGES["submitted_to_simulator"]},
                                     inc_fields={"attempts": 1}, push_outbox=[retry_event])
    if updated is None:
        raise PaymentServiceError(409, "ILLEGAL_TRANSITION", "The payment was retried concurrently.",
                                  payment_id=payment_id)
    metrics.inc(metrics.RETRIES)
    _log("retry_requested", payment_id=payment_id, attempt=attempt, correlation_id=correlation_id)
    final = _run_simulator(updated, actor, attempt=attempt, amount=decision.canonical_amount,
                           profile=settings.profile)
    return _out(final)


def reconcile_audit(payment_id: str) -> PaymentSimulation:
    """Re-deliver missing audit events (idempotent). Raises 503 AUDIT_UNAVAILABLE if still incomplete."""
    try:
        record = repo.get_by_payment_id(payment_id)
        if record is None:
            raise PaymentServiceError(404, "PAYMENT_NOT_FOUND", "Simulated payment not found.")
        if not record.get("audit_complete") and not _deliver_outbox(payment_id):
            metrics.inc(metrics.RECONCILIATION_FAILURES)
            raise _audit_unavailable(payment_id, _AUDIT_INCOMPLETE)
        record = repo.get_by_payment_id(payment_id) or record
    except PersistenceError:
        metrics.inc(metrics.RECONCILIATION_FAILURES)
        raise _audit_unavailable(payment_id) from None
    _log("audit_reconciled", payment_id=payment_id)
    return _public(record)


def get_payment(payment_id: str) -> PaymentSimulation:
    """Read a payment; best-effort reconciliation of a pending audit outbox first."""
    try:
        record = repo.get_by_payment_id(payment_id)
        if record is None:
            raise PaymentServiceError(404, "PAYMENT_NOT_FOUND", "Simulated payment not found.")
        if not record.get("audit_complete"):
            if _deliver_outbox(payment_id):
                record = repo.get_by_payment_id(payment_id) or record
            else:
                metrics.inc(metrics.RECONCILIATION_FAILURES)
    except PersistenceError:
        raise _audit_unavailable(payment_id, "Simulated payment records are unavailable.") from None
    return _public(record)


def list_events(payment_id: str) -> list[PaymentEvent]:
    try:
        if repo.get_by_payment_id(payment_id) is None:
            raise PaymentServiceError(404, "PAYMENT_NOT_FOUND", "Simulated payment not found.")
        docs = repo.list_events(payment_id)
    except PersistenceError:
        raise _audit_unavailable(payment_id, "Simulated payment records are unavailable.") from None
    return [PaymentEvent(**{k: v for k, v in d.items() if k in PaymentEvent.model_fields}) for d in docs]


def list_payments(ticket_id: Optional[str] = None, limit: int = 100) -> list[PaymentSimulation]:
    try:
        docs = repo.list_simulations(ticket_id, limit)
    except PersistenceError:
        raise _audit_unavailable(message="Simulated payment records are unavailable.") from None
    return [_public(d) for d in docs]
