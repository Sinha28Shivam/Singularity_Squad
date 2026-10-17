"""Phase 3 workflow integration: the normal ticket-run path evaluates the active approved policy set, stores
the engine output in the evidence snapshot, binds policy_set_version/engine_hash/rule refs into the review
snapshot, merges outcomes monotonically, rejects stale decisions after a new activation, keeps old snapshots
immutable, never leaves the PO non-draft and fails closed on engine/registry errors (spec §10, §12, §17.4)."""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.approvals.policy import evaluate_approval_requirement
from app.db import get_database
from app.main import app
from app.policy_rules import engine, registry
from app.policy_rules.schemas import PolicyRuleIn
from app.portal_schemas import UserPublic
from app.repositories import evidence_snapshot_repository as snaps
from tests.policy_rag_helpers import HAPPY, TODAY, seed_basic_library
from tests.test_rule_registry import evidence_for

STALE_MSG = "Policy set changed since this request was evaluated; re-run the ticket."
BIG = {**HAPPY, "title": "Annual offsite lunch", "attendees": 180, "budget_inr": 200000}


def reviewer(name: str = "rita", role: str = "approver") -> UserPublic:
    return UserPublic(username=name, name=name.title(), email=f"{name}@example.com", role=role)


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setenv("POLICY_RAG_ENABLED", "1")
    monkeypatch.setenv("POLICY_RULES_ENABLED", "1")
    with TestClient(app) as client:
        yield client


def activate(doc_id: str, *, rule_id: str, field: str = "request.attendees", op: str = "gte", value=150,
             value_type: str = "integer", decision: str = "review_required", reason: str = "LARGE_EVENT_REVIEW",
             mandatory: bool = False, creator: str = "alice", approver: str = "bob") -> int:
    severity = {"blocked": "block", "pass": "info"}.get(decision, "review")
    rule = PolicyRuleIn.model_validate({
        "rule_id": rule_id, "name": f"Rule {rule_id}", "severity": severity, "mandatory": mandatory,
        "condition": {"field": field, "op": op, "value": value, "value_type": value_type},
        "outcome": {"decision": decision, "reason_code": reason, "message": f"{reason} message."},
        "evidence_refs": [evidence_for(doc_id).model_dump()]})
    cand = registry.create_candidate(rule, reviewer(creator))
    return registry.approve(cand["rule_id"], cand["rule_version"], reviewer(approver))["policy_set_version"]


def _library():
    a, _b = seed_basic_library()
    return a.document_id


def _submit_and_run(c, admin, owner, body=HAPPY):
    r = c.post("/api/my/tickets", json=body, headers=owner)
    assert r.status_code == 201, r.text
    tid = r.json()["ticket_id"]
    return tid, c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY}, headers=admin)


def _decide(c, headers, cur, action="approve", key="key-0000-0001", comment=None):
    body = {"action": action, "idempotency_key": key, "request_version": cur["request_version"],
            "workflow_snapshot_hash": cur["workflow_snapshot_hash"]}
    if comment:
        body["comment"] = comment
    return c.post(f"/api/approvals/{cur['approval_id']}/decision", json=body, headers=headers)


# ------------------------------------------------------------------------------------------- run path
def test_normal_run_evaluates_active_policy_set_and_binds_versions(c, admin_headers, user_headers_factory):
    doc_id = _library()
    version = activate(doc_id, rule_id="RULE-LARGE-EVENT")
    tid, r = _submit_and_run(c, admin_headers, user_headers_factory("alice"), BIG)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval"
    snap = snaps.get_latest(tid)
    eng = snap["evaluation"]["rule_engine"]
    assert eng["policy_set_version"] == version and eng["overall"] == "review_required"
    assert eng["review_rule_refs"] == ["RULE-LARGE-EVENT@1"] and len(eng["engine_hash"]) == 64
    assert eng["facts_used"] == {"request.attendees": 180}
    assert snap["evaluation"]["evaluation_hash"]  # rule_engine is excluded from the RAG hash (own engine_hash)
    events = list(get_database().policy_events.find({"event_type": "rule_engine_evaluated", "ticket_id": tid}))
    assert [e["policy_set_version"] for e in events] == [version] and events[0]["engine_hash"] == eng["engine_hash"]
    cur = body["approval"]["current"]
    assert "LARGE_EVENT_REVIEW" in cur["reason_codes"] and cur["risk_level"] in ("medium", "high")
    bound = cur["review_snapshot"]["policy_evaluation"]["rule_engine"]
    assert bound == {"policy_set_version": version, "engine_hash": eng["engine_hash"],
                     "evaluator_version": eng["evaluator_version"], "rule_refs": ["RULE-LARGE-EVENT@1"]}
    assert cur["review_snapshot"]["policy_set_version"] == version
    assert cur["stale_policy"] is False and cur["can_decide"] in (True, False)
    # admin ticket view exposes the server-derived flags
    assert body["policy_status"]["policy_set_version"] == version
    assert body["policy_status"]["rule_engine_overall"] == "review_required"


