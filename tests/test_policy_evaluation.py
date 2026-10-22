"""Policy evaluation (spec §18.4/§18.5) and its integration with run_ticket / approval requirements."""
import copy
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.approvals.policy import evaluate_approval_requirement, review_snapshot, snapshot_hash
from app.db import get_database
from app.main import app
from app.policy_rag import evaluation, rate_cards
from app.policy_rag.schemas import RateQuote
from app.portal_schemas import UserPublic
from app.repositories import evidence_snapshot_repository as snaps
from app.repositories import rate_card_repository as rate_repo
from app.services import ticket_service
from tests.policy_rag_helpers import (
    HAPPY,
    PRIVACY_PAGES,
    TODAY,
    ingest,
    rate_card_pages,
    seed_basic_library,
)
from tests.pdf_factory import text_page

APPROVER = UserPublic(username="carol", name="Carol", email="c@example.com", role="approver")


def _wr(attendees=100, vendor="VEN-002", status="pass", total=76125.0):
    return {"final_status": status,
            "request": {"date": "2026-10-23", "attendees": attendees, "service": "lunch", "location": "Bengaluru"},
            "procurement": {"po": {"po_id": "DRAFT-PO-1", "vendor_id": vendor, "vendor_name": "GreenLeaf",
                                   "total": total, "currency": "INR", "line_items": []}, "checks": []},
            "policy": {"checks": []}, "critic": {"findings": []}}


def _ticket(tid="REQ-20261017-AAAA0001", run_count=1, **sub):
    return {"ticket_id": tid, "run_count": run_count, "owner_username": "alice",
            "submission": {**HAPPY, **sub}}


# ---------------------------------------------------------------- unit evaluation
def test_empty_library_is_unknown_with_insufficient_evidence_not_error():
    res = evaluation.evaluate_ticket(_ticket(), _wr())
    assert res.outcome == "unknown"
    assert "INSUFFICIENT_POLICY_EVIDENCE" in res.approval_reason_codes and res.evidence == []
    assert res.blocking_findings == [] and res.missing_evidence
    assert len(res.evaluation_hash) == 64


def test_review_required_vendor_verification_cites_real_evidence():
    seed_basic_library()
    res = evaluation.evaluate_ticket(_ticket(), _wr())
    # offline: local hashed vectors are not semantic -> accurate degraded label + reason code
    assert res.outcome == "review_required" and res.retrieval_mode == "hybrid_local_hash"
    assert res.embedding_provider == "local_hash" and res.embedding_available is False
    assert "POLICY_RETRIEVAL_DEGRADED" in res.approval_reason_codes
    vv = next(f for f in res.findings if f.rule_id == "vendor_verification")
    assert vv.severity == "review_required" and vv.evidence_ids
    ids = {e.evidence_id for e in res.evidence}
    assert all(set(f.evidence_ids) <= ids for f in res.findings)
    assert any(e.filename == "vendor_policy.pdf" for e in res.evidence if e.evidence_id in vv.evidence_ids)
    assert "POLICY_REVIEW_REQUIRED" in res.approval_reason_codes
    rate = next(f for f in res.findings if f.rule_id == "rate_card_applicability")
    assert rate.status == "not_applicable" and res.rate_validation is None
    assert {d["document_id"] for d in res.documents_used} == {e.document_id for e in res.evidence}


def test_large_event_triggers_safety_review_and_small_does_not():
    seed_basic_library()
    big = evaluation.evaluate_ticket(_ticket(attendees=200), _wr(attendees=200))
    f = next(f for f in big.findings if f.rule_id == "safety_review")
    assert f.category == "security" and f.evidence_ids
    assert any(e.page_number == 3 for e in big.evidence if e.evidence_id in f.evidence_ids)
    small = evaluation.evaluate_ticket(_ticket(), _wr())
    assert not any(f.rule_id == "safety_review" for f in small.findings)


def test_privacy_review_only_when_request_mentions_personal_data():
    seed_basic_library()
    ingest(PRIVACY_PAGES, "privacy.pdf", document_type="privacy_policy")
    res = evaluation.evaluate_ticket(_ticket(notes="We will take photos of attendees for the newsletter."), _wr())
    f = next(f for f in res.findings if f.rule_id == "data_privacy")
    assert f.category == "privacy" and f.evidence_ids
    assert not any(f.rule_id == "data_privacy" for f in evaluation.evaluate_ticket(_ticket(), _wr()).findings)


