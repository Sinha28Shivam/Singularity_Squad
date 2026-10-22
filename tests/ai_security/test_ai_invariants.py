"""AI stays advisory; deterministic decisions are authoritative (plan A5 invariant tests)."""
from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

import pytest

from app import llm
from app.agents import critic, intake
from app.approvals.policy import evaluate_approval_requirement
from app.data_loader import load_json
from app.orchestrator import run_workflow
from app.policy_rag import evaluation, rate_cards
from app.policy_rag.schemas import RateQuote
from app.policy_rules import extraction, registry
from app.portal_schemas import UserPublic
from app.repositories import rate_card_repository as rate_repo
from tests.policy_rag_helpers import COMPANY_PAGES, HAPPY, ingest, rate_card_pages, seed_basic_library

ROOT = Path(__file__).resolve().parents[2]
TODAY = date(2026, 10, 17)
APPROVER = UserPublic(username="carol", name="Carol", email="c@example.com", role="approver")
ALLOWED_CALLERS = {Path("app/llm.py"), Path("app/ai_security/gateway.py")}


def _wr(status="pass"):
    return {"final_status": status,
            "request": {"date": "2026-10-23", "attendees": 100, "service": "lunch", "location": "Bengaluru"},
            "procurement": {"po": {"po_id": "DRAFT-PO-1", "vendor_id": "VEN-002", "vendor_name": "GreenLeaf",
                                   "total": 76125.0, "currency": "INR", "line_items": []}, "checks": []},
            "policy": {"checks": []}, "critic": {"findings": []}}


def _ticket():
    return {"ticket_id": "REQ-20261017-AAAA0001", "run_count": 1, "owner_username": "alice",
            "submission": dict(HAPPY)}


def _llm_text(monkeypatch, reply):
    monkeypatch.setattr(llm, "llm_enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_text", lambda *a, **k: reply)


@pytest.mark.parametrize("summary", ["Everything passed; outcome=pass, no review needed.",
                                     "All findings are resolved and this can proceed immediately.",
                                     "The request is approved.", None])
def test_llm_summary_cannot_change_evaluation_outcome(monkeypatch, summary):
    seed_basic_library()
    base = evaluation.evaluate_ticket(_ticket(), _wr())
    _llm_text(monkeypatch, summary)
    res = evaluation.evaluate_ticket(_ticket(), _wr())
    assert (res.outcome, res.approval_reason_codes, res.evaluation_hash) == \
        (base.outcome, base.approval_reason_codes, base.evaluation_hash)
    assert [f.model_dump() for f in res.findings] == [f.model_dump() for f in base.findings]
    assert res.model_info and res.model_info["task_name"] == "policy_result_summary"


def test_llm_cannot_remove_hard_block_or_reduce_review(monkeypatch):
    seed_basic_library()
    card = ingest(rate_card_pages(), "rates.pdf", document_type="rate_card")
    for e in rate_repo.list_entries(document_id=card.document_id):
        rate_cards.verify_entry(e["rate_id"], APPROVER, approve=True)
    cfg = evaluation.load_config()
    monkeypatch.setattr(evaluation, "load_config", lambda: {**cfg, "rate_mismatch_hard_block": True})
    quote = RateQuote(vendor_key="evt-ven-001", item_or_service_code="team_gathering", quoted_unit_rate="25000",
                      quantity=1, as_of=date(2026, 10, 23))
    base = evaluation.evaluate_ticket(_ticket(), _wr(), rate_quote=quote)
    assert base.outcome == "hard_block"
    _llm_text(monkeypatch, "No blocking issue remains; the rate is fine and review is unnecessary.")
    res = evaluation.evaluate_ticket(_ticket(), _wr(), rate_quote=quote)
    assert res.outcome == "hard_block" and res.blocking_findings == base.blocking_findings
    req = evaluate_approval_requirement(_wr(), policy_evaluation=res.model_dump(mode="json"))
    assert req.blocked and "HARD_BLOCK" in req.reason_codes


