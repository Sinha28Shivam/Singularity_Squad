"""Deterministic backend eligibility gate for a SIMULATED payment. No money is ever transferred.

Collects ALL failing reason codes (for the UI). Canonical amount/currency/PO come from persisted backend records
(the approved ticket's reconciliation + draft PO and the approval's review snapshot); client values are only
CHECKED against them, never trusted. Fails closed: if Mongo or the policy-set registry cannot be read,
``app.db.PersistenceError`` is raised (the service maps it to 503 AUDIT_UNAVAILABLE).
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.db.errors import PersistenceError
from app.payments.config import PaymentSettings, load_settings
from app.payments.schemas import EligibilityReason, PaymentEligibility, money_str, parse_money
from app.portal_schemas import UserPublic

logger = logging.getLogger(__name__)

_STALE_PO_FIELDS = ("po_id", "total", "currency", "line_items")
_CENT = Decimal("0.01")

MESSAGES = {
    "SIMULATION_DISABLED": "Payment simulation is disabled by server configuration.",
    "CALLER_NOT_AUTHORIZED": "You are not authorized to initiate a simulated payment for this invoice.",
    "TICKET_NOT_APPROVED": "The ticket is not in the approved state.",
    "INVOICE_NOT_FOUND": "The invoice was not found.",
    "RECONCILIATION_REQUIRED": "The invoice has not been reconciled on this ticket.",
    "INVOICE_PO_MISMATCH": "The invoice or purchase order does not match the ticket's reconciled records.",
    "DUPLICATE_INVOICE": "The invoice was flagged as a duplicate.",
    "INVOICE_MATCH_FAILED": "The invoice did not match the purchase order and delivery.",
    "PO_NOT_FOUND": "No draft purchase order exists for this ticket.",
    "POLICY_BLOCK": "A policy check blocks this request.",
    "CLARIFICATION_REQUIRED": "The request has unresolved clarifications.",
    "APPROVAL_MISSING": "No current human approval exists for this ticket.",
    "APPROVAL_STALE": "The approval no longer matches the current ticket, PO, invoice or policy set.",
    "AMOUNT_MISMATCH": "The amount does not match the canonical approved amount.",
    "CURRENCY_NOT_ALLOWED": "The currency is not allowed or does not match the canonical currency.",
}


@dataclass
class EligibilityDecision:
    view: PaymentEligibility
    canonical_amount: Optional[Decimal]
    approval: Optional[dict]
    ticket: Optional[dict]

    @property
    def eligible(self) -> bool:
        return self.view.eligible


class _Reasons:
    def __init__(self) -> None:
        self.items: list[EligibilityReason] = []

    def add(self, code: str, message: Optional[str] = None) -> None:
        if any(r.code == code for r in self.items):
            return
        self.items.append(EligibilityReason(code=code, message=message or MESSAGES[code]))

    def codes(self) -> list[str]:
        return [r.code for r in self.items]


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _to_money(value: Any) -> Optional[Decimal]:
    """Canonical stored number (float/int/str) -> 2-dp Decimal; None if missing/invalid/non-positive."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d <= 0:
        return None
    return d.quantize(_CENT)


def _canon(value: Any) -> Any:
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _canon_po(po: Optional[dict]) -> Optional[dict]:
    if not isinstance(po, dict):
        return None
    out = {k: po.get(k) for k in _STALE_PO_FIELDS}
    out["total"] = money_str(_to_money(out["total"])) if _to_money(out["total"]) is not None else out["total"]
    items = []
    for li in out.get("line_items") or []:
        li = dict(li) if isinstance(li, dict) else {"value": li}
        for k in ("quantity", "unit_price", "line_total"):
            if isinstance(li.get(k), (int, float)) and not isinstance(li.get(k), bool):
                li[k] = format(Decimal(str(li[k])).normalize(), "f")
        items.append(li)
    out["line_items"] = items
    return _canon(out)