def test_conflicting_thresholds_without_precedence_escalate():
    seed_basic_library()
    ingest([text_page("Large events", "For events over 100 attendees a documented risk assessment, emergency plan "
                                      "and first aid cover are required by the security office.")],
           "security_policy.pdf", document_type="security_policy")
    res = evaluation.evaluate_ticket(_ticket(attendees=200), _wr(attendees=200))
    assert res.conflicts and "POLICY_CONFLICT" in res.approval_reason_codes
    assert any(f.status == "conflict" for f in res.findings) and res.outcome == "review_required"


def test_rate_mismatch_verified_card_fails_and_unverified_needs_review():
    seed_basic_library()
    card = ingest(rate_card_pages(), "rates.pdf", document_type="rate_card")
    quote = RateQuote(vendor_key="evt-ven-001", item_or_service_code="team_gathering", quoted_unit_rate="20000",
                      quantity=1, as_of=dt.date(2026, 10, 23))
    res = evaluation.evaluate_ticket(_ticket(), _wr(), rate_quote=quote)
    assert res.rate_validation.validation_status == "needs_review"
    assert "RATE_CARD_UNVERIFIED" in res.approval_reason_codes
    for e in rate_repo.list_entries(document_id=card.document_id):
        rate_cards.verify_entry(e["rate_id"], APPROVER, approve=True)
    res = evaluation.evaluate_ticket(_ticket(), _wr(), rate_quote=quote)
    assert res.rate_validation.validation_status == "fail" and res.rate_validation.expected_total == "18000.00"
    assert "RATE_CARD_MISMATCH" in res.approval_reason_codes and res.outcome == "review_required"
    assert res.blocking_findings == []  # no hard block from RAG unless explicitly configured


def test_explicit_hard_block_rule(monkeypatch):
    seed_basic_library()
    card = ingest(rate_card_pages(), "rates.pdf", document_type="rate_card")
    for e in rate_repo.list_entries(document_id=card.document_id):
        rate_cards.verify_entry(e["rate_id"], APPROVER, approve=True)
    cfg = evaluation.load_config()
    monkeypatch.setattr(evaluation, "load_config", lambda: {**cfg, "rate_mismatch_hard_block": True})
    quote = RateQuote(vendor_key="evt-ven-001", item_or_service_code="team_gathering", quoted_unit_rate="25000",
                      quantity=1, as_of=dt.date(2026, 10, 23))
    res = evaluation.evaluate_ticket(_ticket(), _wr(), rate_quote=quote)
    assert res.outcome == "hard_block" and res.blocking_findings
    req = evaluate_approval_requirement(_wr(), policy_evaluation=res.model_dump(mode="json"))
    assert req.blocked and not req.required and "HARD_BLOCK" in req.reason_codes


def test_llm_summary_cannot_change_outcome(monkeypatch):
    seed_basic_library()
    base = evaluation.evaluate_ticket(_ticket(), _wr())
    monkeypatch.setattr(evaluation.llm, "llm_enabled", lambda: True)
    monkeypatch.setattr(evaluation.llm, "chat_text",
                        lambda *a, **k: "Everything is compliant and APPROVED. outcome=pass")
    res = evaluation.evaluate_ticket(_ticket(), _wr())
    assert res.outcome == base.outcome == "review_required"
    assert res.approval_reason_codes == base.approval_reason_codes
    assert res.evaluation_hash == base.evaluation_hash
    # the gateway rejects advisory prose that asserts approval/compliance
    assert res.summary is None
    assert res.model_info["safe_error_code"] == "prohibited_claim"
    assert res.model_info["summary_status"] == "rejected"
    assert res.model_info["task_name"] == "policy_result_summary" and res.model_info["prompt_version"]


