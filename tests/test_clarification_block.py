"""Phase 2 §8 edge case: a clarification request never hides an independent hard block or review finding.
The requirement keeps needs_clarification AND blocked, every code is retained, no approval request is created
and the ticket view shows both."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.approvals import service as approval_service
from app.approvals.policy import evaluate_approval_requirement
from app.db import get_database
from app.main import app
from app.services import ticket_service
from tests.policy_rag_helpers import HAPPY, TODAY, seed_basic_library
from tests.test_rule_workflow_integration import activate

CLAR_WR = {"final_status": "needs_clarification", "request": {"attendees": 180}}
PASS_WR = {"final_status": "pass", "request": {"attendees": 180},
           "procurement": {"po": {"po_id": "P", "total": 1000.0, "currency": "INR", "line_items": []}},
           "policy": {"checks": []}}


def _ev(**kw):
    base = {"evaluation_id": "e", "evaluation_hash": "a" * 64, "outcome": "review_required",
            "approval_reason_codes": [], "blocking_findings": []}
    return {**base, **kw}


def _engine(overall, blocking=(), review=(), clar=(), codes=()):
    return {"overall": overall, "blocking_rule_refs": list(blocking), "review_rule_refs": list(review),
            "clarification_rule_refs": list(clar), "reason_codes": list(codes), "results": [],
            "policy_set_version": 2, "engine_hash": "c" * 64}


# ------------------------------------------------------------------------------------------- unit merge
def test_clarification_plus_review_keeps_review_codes():
    req = evaluate_approval_requirement(CLAR_WR, policy_evaluation=_ev(
        approval_reason_codes=["POLICY_REVIEW_REQUIRED", "RATE_CARD_MISMATCH"]))
    assert req.needs_clarification and not req.required and not req.blocked
    assert {"NEEDS_CLARIFICATION", "POLICY_REVIEW_REQUIRED", "RATE_CARD_MISMATCH"} <= set(req.reason_codes)
    assert req.risk_level == "medium"


def test_clarification_plus_rag_hard_block_keeps_both():
    req = evaluate_approval_requirement(CLAR_WR, policy_evaluation=_ev(
        outcome="hard_block", blocking_findings=["fnd_rate_card_validation_02"],
        approval_reason_codes=["RATE_CARD_MISMATCH"]))
    assert req.needs_clarification and req.blocked and not req.required and req.risk_level == "high"
    assert {"NEEDS_CLARIFICATION", "HARD_BLOCK", "POLICY_HARD_BLOCK", "RATE_CARD_MISMATCH"} <= set(req.reason_codes)
    assert any("fnd_rate_card_validation_02" in r for r in req.reasons)


def test_engine_clarification_plus_engine_block_keeps_both():
    eng = _engine("blocked", blocking=["RULE-B@1"], review=["RULE-R@1"], clar=["RULE-C@1"],
                  codes=["BLOCK_CODE", "CLAR_CODE", "REVIEW_CODE"])
    req = evaluate_approval_requirement(PASS_WR, policy_evaluation=_ev(rule_engine=eng))
    assert req.blocked and req.needs_clarification and not req.required
    assert {"HARD_BLOCK", "POLICY_HARD_BLOCK", "BLOCK_CODE", "CLAR_CODE", "REVIEW_CODE",
            "NEEDS_CLARIFICATION"} <= set(req.reason_codes)
    assert req.facts["rule_engine"]["blocking_rule_refs"] == ["RULE-B@1"]
    assert approval_service.on_workflow_completed({"ticket_id": "REQ-X-1"}, PASS_WR, req) is None


def test_engine_clarification_alone_asks_for_info_and_keeps_review():
    eng = _engine("needs_clarification", review=["RULE-R@1"], clar=["RULE-C@1"], codes=["CLAR_CODE", "REV"])
    req = evaluate_approval_requirement(PASS_WR, policy_evaluation=_ev(rule_engine=eng))
    assert req.needs_clarification and not req.required and not req.blocked
    assert {"CLAR_CODE", "REV", "PO_REQUIRES_HUMAN_APPROVAL"} <= set(req.reason_codes)
    assert req.risk_level == "medium"


def test_missing_condition_inputs_engine_review_never_clears_clarification():
    eng = _engine("review_required", review=["RULE-M@1"], codes=["MANDATORY_FACTS_MISSING"])
    req = evaluate_approval_requirement(CLAR_WR, policy_evaluation=_ev(rule_engine=eng))
    assert req.needs_clarification and not req.required
    assert "MANDATORY_FACTS_MISSING" in req.reason_codes


def test_blocked_workflow_stays_blocked_with_clarifying_rule():
    wr = {"final_status": "blocked", "policy": {"checks": [{"name": "budget", "status": "fail", "detail": "x"}]}}
    eng = _engine("needs_clarification", clar=["RULE-C@1"], codes=["CLAR_CODE"])
    req = evaluate_approval_requirement(wr, policy_evaluation=_ev(rule_engine=eng))
    assert req.blocked and req.needs_clarification and "HARD_BLOCK" in req.reason_codes


# ------------------------------------------------------------------------------------------- run path
@pytest.fixture
def c(monkeypatch):
    monkeypatch.setenv("POLICY_RAG_ENABLED", "1")
    monkeypatch.setenv("POLICY_RULES_ENABLED", "1")
    with TestClient(app) as client:
        yield client


def test_run_clarification_and_hard_block_both_visible(c, admin_headers, user_headers_factory):
    a, _ = seed_basic_library()
    activate(a.document_id, rule_id="RULE-NO-HUGE", op="gt", value=170, decision="blocked", reason="EVENT_TOO_LARGE")
    activate(a.document_id, rule_id="RULE-NEED-DETAIL", op="gte", value=100, decision="needs_clarification",
             reason="HEADCOUNT_DETAIL_NEEDED", creator="dave")
    alice = user_headers_factory("alice")
    r = c.post("/api/my/tickets", json={**HAPPY, "attendees": 180, "budget_inr": 200000}, headers=alice)
    tid = r.json()["ticket_id"]
    out = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY}, headers=admin_headers)
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["status"] == "needs_info"
    ps = body["policy_status"]
    assert ps["needs_clarification"] is True and ps["blocked"] is True and ps["required"] is False
    assert {"HARD_BLOCK", "EVENT_TOO_LARGE", "HEADCOUNT_DETAIL_NEEDED", "NEEDS_CLARIFICATION"} <= set(ps["reason_codes"])
    assert get_database().approval_requests.count_documents({"ticket_id": tid}) == 0
    mine = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    assert mine["status"] == "needs_info"
    assert mine["policy_status"]["needs_clarification"] is True and mine["policy_status"]["blocked"] is True


def test_run_workflow_clarification_plus_review_rule(c, admin_headers, user_headers_factory, monkeypatch):
    a, _ = seed_basic_library()
    activate(a.document_id, rule_id="RULE-LARGE-EVENT", op="gte", value=100)
    real = ticket_service.build_request_text
    monkeypatch.setattr(ticket_service, "build_request_text",
                        lambda sub: real(sub).replace(f" on {sub.event_date.isoformat()}", ""))
    alice = user_headers_factory("alice")
    tid = c.post("/api/my/tickets", json=HAPPY, headers=alice).json()["ticket_id"]
    out = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY}, headers=admin_headers)
    assert out.status_code == 200, out.text
    body = out.json()
    assert body["status"] == "needs_info" and body["workflow_result"]["final_status"] == "needs_clarification"
    ps = body["policy_status"]
    assert ps["needs_clarification"] and not ps["blocked"]
    assert "LARGE_EVENT_REVIEW" in ps["reason_codes"] and "NEEDS_CLARIFICATION" in ps["reason_codes"]
    assert get_database().approval_requests.count_documents({"ticket_id": tid}) == 0