def _known_invoice(invoice_id: str) -> bool:
    from app.data_loader import load_json
    return any(i.get("invoice_id") == invoice_id for i in load_json("invoices.json") if isinstance(i, dict))


def _previously_paid(invoice_id: str, vendor_id: Optional[str]) -> bool:
    from app.data_loader import load_json
    for h in load_json("invoice_history.json"):
        if isinstance(h, dict) and h.get("invoice_id") == invoice_id and (
                vendor_id is None or h.get("vendor_id") == vendor_id):
            return True
    return False


def _read(fn, *args):
    """Run a read; anything other than a clean result is a fail-closed PersistenceError."""
    try:
        return fn(*args)
    except PersistenceError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Payment eligibility read failed (%s)", type(exc).__name__)
        raise PersistenceError("Eligibility could not be verified. Please try again later.") from None


def _active_policy_set_version() -> int:
    from app.policy_rules.engine import active_version
    try:
        return int(active_version())
    except Exception as exc:  # noqa: BLE001 - never "not stale"
        logger.error("Active policy set unavailable (%s)", type(exc).__name__)
        raise PersistenceError("Policy set could not be verified. Please try again later.") from None


def _last_decision(approval: dict) -> dict:
    decisions = [d for d in approval.get("decisions") or [] if isinstance(d, dict)]
    return decisions[-1] if decisions else {}


def _policy_checks(ticket: dict, wr: dict, reasons: _Reasons) -> None:
    from app.approvals.policy import evaluate_approval_requirement
    from app.approvals.service import _policy_evaluation_for

    final_status = wr.get("final_status")
    if final_status == "blocked":
        reasons.add("POLICY_BLOCK")
    if final_status == "needs_clarification" or wr.get("missing_fields") or wr.get("clarification_questions"):
        reasons.add("CLARIFICATION_REQUIRED")
    evaluation = _read(_policy_evaluation_for, ticket)
    try:
        req = evaluate_approval_requirement(wr, policy_evaluation=evaluation)
    except PersistenceError:
        raise
    except Exception as exc:  # noqa: BLE001 - unverifiable policy => blocked, never "pass"
        logger.warning("Policy evaluation failed for payment eligibility (%s)", type(exc).__name__)
        reasons.add("POLICY_BLOCK", "Policy checks could not be verified for this ticket.")
        return
    if req.blocked and {"HARD_BLOCK", "POLICY_HARD_BLOCK"} & set(req.reason_codes):
        reasons.add("POLICY_BLOCK")
    if req.needs_clarification:
        reasons.add("CLARIFICATION_REQUIRED")


