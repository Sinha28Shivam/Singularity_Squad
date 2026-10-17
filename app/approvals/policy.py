"""Deterministic approval policy and canonical review snapshot.

Only structured workflow fields are read (final_status, procurement.po.total, check/finding statuses,
request.attendees). Free text such as critic.summary / policy.summary / LLM output is never consulted,
so an LLM cannot raise, lower or clear an approval requirement.
"""
from __future__ import annotations

import hashlib
import json
import re
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


POLICY_RAG_REASON_CODES = ("POLICY_REVIEW_REQUIRED", "RATE_CARD_MISMATCH", "RATE_CARD_UNVERIFIED",
                           "POLICY_CONFLICT", "INSUFFICIENT_POLICY_EVIDENCE", "POLICY_RETRIEVAL_DEGRADED")
_RAG_TEXTS_CACHE: dict[str, str] = {}


def _rag_reason_text(code: str) -> str:
    if not _RAG_TEXTS_CACHE:
        try:
            from app.policy_rag.embeddings import load_config
            _RAG_TEXTS_CACHE.update(load_config().get("approval_reason_texts") or {})
        except Exception:  # noqa: BLE001 - fall back to the code itself
            pass
    return str(_RAG_TEXTS_CACHE.get(code, code))


_RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def _raise_risk(current: str, floor: str) -> str:
    return floor if _RISK_ORDER.get(floor, 0) > _RISK_ORDER.get(current, 0) else current


def _hard_block(update: dict, codes: list[str], reasons: list[str], detail: str) -> None:
    update.update({"required": False, "blocked": True, "risk_level": "high"})
    for code, text in (("POLICY_HARD_BLOCK", detail),
                       ("HARD_BLOCK", "A policy rule hard-blocks this request; ordinary approval cannot override it.")):
        if code not in codes:
            codes.insert(0, code)
            reasons.insert(0, text)


def _apply_policy_evaluation(req: ApprovalRequirement, evaluation: dict) -> ApprovalRequirement:
    """Merge a Policy RAG evaluation (and its embedded ``rule_engine`` output, if any) into a requirement.

    Monotonic: can only ADD reason codes, raise risk, turn the request into a hard block or ask for
    clarification; never clears a block, a review requirement or a clarification request, never approves.
    A clarification request does NOT hide findings: blocking/review codes are still added, so a ticket can be
    ``needs_clarification`` AND ``blocked`` at the same time (no approval request is created either way)."""
    if not isinstance(evaluation, dict):
        raise ValueError("policy_evaluation must be a dict")
    raw_codes = evaluation.get("approval_reason_codes") or []
    blocking = [str(b) for b in evaluation.get("blocking_findings") or []]
    if not isinstance(raw_codes, list):
        raise ValueError("approval_reason_codes must be a list")
    added = [c for c in POLICY_RAG_REASON_CODES if c in raw_codes]
    codes, reasons = list(req.reason_codes), list(req.reasons)
    facts = dict(req.facts)
    facts["policy_evaluation"] = {"evaluation_id": evaluation.get("evaluation_id"),
                                  "evaluation_hash": evaluation.get("evaluation_hash"),
                                  "outcome": evaluation.get("outcome"),
                                  "rule_set_version": evaluation.get("rule_set_version")}
    for c in added:
        if c not in codes:
            codes.append(c)
            reasons.append(_rag_reason_text(c))
    update: dict[str, Any] = {"reason_codes": codes, "reasons": reasons, "facts": facts}
    if blocking and not req.blocked:
        _hard_block(update, codes, reasons, f"Blocking policy findings: {', '.join(blocking)}")
    elif added and not req.blocked and (req.required or req.needs_clarification):
        update["risk_level"] = _raise_risk(req.risk_level, "medium")

    engine = evaluation.get("rule_engine")
    if engine is not None:
        _merge_rule_engine(req, engine, update, codes, reasons, facts)
    return req.model_copy(update=update)