def test_rules_disabled_keeps_phase2_behaviour(c, admin_headers, user_headers_factory, monkeypatch):
    monkeypatch.setenv("POLICY_RULES_ENABLED", "0")
    activate(_library(), rule_id="RULE-LARGE-EVENT")
    tid, r = _submit_and_run(c, admin_headers, user_headers_factory("alice"), BIG)
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    snap = snaps.get_latest(tid)
    assert snap["evaluation"].get("rule_engine") is None
    cur = r.json()["approval"]["current"]
    assert "LARGE_EVENT_REVIEW" not in cur["reason_codes"]
    assert "rule_engine" not in cur["review_snapshot"]["policy_evaluation"]
    assert "policy_set_version" not in cur["review_snapshot"]


def test_empty_active_set_is_missing_coverage_not_pass(c, admin_headers, user_headers_factory):
    _library()
    tid, r = _submit_and_run(c, admin_headers, user_headers_factory("alice"))
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    eng = snaps.get_latest(tid)["evaluation"]["rule_engine"]
    assert eng["policy_set_version"] == 0 and eng["overall"] == "review_required"
    assert eng["coverage"][0]["status"] == "missing_coverage"
    assert "POLICY_COVERAGE_MISSING" in r.json()["approval"]["current"]["reason_codes"]


def test_engine_block_hard_blocks_and_creates_no_approval(c, admin_headers, user_headers_factory):
    activate(_library(), rule_id="RULE-NO-HUGE", op="gt", value=170, decision="blocked", reason="EVENT_TOO_LARGE")
    tid, r = _submit_and_run(c, admin_headers, user_headers_factory("alice"), BIG)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "reviewed"
    assert get_database().approval_requests.count_documents({"ticket_id": tid}) == 0
    ps = r.json()["policy_status"]
    assert ps["blocked"] is True and ps["required"] is False
    assert {"HARD_BLOCK", "POLICY_HARD_BLOCK", "EVENT_TOO_LARGE"} <= set(ps["reason_codes"])


def test_missing_mandatory_fact_cannot_pass(c, admin_headers, user_headers_factory):
    # workflow.reconciliation_status is absent without an invoice -> mandatory rule -> review_required
    activate(_library(), rule_id="RULE-RECON-REQUIRED", field="workflow.reconciliation_status", op="ne",
             value="matched", value_type="string", mandatory=True, reason="RECONCILIATION_CHECK")
    tid, r = _submit_and_run(c, admin_headers, user_headers_factory("alice"))
    assert r.status_code == 200
    eng = snaps.get_latest(tid)["evaluation"]["rule_engine"]
    res = eng["results"][0]
    assert res["status"] == "review_required" and res["missing_fields"] == ["workflow.reconciliation_status"]
    assert eng["overall"] == "review_required"
    assert "MANDATORY_FACTS_MISSING" in r.json()["approval"]["current"]["reason_codes"]


def test_registry_failure_fails_closed(c, admin_headers, user_headers_factory, monkeypatch):
    _library()

    def broken(*a, **k):
        raise RuntimeError("registry down with secret detail")

    monkeypatch.setattr(registry, "active_version", broken)
    tid, r = _submit_and_run(c, admin_headers, user_headers_factory("alice"))
    assert r.status_code == 503 and "secret detail" not in r.text
    assert get_database().approval_requests.count_documents({"ticket_id": tid}) == 0
    assert snaps.get_latest(tid) is None  # no partial snapshot
    assert get_database().tickets.find_one({"ticket_id": tid})["status"] == "reviewed"


def test_engine_evaluator_error_is_review_never_pass():
    facts_ticket = {"ticket_id": "REQ-1", "run_count": 1, "submission": HAPPY}
    wr = {"final_status": "pass", "request": {"attendees": 100, "date": "2026-10-23"}}
    rule = {"rule_id": "RULE-BROKEN", "rule_version": 1, "name": "Broken rule", "status": "approved",
            "severity": "review", "source": "legacy_config", "created_by": "x", "created_at": "2026-01-01T00:00:00Z",
            "condition": {"field": "request.attendees", "op": "gt", "value": "abc", "value_type": "integer"},
            "outcome": {"decision": "review_required", "reason_code": "X_CODE", "message": "msg here"}}
    ev = engine.evaluate_ticket_rules(facts_ticket, wr, [], None, dt.date(2026, 10, 23), rules=[rule],
                                      policy_set_version=3)
    assert ev.results[0].status == "evaluation_error" and ev.overall == "review_required"
    assert ev.coverage[0].status == "evaluation_error"


