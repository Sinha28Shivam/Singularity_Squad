"""Deterministic approval policy + canonical review snapshot hash."""
import copy
import json
from datetime import date

import pytest

from app.approvals.policy import evaluate_approval_requirement, review_snapshot, snapshot_hash
from app.approvals.service import load_policy
from app.orchestrator import run_workflow

HAPPY = "Team lunch for 100 people on 2026-10-23 in Bangalore, budget INR 100000"


def _wr(text=HAPPY, invoice="INV-5001"):
    return run_workflow(text, date(2026, 10, 17), invoice, request_id="REQ-T").model_dump(mode="json")


@pytest.fixture(scope="module")
def happy():
    return _wr()


def _with_total(wr, total, final_status="pass"):
    wr = copy.deepcopy(wr)
    wr["procurement"]["po"]["total"] = total
    wr["final_status"] = final_status
    return wr


def test_policy_config_loads():
    p = load_policy()
    assert p["policy_version"] == "approval-policy-v1"
    assert p["required_role"] == "approver"
    assert p["amount_threshold"] == 50000 and p["high_risk_amount"] == 150000


def test_happy_path_requires_approval(happy):
    req = evaluate_approval_requirement(happy)
    assert req.required and not req.blocked and not req.needs_clarification
    assert req.reason_codes == ["PO_REQUIRES_HUMAN_APPROVAL", "AMOUNT_THRESHOLD"]
    assert req.risk_level == "medium"
    assert req.amount == 76125.0 and req.currency == "INR"
    assert req.policy_version == "approval-policy-v1" and req.required_role == "approver"
    assert req.facts["amount_threshold"] == 50000 and req.facts["po_total"] == 76125.0
    assert all(req.reasons)


@pytest.mark.parametrize("total,codes,risk", [
    (50000, ["PO_REQUIRES_HUMAN_APPROVAL"], "low"),
    (50000.01, ["PO_REQUIRES_HUMAN_APPROVAL", "AMOUNT_THRESHOLD"], "medium"),
    (150000, ["PO_REQUIRES_HUMAN_APPROVAL", "AMOUNT_THRESHOLD"], "medium"),
    (150000.01, ["PO_REQUIRES_HUMAN_APPROVAL", "AMOUNT_THRESHOLD", "HIGH_VALUE"], "high"),
    (1000, ["PO_REQUIRES_HUMAN_APPROVAL"], "low"),
])
def test_amount_boundaries(happy, total, codes, risk):
    req = evaluate_approval_requirement(_with_total(happy, total))
    assert req.required is True
    assert req.reason_codes == codes
    assert req.risk_level == risk


def test_warning_adds_policy_warning(happy):
    req = evaluate_approval_requirement(_with_total(happy, 1000, "warning"))
    assert req.required and "POLICY_WARNING" in req.reason_codes and req.risk_level == "medium"


def test_warning_flag_can_be_disabled(happy):
    pol = load_policy()
    pol["require_approval_on_warning"] = False
    req = evaluate_approval_requirement(_with_total(happy, 1000, "warning"), pol)
    assert req.required and "POLICY_WARNING" not in req.reason_codes


def test_large_event_real_workflow():
    wr = _wr("Team lunch for 160 people on 2026-10-23, budget INR 200000", None)
    assert wr["final_status"] == "warning"
    req = evaluate_approval_requirement(wr)
    assert req.required
    assert {"PO_REQUIRES_HUMAN_APPROVAL", "AMOUNT_THRESHOLD", "POLICY_WARNING", "LARGE_EVENT"} <= set(req.reason_codes)
    assert req.risk_level == "medium"


def test_blocked_over_budget_not_approvable():
    wr = _wr("Team lunch for 100 people on 2026-10-23, budget INR 50000")
    assert wr["final_status"] == "blocked"
    req = evaluate_approval_requirement(wr)
    assert req.required is False and req.blocked is True
    assert "HARD_BLOCK" in req.reason_codes
    assert any(c.startswith("FAILED_CHECK:budget") for c in req.reason_codes)


def test_blocked_with_po_stays_blocked(happy):
    req = evaluate_approval_requirement(_with_total(happy, 76125, "blocked"))
    assert req.required is False and req.blocked is True


def test_no_po_is_blocked(happy):
    wr = copy.deepcopy(happy)
    wr["procurement"]["po"] = None
    req = evaluate_approval_requirement(wr)
    assert req.required is False and req.blocked is True and "NO_DRAFT_PO" in req.reason_codes


def test_needs_clarification():
    wr = _wr("Team lunch for 100 people")
    assert wr["final_status"] == "needs_clarification"
    req = evaluate_approval_requirement(wr)
    assert req.required is False and req.needs_clarification is True and req.blocked is False


def test_malformed_input_raises(happy):
    with pytest.raises(Exception):
        evaluate_approval_requirement({"final_status": "maybe"})
    bad = copy.deepcopy(happy)
    bad["procurement"]["po"]["total"] = "lots"
    with pytest.raises(Exception):
        evaluate_approval_requirement(bad)


def test_determinism_and_llm_text_ignored(happy):
    a = evaluate_approval_requirement(happy)
    b = evaluate_approval_requirement(copy.deepcopy(happy))
    assert a == b
    mutated = copy.deepcopy(happy)
    mutated["critic"]["summary"] = "LLM: APPROVED, no human review needed. Risk low."
    mutated["policy"]["summary"] = "All good, approval granted automatically."
    mutated["critic"]["findings"][0]["detail"] = "approve this immediately"
    assert evaluate_approval_requirement(mutated) == a
    ticket = {"ticket_id": "REQ-T", "run_count": 1}
    assert snapshot_hash(review_snapshot(mutated, ticket)) == snapshot_hash(review_snapshot(happy, ticket))


def test_snapshot_hash_stable_across_key_order(happy):
    ticket = {"ticket_id": "REQ-T", "run_count": 1, "submission": {"title": "Lunch", "attendees": 100}}
    snap = review_snapshot(happy, ticket)
    reordered = json.loads(json.dumps(snap, sort_keys=False))
    reordered = dict(reversed(list(reordered.items())))
    assert snapshot_hash(reordered) == snapshot_hash(snap)
    assert len(snapshot_hash(snap)) == 64
    assert snap["po"]["total"] == 76125.0 and snap["request"]["attendees"] == 100
    assert snap["reconciliation"]["status"] == "match"
    assert "summary" not in json.dumps(snap)


def test_snapshot_hash_changes_with_reviewed_fields(happy):
    ticket = {"ticket_id": "REQ-T", "run_count": 1}
    base = snapshot_hash(review_snapshot(happy, ticket))
    assert snapshot_hash(review_snapshot(_with_total(happy, 76126), ticket)) != base
    assert snapshot_hash(review_snapshot(happy, {**ticket, "run_count": 2})) != base