def _merge_rule_engine(req: ApprovalRequirement, engine: Any, update: dict, codes: list[str],
                       reasons: list[str], facts: dict) -> None:
    """Rule-engine merge: blocked → hard block; review → codes + risk ≥ medium; needs_clarification →
    needs_clarification (block/review findings stay visible)."""
    if not isinstance(engine, dict):
        raise ValueError("rule_engine must be a dict")
    overall = engine.get("overall")
    if overall not in ("pass", "review_required", "blocked", "needs_clarification"):
        raise ValueError("Unknown rule engine overall status")
    blocking = [str(r) for r in engine.get("blocking_rule_refs") or []]
    review = [str(r) for r in engine.get("review_rule_refs") or []]
    clar = [str(r) for r in engine.get("clarification_rule_refs") or []]
    messages = {f"{r.get('rule_id')}@{r.get('rule_version')}": r for r in engine.get("results") or []
                if isinstance(r, dict)}
    facts["rule_engine"] = {"policy_set_version": engine.get("policy_set_version"),
                            "engine_hash": engine.get("engine_hash"), "overall": overall,
                            "blocking_rule_refs": blocking, "review_rule_refs": review,
                            "clarification_rule_refs": clar}
    for code in engine.get("reason_codes") or []:
        code = str(code)
        if _CODE_RE.match(code) and code not in codes:
            codes.append(code)
            text = next((str(r.get("explanation")) for r in messages.values()
                         if r.get("reason_code") == code and r.get("explanation")), None)
            reasons.append(text or _rag_reason_text(code))
    if overall == "blocked" and not blocking:
        raise ValueError("Blocked rule engine result without blocking rule refs")
    blocked_now = bool(update.get("blocked", req.blocked))
    if blocking and not blocked_now:
        _hard_block(update, codes, reasons, f"Blocking policy rules: {', '.join(blocking)}")
        blocked_now = True
    if clar or overall == "needs_clarification":
        update["needs_clarification"] = True
        update["required"] = False
        if "NEEDS_CLARIFICATION" not in codes:
            codes.append("NEEDS_CLARIFICATION")
            reasons.append("A policy rule needs more information from the requester before review.")
    if (review or clar) and not blocked_now:
        update["risk_level"] = _raise_risk(update.get("risk_level", req.risk_level), "medium")


def evaluate_approval_requirement(workflow_result: dict, policy: Optional[dict] = None,
                                  policy_evaluation: Optional[dict] = None) -> ApprovalRequirement:
    """Decide whether a human approval request is required. Raises on malformed input: callers must
    fail closed (no approval request, ticket not reported as approved).

    ``policy_evaluation`` (a PolicyEvaluationResult as JSON) may only add reason codes / raise risk / add an
    explicit hard block; when None the behaviour is exactly the pre-RAG behaviour."""
    req = _evaluate_base(workflow_result, policy)
    if policy_evaluation is None:
        return req
    return _apply_policy_evaluation(req, policy_evaluation)


def _evaluate_base(workflow_result: dict, policy: Optional[dict]) -> ApprovalRequirement:
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


# Transient key on the ticket dict handed to on_workflow_completed (never persisted on the ticket).
POLICY_BINDING_KEY = "_policy_evaluation_binding"
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
    binding = ticket.get(POLICY_BINDING_KEY)
    if isinstance(binding, dict) and binding.get("evaluation_hash"):
        # Binds the decision to the exact policy evidence version (covered by snapshot_hash).
        snap["policy_evaluation"] = {k: binding.get(k) for k in
                                     ("evaluation_id", "evaluation_hash", "rule_set_version", "documents_used")}
        engine = binding.get("rule_engine")
        if isinstance(engine, dict) and engine.get("engine_hash"):
            # Binds the decision to the exact active policy set / rule versions (stale check at decision time).
            snap["policy_evaluation"]["rule_engine"] = {k: engine.get(k) for k in
                                                        ("policy_set_version", "engine_hash", "evaluator_version",
                                                         "rule_refs")}
            snap["policy_set_version"] = engine.get("policy_set_version")
        rates = binding.get("rate_validations")
        if rates:
            snap["policy_evaluation"]["rate_validations"] = rates
    # Round-trip through JSON so the stored snapshot equals what was hashed.
    return json.loads(json.dumps(snap, sort_keys=True, default=str))


def snapshot_hash(snapshot: dict) -> str:
    payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