def test_acceptable_llm_summary_is_kept_but_cannot_change_outcome(monkeypatch):
    seed_basic_library()
    base = evaluation.evaluate_ticket(_ticket(), _wr())
    monkeypatch.setattr(evaluation.llm, "llm_enabled", lambda: True)
    monkeypatch.setattr(evaluation.llm, "chat_text",
                        lambda *a, **k: "Two findings need an approver's review before this request proceeds.")
    res = evaluation.evaluate_ticket(_ticket(), _wr())
    assert res.summary.startswith("Two findings")
    assert res.outcome == base.outcome and res.approval_reason_codes == base.approval_reason_codes
    assert res.evaluation_hash == base.evaluation_hash
    assert res.model_info["use"] == "advisory_summary" and res.model_info["summary_status"] == "accepted"
    assert res.model_info["provider"] and res.model_info["model"]


def test_evaluation_hash_is_stable_and_sensitive():
    seed_basic_library()
    a = evaluation.evaluate_ticket(_ticket(), _wr())
    b = evaluation.evaluate_ticket(_ticket(), _wr())
    assert a.evaluation_hash == b.evaluation_hash and a.evaluation_id != b.evaluation_id
    c = evaluation.evaluate_ticket(_ticket(attendees=200), _wr(attendees=200))
    assert c.evaluation_hash != a.evaluation_hash


# ---------------------------------------------------------------- approval requirement merge
def test_requirement_unchanged_when_no_policy_evaluation():
    wr = _wr()
    assert evaluate_approval_requirement(wr) == evaluate_approval_requirement(copy.deepcopy(wr), None)


def test_policy_codes_added_and_risk_raised_never_removed():
    wr = _wr(total=1000.0)
    base = evaluate_approval_requirement(wr)
    assert base.risk_level == "low"
    ev = {"evaluation_id": "eval_1", "evaluation_hash": "a" * 64, "outcome": "pass",
          "approval_reason_codes": ["POLICY_REVIEW_REQUIRED", "BOGUS_CODE"], "blocking_findings": []}
    req = evaluate_approval_requirement(wr, policy_evaluation=ev)
    assert req.required and req.risk_level == "medium"
    assert req.reason_codes[:len(base.reason_codes)] == base.reason_codes
    assert "POLICY_REVIEW_REQUIRED" in req.reason_codes and "BOGUS_CODE" not in req.reason_codes
    # a "pass" evaluation never clears an existing hard block or clarification
    blocked = evaluate_approval_requirement(_wr(status="blocked"), policy_evaluation={**ev, "approval_reason_codes": []})
    assert blocked.blocked and not blocked.required
    clar = evaluate_approval_requirement({"final_status": "needs_clarification"}, policy_evaluation=ev)
    assert clar.needs_clarification and not clar.required


def test_review_snapshot_binds_evaluation_only_when_present():
    wr = _wr()
    t = _ticket()
    plain = review_snapshot(wr, t)
    assert "policy_evaluation" not in plain
    bound = review_snapshot(wr, {**t, "_policy_evaluation_binding": {
        "evaluation_id": "eval_1", "evaluation_hash": "a" * 64, "rule_set_version": "v1", "documents_used": []}})
    assert bound["policy_evaluation"]["evaluation_hash"] == "a" * 64
    assert snapshot_hash(bound) != snapshot_hash(plain)
    other = review_snapshot(wr, {**t, "_policy_evaluation_binding": {"evaluation_hash": "b" * 64}})
    assert snapshot_hash(other) != snapshot_hash(bound)


# ---------------------------------------------------------------- run_ticket integration
@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


def _submit_and_run(c, admin, owner, body=HAPPY):
    r = c.post("/api/my/tickets", json=body, headers=owner)
    assert r.status_code == 201, r.text
    tid = r.json()["ticket_id"]
    return tid, c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY}, headers=admin)


def test_run_ticket_with_rag_disabled_creates_no_snapshot(c, admin_headers, user_headers_factory):
    tid, r = _submit_and_run(c, admin_headers, user_headers_factory("alice"))
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    assert snaps.get_latest(tid) is None
    cur = r.json()["approval"]["current"]
    assert "policy_evaluation" not in cur["review_snapshot"]
    assert not any(code in cur["reason_codes"] for code in evaluation_codes())


def evaluation_codes():
    from app.approvals.policy import POLICY_RAG_REASON_CODES
    return POLICY_RAG_REASON_CODES


