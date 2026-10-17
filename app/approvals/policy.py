"""Deterministic approval policy and canonical review snapshot.

Only structured workflow fields are read (final_status, procurement.po.total, check/finding statuses,
request.attendees). Free text such as critic.summary / policy.summary / LLM output is never consulted,
so an LLM cannot raise, lower or clear an approval requirement.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from app.approvals.schemas import ApprovalRequirement

_KNOWN_FINAL_STATUSES = {"pass", "warning", "blocked", "needs_clarification"}


def _checks(section: Any) -> list[dict]:
    if not isinstance(section, dict):
        return []
    return [c for c in (section.get("checks") or []) if isinstance(c, dict)]


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate_approval_requirement(workflow_result: dict, policy: Optional[dict] = None) -> ApprovalRequirement:
    """Decide whether a human approval request is required. Raises on malformed input: callers must
    fail closed (no approval request, ticket not reported as approved)."""
    if policy is None:
        from app.approvals.service import load_policy
        policy = load_policy()
    if not isinstance(workflow_result, dict):
        raise ValueError("workflow_result must be a dict")
    final_status = workflow_result.get("final_status")
    if final_status not in _KNOWN_FINAL_STATUSES:
        raise ValueError("Unknown workflow final_status")

    policy_version = str(policy["policy_version"])
    required_role = str(policy.get("required_role") or "approver")
    threshold = float(policy["amount_threshold"])
    high_value = float(policy["high_risk_amount"])
    texts: dict = policy.get("reason_texts") or {}
    base = {"policy_version": policy_version, "required_role": required_role}

    if final_status == "needs_clarification":
        return ApprovalRequirement(required=False, needs_clarification=True,
                                   reason_codes=["NEEDS_CLARIFICATION"],
                                   reasons=["The request needs more information before it can be reviewed."],
                                   **base)

    procurement = workflow_result.get("procurement") if isinstance(workflow_result.get("procurement"), dict) else {}
    po = procurement.get("po") if isinstance(procurement.get("po"), dict) else None

    if final_status == "blocked" or po is None:
        codes, reasons = ["HARD_BLOCK" if final_status == "blocked" else "NO_DRAFT_PO"], []
        failing = [c for c in _checks(workflow_result.get("policy")) + _checks(procurement)
                   if c.get("status") == "fail"]
        critic = workflow_result.get("critic") if isinstance(workflow_result.get("critic"), dict) else {}
        failing += [f for f in (critic.get("findings") or []) if isinstance(f, dict) and f.get("status") == "fail"]
        seen: set[str] = set()
        for c in failing:
            name = str(c.get("name") or "check")
            if name in seen:
                continue
            seen.add(name)
            codes.append(f"FAILED_CHECK:{name}")
            reasons.append(str(c.get("detail") or name))
        if not reasons:
            reasons.append("No draft PO was produced; nothing can be approved."
                           if po is None else "The workflow is blocked by policy.")
        return ApprovalRequirement(required=False, blocked=True, reason_codes=codes, reasons=reasons,
                                   risk_level="high" if final_status == "blocked" else "low", **base)

    total = _num(po.get("total"))
    if total is None:
        raise ValueError("Draft PO total is missing or invalid")
    currency = str(po.get("currency") or "INR")

    codes = ["PO_REQUIRES_HUMAN_APPROVAL"]
    if total > threshold:
        codes.append("AMOUNT_THRESHOLD")
    if total > high_value:
        codes.append("HIGH_VALUE")
    if final_status == "warning" and policy.get("require_approval_on_warning", True):
        codes.append("POLICY_WARNING")
    if any(c.get("name") == "attendee_limit" and c.get("status") == "warn"
           for c in _checks(workflow_result.get("policy"))):
        codes.append("LARGE_EVENT")

    if "HIGH_VALUE" in codes:
        risk = "high"
    elif {"AMOUNT_THRESHOLD", "POLICY_WARNING", "LARGE_EVENT"} & set(codes):
        risk = "medium"
    else:
        risk = "low"

    request = workflow_result.get("request") if isinstance(workflow_result.get("request"), dict) else {}
    facts = {"po_total": total, "currency": currency, "amount_threshold": threshold,
             "high_risk_amount": high_value, "final_status": final_status,
             "attendees": request.get("attendees")}
    return ApprovalRequirement(required=True, reason_codes=codes, reasons=[str(texts.get(c, c)) for c in codes],
                               risk_level=risk, amount=total, currency=currency, facts=facts, **base)


_REQUEST_FIELDS = ("date", "attendees", "budget_limit", "service", "location")
_PO_FIELDS = ("po_id", "vendor_id", "vendor_name", "line_items", "subtotal", "tax", "total", "currency",
              "required_approver")


def review_snapshot(workflow_result: dict, ticket: dict) -> dict:
    """Canonical fields an approver reviews; covered by ``snapshot_hash``."""
    wr = workflow_result if isinstance(workflow_result, dict) else {}
    request = wr.get("request") if isinstance(wr.get("request"), dict) else {}
    procurement = wr.get("procurement") if isinstance(wr.get("procurement"), dict) else {}
    po = procurement.get("po") if isinstance(procurement.get("po"), dict) else None
    reconciliation = wr.get("reconciliation") if isinstance(wr.get("reconciliation"), dict) else None
    snap: dict[str, Any] = {
        "ticket_id": ticket.get("ticket_id"),
        "run_count": int(ticket.get("run_count") or 0),
        "request": {k: request.get(k) for k in _REQUEST_FIELDS},
        "po": {k: po.get(k) for k in _PO_FIELDS} if po else None,
        "final_status": wr.get("final_status"),
    }
    if ticket.get("submission"):
        snap["submission"] = ticket["submission"]
    if reconciliation is not None:
        snap["reconciliation"] = {"invoice_id": reconciliation.get("invoice_id"),
                                  "status": reconciliation.get("status")}
    # Round-trip through JSON so the stored snapshot equals what was hashed.
    return json.loads(json.dumps(snap, sort_keys=True, default=str))


def snapshot_hash(snapshot: dict) -> str:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