# ------------------------------------------------------------------------------------------- stale + immutable
def test_stale_snapshot_rejected_after_new_activation_and_old_snapshot_immutable(
        c, admin_headers, user_headers_factory, approver_headers_factory):
    doc_id = _library()
    v1 = activate(doc_id, rule_id="RULE-LARGE-EVENT")
    alice = user_headers_factory("alice")
    tid, r = _submit_and_run(c, admin_headers, alice, BIG)
    cur = r.json()["approval"]["current"]
    before = snaps.get_for_run(tid, 1)
    # a new policy set is activated while the request is pending
    v2 = activate(doc_id, rule_id="RULE-PRIVACY-TERMS", field="request.free_text_terms", op="contains_any",
                  value=["photo"], value_type="string_list", reason="PRIVACY_CHECK", creator="dave")
    assert v2 == v1 + 1 and registry.active_version() == v2
    carol = approver_headers_factory("carol")
    view = c.get(f"/api/approvals/{cur['approval_id']}", headers=carol).json()
    assert view["stale_policy"] is True and view["can_decide"] is False
    assert view["cannot_decide_reason"] == STALE_MSG
    ev = c.get(f"/api/requests/{tid}/evidence", headers=carol).json()
    assert ev["stale_policy"] is True and ev["rule_engine"]["policy_set_version"] == v1
    d = _decide(c, carol, cur)
    assert d.status_code == 409 and d.json()["detail"] == STALE_MSG
    doc = get_database().approval_requests.find_one({"approval_id": cur["approval_id"]})
    assert doc["status"] == "pending" and doc["decisions"] == []
    # the old evidence snapshot is untouched by the activation
    assert snaps.get_for_run(tid, 1) == before
    # re-run binds to the new policy set and becomes decidable; PO stays draft after approval
    r2 = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY}, headers=admin_headers)
    cur2 = r2.json()["approval"]["current"]
    assert cur2["review_snapshot"]["policy_set_version"] == v2 and cur2["stale_policy"] is False
    assert snaps.get_for_run(tid, 1) == before and snaps.get_for_run(tid, 2)["evaluation"]["rule_engine"][
        "policy_set_version"] == v2
    ok = _decide(c, carol, cur2, key="key-0000-0002")
    assert ok.status_code == 200, ok.text
    po = get_database().purchase_orders.find_one({"request_id": tid}) or \
        get_database().purchase_orders.find_one({"po_id": cur2["review_snapshot"]["po"]["po_id"]})
    assert po["status"] == "draft" and po["approval_status"] == "approved"


def test_registry_unreadable_at_decision_time_fails_closed(c, admin_headers, user_headers_factory,
                                                           approver_headers_factory, monkeypatch):
    activate(_library(), rule_id="RULE-LARGE-EVENT")
    tid, r = _submit_and_run(c, admin_headers, user_headers_factory("alice"), BIG)
    cur = r.json()["approval"]["current"]
    carol = approver_headers_factory("carol")
    monkeypatch.setattr(registry, "active_version", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    d = _decide(c, carol, cur)
    assert d.status_code == 503
    assert get_database().approval_requests.find_one({"approval_id": cur["approval_id"]})["status"] == "pending"


def test_requester_evidence_view_redacts_rule_engine(c, admin_headers, user_headers_factory):
    activate(_library(), rule_id="RULE-LARGE-EVENT")
    alice = user_headers_factory("alice")
    tid, _r = _submit_and_run(c, admin_headers, alice, BIG)
    ev = c.get(f"/api/requests/{tid}/evidence", headers=alice).json()
    assert ev["redacted"] is True
    re_ = ev["rule_engine"]
    assert re_["overall"] == "review_required"
    assert all(set(x) == {"status", "explanation"} for x in re_["results"])
    text = str(ev)
    assert "engine_hash" not in text and "facts_used" not in text and "RULE-LARGE-EVENT" not in text
    mine = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    assert set(mine["policy_status"]) == {"needs_clarification", "blocked", "reasons"}


# ------------------------------------------------------------------------------------------- safety
_FORBIDDEN_CALL = re.compile(r"requests\.(post|put)|httpx\.|urllib\.request|smtplib|stripe|paypal|\.pay\(",
                             re.IGNORECASE)


def test_no_external_execution_calls_in_engine_or_integration():
    root = Path(__file__).resolve().parents[1] / "app"
    for rel in ("policy_rules/engine.py", "policy_rules/facts.py", "policy_rules/evaluator.py",
                "policy_rag/evaluation.py", "approvals/policy.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert not _FORBIDDEN_CALL.search(text), rel


def test_merge_is_monotonic_and_ignores_malformed_codes():
    wr = {"final_status": "pass", "request": {"attendees": 10},
          "procurement": {"po": {"po_id": "P", "total": 1000.0, "currency": "INR", "line_items": []}},
          "policy": {"checks": []}}
    base = evaluate_approval_requirement(wr)
    ev = {"evaluation_id": "e", "evaluation_hash": "a" * 64, "outcome": "pass", "approval_reason_codes": [],
          "blocking_findings": [],
          "rule_engine": {"overall": "review_required", "reason_codes": ["GOOD_CODE", "bad code; drop"],
                          "blocking_rule_refs": [], "review_rule_refs": ["RULE-X@1"], "clarification_rule_refs": [],
                          "results": [], "policy_set_version": 1, "engine_hash": "b" * 64}}
    req = evaluate_approval_requirement(wr, policy_evaluation=ev)
    assert req.required and req.risk_level == "medium"
    assert req.reason_codes[:len(base.reason_codes)] == base.reason_codes
    assert "GOOD_CODE" in req.reason_codes and "bad code; drop" not in req.reason_codes
    with pytest.raises(ValueError):
        evaluate_approval_requirement(wr, policy_evaluation={**ev, "rule_engine": {"overall": "approved"}})