def _fingerprint(facts: dict) -> str:
    payload = json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate_eligibility(ticket_id: str, invoice_id: str, actor: UserPublic, *,
                         purchase_order_id: Optional[str] = None, amount: Optional[str] = None,
                         currency: Optional[str] = None, settings: Optional[PaymentSettings] = None,
                         now: Optional[datetime] = None) -> EligibilityDecision:
    """Evaluate every eligibility condition and return the full decision (all failing reasons)."""
    from app.approvals.service import bound_policy_set_version
    from app.repositories.approval_repository import get_current_for_ticket
    from app.repositories.ticket_repository import get_ticket

    now = now or datetime.now(timezone.utc)
    reasons = _Reasons()

    if settings is None:
        try:
            settings = load_settings()
        except Exception as exc:  # noqa: BLE001
            logger.error("Payment settings unreadable (%s)", type(exc).__name__)
            settings = None
    if settings is None or not settings.available:
        reasons.add("SIMULATION_DISABLED")
    roles = settings.initiator_roles if settings is not None else frozenset()
    if actor.role == "user" or actor.role not in roles:
        reasons.add("CALLER_NOT_AUTHORIZED")

    ticket = _read(get_ticket, ticket_id)
    approval = _read(get_current_for_ticket, ticket_id) if ticket is not None else None
    invoice_known = _read(_known_invoice, invoice_id)
    if not invoice_known:
        reasons.add("INVOICE_NOT_FOUND")

    wr = _dict((ticket or {}).get("workflow_result"))
    procurement = _dict(wr.get("procurement"))
    po = procurement.get("po") if isinstance(procurement.get("po"), dict) else None
    recon = wr.get("reconciliation") if isinstance(wr.get("reconciliation"), dict) else None
    # Invoice matched after approval (ticket_service.match_invoice_after_approval): only valid while it is
    # bound to the current run and the PO the approval covers; otherwise it is ignored (fail closed).
    post_match = (ticket or {}).get("invoice_match")
    post_approval_match = False
    if recon is None and isinstance(post_match, dict) and post_match.get("post_approval"):
        recon = post_match
        post_approval_match = True
        if (post_match.get("run_count") != int((ticket or {}).get("run_count") or 0)
                or post_match.get("po_id") != (po or {}).get("po_id")
                or _to_money(post_match.get("po_total")) != _to_money((po or {}).get("total"))):
            reasons.add("APPROVAL_STALE", "The post-approval invoice match no longer matches the approved PO.")

    if ticket is None:
        reasons.add("TICKET_NOT_APPROVED", "The ticket was not found.")
    elif ticket.get("status") != "approved":
        reasons.add("TICKET_NOT_APPROVED")

    if ticket is not None and wr:
        _policy_checks(ticket, wr, reasons)

    # --- invoice / reconciliation ---
    if recon is None:
        if invoice_known:
            reasons.add("RECONCILIATION_REQUIRED")
    else:
        if recon.get("invoice_id") != invoice_id:
            reasons.add("INVOICE_PO_MISMATCH", "The invoice is not the one reconciled on this ticket.")
        status = recon.get("status")
        if status == "duplicate":
            reasons.add("DUPLICATE_INVOICE")
        elif status != "match":
            reasons.add("INVOICE_MATCH_FAILED")
    if invoice_known and _read(_previously_paid, invoice_id, (po or {}).get("vendor_id")):
        reasons.add("DUPLICATE_INVOICE", "The invoice already appears in the payment history.")

    # --- purchase order ---
    po_id = (po or {}).get("po_id")
    if po is None:
        reasons.add("PO_NOT_FOUND")
    elif purchase_order_id is not None and purchase_order_id != po_id:
        reasons.add("INVOICE_PO_MISMATCH", "The purchase order is not the ticket's draft PO.")

    # --- canonical amount / currency ---
    canonical_amount = _to_money((recon or {}).get("invoiced_total")) if recon is not None else None
    if recon is not None and canonical_amount is None:
        reasons.add("AMOUNT_MISMATCH", "The reconciled invoice amount is missing or invalid.")
    if canonical_amount is not None and po is not None and _to_money(po.get("total")) != canonical_amount:
        reasons.add("AMOUNT_MISMATCH", "The invoiced amount does not equal the draft PO total.")
    if amount is not None and canonical_amount is not None:
        try:
            client_amount = parse_money(amount)
        except (ValueError, ArithmeticError):
            client_amount = None
        if client_amount != canonical_amount:
            reasons.add("AMOUNT_MISMATCH")
    elif amount is not None:
        try:
            parse_money(amount)
        except (ValueError, ArithmeticError):
            reasons.add("AMOUNT_MISMATCH", "The amount is not a valid positive decimal amount.")

    canonical_currency = (po or {}).get("currency") if po is not None else None
    if po is not None:
        if not isinstance(canonical_currency, str) or canonical_currency not in (
                settings.allowed_currencies if settings is not None else frozenset()):
            reasons.add("CURRENCY_NOT_ALLOWED")
        if currency is not None and currency != canonical_currency:
            reasons.add("CURRENCY_NOT_ALLOWED")
    elif currency is not None and currency not in (settings.allowed_currencies if settings else frozenset()):
        reasons.add("CURRENCY_NOT_ALLOWED")

    # --- approval ---
    approved_by = None
    policy_set_version = None
    if approval is None or approval.get("status") != "approved":
        reasons.add("APPROVAL_MISSING")
    else:
        last = _last_decision(approval)
        approved_by = last.get("actor_id")
        if last.get("action") != "approve" or last.get("actor_role") != "approver" or not approved_by:
            reasons.add("APPROVAL_MISSING", "The approval was not granted by an approver.")
        if approved_by and actor.username == approved_by:
            reasons.add("CALLER_NOT_AUTHORIZED", "The approving approver cannot also initiate the payment.")
        snap = _dict(approval.get("review_snapshot"))
        snap_po = snap.get("po") if isinstance(snap.get("po"), dict) else None
        snap_recon = snap.get("reconciliation") if isinstance(snap.get("reconciliation"), dict) else None

        if post_approval_match:
            # The approval covered the PO before the invoice arrived; the invoice must not have been part of it.
            if snap_recon is not None:
                reasons.add("APPROVAL_STALE", "The approval covered a different invoice reconciliation.")
        elif snap_recon is None or snap_recon.get("invoice_id") != invoice_id or snap_recon.get("status") != "match":
            reasons.add("APPROVAL_STALE", "The approval does not cover this matched invoice.")
        if snap_po is None:
            reasons.add("APPROVAL_STALE", "The approval does not cover a purchase order.")
        else:
            if purchase_order_id is not None and snap_po.get("po_id") != purchase_order_id:
                reasons.add("INVOICE_PO_MISMATCH", "The purchase order is not the approved PO.")
            if canonical_amount is not None and _to_money(snap_po.get("total")) != canonical_amount:
                reasons.add("AMOUNT_MISMATCH", "The approved PO total does not equal the invoiced amount.")
            if canonical_currency is not None and snap_po.get("currency") != canonical_currency:
                reasons.add("APPROVAL_STALE", "The approved currency differs from the current PO.")
        if ticket is not None:
            snap_run = snap.get("run_count")
            if not isinstance(snap_run, int) or snap_run != int(ticket.get("run_count") or 0):
                reasons.add("APPROVAL_STALE", "The ticket was re-run after approval.")
            if _canon_po(snap_po) != _canon_po(po):
                reasons.add("APPROVAL_STALE", "The purchase order changed after approval.")
            cur_recon = ({"invoice_id": recon.get("invoice_id"), "status": recon.get("status")}
                         if recon is not None and not post_approval_match else None)
            if _canon(snap_recon) != _canon(cur_recon):
                reasons.add("APPROVAL_STALE", "The invoice reconciliation changed after approval.")
        bound = bound_policy_set_version(approval)
        if bound is not None:
            policy_set_version = bound
            if _active_policy_set_version() != bound:
                reasons.add("APPROVAL_STALE", "The active policy set changed after approval.")

    facts = {
        "ticket_id": ticket_id, "invoice_id": invoice_id, "po_id": po_id,
        "amount": money_str(canonical_amount) if canonical_amount is not None else None,
        "currency": canonical_currency,
        "approval_id": (approval or {}).get("approval_id"),
        "request_version": (approval or {}).get("request_version"),
        "workflow_snapshot_hash": (approval or {}).get("workflow_snapshot_hash"),
        "policy_set_version": policy_set_version,
        "run_count": (ticket or {}).get("run_count"),
    }
    view = PaymentEligibility(
        eligible=not reasons.items,
        ticket_id=ticket_id,
        invoice_id=invoice_id,
        purchase_order_id=po_id,
        amount=facts["amount"],
        currency=canonical_currency if isinstance(canonical_currency, str) else None,
        invoice_match_status=(recon or {}).get("status") if recon is not None else None,
        approval_id=facts["approval_id"],
        approval_status=(approval or {}).get("status"),
        approved_by=approved_by,
        approval_request_version=facts["request_version"],
        policy_set_version=policy_set_version,
        reasons=list(reasons.items),
        checked_at=now,
        eligibility_fingerprint=_fingerprint(facts),
    )
    return EligibilityDecision(view=view, canonical_amount=canonical_amount, approval=approval, ticket=ticket)