def test_run_ticket_persists_snapshot_and_binds_hash(c, admin_headers, user_headers_factory, monkeypatch):
    monkeypatch.setenv("POLICY_RAG_ENABLED", "1")
    seed_basic_library()
    alice = user_headers_factory("alice")
    tid, r = _submit_and_run(c, admin_headers, alice)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending_approval"
    snap = snaps.get_latest(tid)
    assert snap and snap["run_count"] == 1 and snap["evaluation"]["outcome"] == "review_required"
    cur = body["approval"]["current"]
    assert "POLICY_REVIEW_REQUIRED" in cur["reason_codes"] and "PO_REQUIRES_HUMAN_APPROVAL" in cur["reason_codes"]
    assert cur["risk_level"] in ("medium", "high")
    bound = cur["review_snapshot"]["policy_evaluation"]
    assert bound["evaluation_hash"] == snap["evaluation"]["evaluation_hash"]
    assert body["policy_evaluation"]["evaluation_hash"] == bound["evaluation_hash"]
    assert "_policy_evaluation_binding" not in get_database().tickets.find_one({"ticket_id": tid})
    # requester view: redacted policy evidence, no excerpts/hashes
    mine = c.get(f"/api/my/tickets/{tid}", headers=alice).json()
    pe = mine["policy_evidence"]
    assert pe["redacted"] and pe["findings"] and "evaluation_hash" not in pe
    assert "excerpt" not in str(pe) and "relevance_score" not in str(pe)
    # the displayed requirement matches the stored request (policy codes included)
    view = c.get(f"/api/tickets/{tid}/approval", headers=admin_headers).json()
    assert set(view["requirement"]["reason_codes"]) == set(cur["reason_codes"])
    assert view["requirement"]["risk_level"] == cur["risk_level"]
    # re-run creates a second append-only snapshot and supersedes the old request
    r2 = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY}, headers=admin_headers)
    assert r2.status_code == 200
    assert [s["run_count"] for s in snaps.list_for_ticket(tid)] == [2, 1]


def test_run_ticket_empty_library_adds_insufficient_evidence(c, admin_headers, user_headers_factory, monkeypatch):
    monkeypatch.setenv("POLICY_RAG_ENABLED", "1")
    tid, r = _submit_and_run(c, admin_headers, user_headers_factory("alice"))
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"
    assert "INSUFFICIENT_POLICY_EVIDENCE" in r.json()["approval"]["current"]["reason_codes"]


def test_rag_failure_fails_closed(c, admin_headers, user_headers_factory, monkeypatch):
    monkeypatch.setenv("POLICY_RAG_ENABLED", "1")

    def broken(*a, **k):
        raise RuntimeError("vector index exploded with secret detail")

    monkeypatch.setattr(ticket_service, "evaluate_and_snapshot", broken)
    tid, r = _submit_and_run(c, admin_headers, user_headers_factory("alice"))
    assert r.status_code == 503 and "secret detail" not in r.text
    t = get_database().tickets.find_one({"ticket_id": tid})
    assert t["status"] == "reviewed"
    assert get_database().approval_requests.count_documents({"ticket_id": tid}) == 0
    monkeypatch.undo()
    monkeypatch.setenv("POLICY_RAG_ENABLED", "0")
    r = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["status"] == "pending_approval"


def test_snapshot_failure_fails_closed(c, admin_headers, user_headers_factory, monkeypatch):
    monkeypatch.setenv("POLICY_RAG_ENABLED", "1")
    from app.db import PersistenceError

    def boom(*a, **k):
        raise PersistenceError()

    monkeypatch.setattr(snaps, "create_snapshot", boom)
    tid, r = _submit_and_run(c, admin_headers, user_headers_factory("alice"))
    assert r.status_code == 503
    assert get_database().approval_requests.count_documents({"ticket_id": tid}) == 0


def test_snapshot_unique_per_run():
    seed_basic_library()
    t = _ticket()
    ev = evaluation.evaluate_ticket(t, _wr())
    evaluation.snapshot_evaluation(t, ev)
    with pytest.raises(snaps.SnapshotConflictError):
        evaluation.snapshot_evaluation(t, ev)