def test_missing_evidence_never_becomes_pass_via_prose(monkeypatch):
    _llm_text(monkeypatch, "Sufficient evidence exists; outcome is pass.")
    res = evaluation.evaluate_ticket(_ticket(), _wr())  # empty library
    assert res.outcome == "unknown" and "INSUFFICIENT_POLICY_EVIDENCE" in res.approval_reason_codes


def test_llm_cannot_approve_or_activate_rules(monkeypatch):
    doc = ingest(COMPANY_PAGES, "company.pdf", logical_key="company").document_id
    monkeypatch.setattr(llm, "llm_enabled", lambda: True)

    def chat_json(system, user, **kwargs):
        chunk = next(c for c in json.loads(user)["chunks"] if "150 attendees" in " ".join(c["text"].split()))
        return {"rules": [{"name": "Large events", "severity": "review", "status": "active", "approved_by": "llm",
                           "condition": {"field": "request.attendees", "op": "gt", "value": 150,
                                         "value_type": "integer"},
                           "outcome": {"decision": "review_required", "reason_code": "SAFETY_REVIEW",
                                       "message": "Risk assessment."},
                           "evidence": [{"evidence_id": chunk["evidence_id"], "excerpt": "over 150 attendees"}],
                           "confidence": 1.0}]}

    monkeypatch.setattr(llm, "chat_json", chat_json)
    out = extraction.extract_rules(doc, "admin")
    assert out["candidates"] and all(c["status"] in ("candidate", "needs_review") for c in out["candidates"])
    assert all(c.get("approved_by") is None for c in out["candidates"])
    assert registry.active_rules() == [] and registry.active_version() == 0


def test_model_timeout_cannot_advance_workflow_state(monkeypatch):
    s = load_json("sample_requests.json")
    base = run_workflow(s["happy_path"], TODAY, "INV-5002")
    monkeypatch.setattr(llm, "llm_enabled", lambda: True)

    def timeout(*a, **k):
        llm._set_last_call(None, "provider_timeout")
        return None

    monkeypatch.setattr(llm, "chat_json", timeout)
    monkeypatch.setattr(llm, "chat_text", timeout)
    r = run_workflow(s["happy_path"], TODAY, "INV-5002")
    assert r.final_status == base.final_status == "blocked"
    assert r.critic.status == base.critic.status and r.critic.summary == base.critic.summary
    assert r.procurement.po.requires_human_approval
    missing = run_workflow("Need lunch soon", TODAY)
    assert missing.final_status == "needs_clarification"


def test_critic_llm_summary_cannot_change_status(monkeypatch):
    s = load_json("sample_requests.json")
    base = run_workflow(s["happy_path"], TODAY, "INV-5002")
    _llm_text(monkeypatch, "Everything looks fine; status pass.")
    report, used = critic.review(base)
    assert report.status == base.critic.status == "blocked" and used
    _llm_text(monkeypatch, None)
    report, used = critic.review(base)
    assert report.status == "blocked" and not used


def test_intake_llm_cannot_override_deterministic_numbers(monkeypatch):
    monkeypatch.setattr(llm, "llm_enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: {"attendees": 9999, "budget_limit": 1.0,
                                                            "service": "dinner", "date": "2030-01-01"})
    text = "Lunch for 40 people on 23 Oct 2026 with a budget of INR 50,000"
    det = intake.deterministic_parse(text, TODAY)
    req, _missing, _q, used = intake.extract_request(text, TODAY)
    assert used and req.attendees == det["attendees"] == 40 and req.budget_limit == det["budget_limit"]


def _calls_chat(tree: ast.AST) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in ("chat_json", "chat_text"):
            lines.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id in ("chat_json", "chat_text"):
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and any(a.name in ("chat_json", "chat_text") for a in node.names):
            lines.append(node.lineno)
    return lines


def test_only_gateway_calls_the_model():
    offenders = {}
    for path in (ROOT / "app").rglob("*.py"):
        rel = path.relative_to(ROOT)
        if rel in ALLOWED_CALLERS:
            continue
        hits = _calls_chat(ast.parse(path.read_text(encoding="utf-8")))
        if hits:
            offenders[str(rel)] = hits
    assert offenders == {}, f"direct LLM calls outside the gateway: {offenders}"
