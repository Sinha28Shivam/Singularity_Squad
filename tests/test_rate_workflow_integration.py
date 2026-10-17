"""Rate-card validation on the NORMAL ticket-run path (Phase 2 §7.3, Phase 3 §11): quotes are built from the
structured draft-PO lines of the current run for vendors that have rate cards, validated with Decimal, merged
into findings/reason codes and the rule engine, and bound into the snapshot. Catering vendors are explicitly
not applicable; missing line fields are listed, never guessed."""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.orchestrator import run_workflow as real_run_workflow
from app.policy_rag import evaluation, rate_cards
from app.policy_rules import registry
from app.portal_schemas import UserPublic
from app.repositories import evidence_snapshot_repository as snaps
from app.repositories import rate_card_repository as rate_repo
from app.schemas import LineItem
from app.services import ticket_service
from tests.policy_rag_helpers import HAPPY, TODAY, ingest, rate_card_pages, seed_basic_library

APPROVER = UserPublic(username="vera", name="Vera", email="v@example.com", role="approver")
EVT = "evt-ven-001"


@pytest.fixture
def c(monkeypatch):
    monkeypatch.setenv("POLICY_RAG_ENABLED", "1")
    with TestClient(app) as client:
        yield client


def _rate_card(verify: bool = True):
    card = ingest(rate_card_pages(), "rates.pdf", document_type="rate_card")
    if verify:
        for e in rate_repo.list_entries(document_id=card.document_id):
            rate_cards.verify_entry(e["rate_id"], APPROVER, approve=True)
    return card


def _event_vendor_po(monkeypatch, *, price=18000.0, unit="event", currency="INR", vendor=EVT, qty=1):
    """The workflow's draft PO names an event vendor with a structured line (vendor mapping fixture)."""
    def fake(text, today=None, invoice_id=None, request_id="REQ-1001"):
        result = real_run_workflow(text, today, invoice_id, request_id=request_id)
        po = result.procurement.po
        po.vendor_id, po.vendor_name, po.currency = vendor, "Vendor A Events", currency
        po.line_items = [LineItem(description="Team Gathering", quantity=qty, unit_price=price,
                                  line_total=price * qty, unit=unit)]
        po.subtotal = po.total = price * qty
        po.tax = 0.0
        return result

    monkeypatch.setattr(ticket_service, "run_workflow", fake)


def _run(c, admin, owner):
    r = c.post("/api/my/tickets", json=HAPPY, headers=owner)
    tid = r.json()["ticket_id"]
    out = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY}, headers=admin)
    assert out.status_code == 200, out.text
    return tid, out.json()


def _rv(tid):
    return snaps.get_latest(tid)["evaluation"]["rate_validations"]


def test_matching_quote_passes_and_is_traceable(c, admin_headers, user_headers_factory, monkeypatch):
    seed_basic_library()
    card = _rate_card()
    _event_vendor_po(monkeypatch, price=18000.0)
    tid, body = _run(c, admin_headers, user_headers_factory("alice"))
    rv = _rv(tid)
    assert len(rv) == 1 and rv[0]["validation_status"] == "pass"
    assert rv[0]["expected_total"] == "18000.00" and rv[0]["source_document_id"] == card.document_id
    assert rv[0]["line_index"] == 0 and rv[0]["vendor_key"] == EVT and rv[0]["rate_id"]
    ev = snaps.get_latest(tid)["evaluation"]
    assert ev["rate_applicable"] is True
    assert card.document_id in {d["document_id"] for d in ev["documents_used"]}
    cur = body["approval"]["current"]
    assert "RATE_CARD_MISMATCH" not in cur["reason_codes"] and "RATE_CARD_UNVERIFIED" not in cur["reason_codes"]
    bound = cur["review_snapshot"]["policy_evaluation"]["rate_validations"]
    assert bound[0]["rate_id"] == rv[0]["rate_id"] and bound[0]["validation_status"] == "pass"
    assert body["status"] == "pending_approval"  # a pass never auto-approves


def test_mismatch_fails_deterministically(c, admin_headers, user_headers_factory, monkeypatch):
    seed_basic_library()
    _rate_card()
    _event_vendor_po(monkeypatch, price=20000.0)
    tid, body = _run(c, admin_headers, user_headers_factory("alice"))
    rv = _rv(tid)[0]
    assert rv["validation_status"] == "fail" and rv["difference_amount"] == "2000.00"
    assert "RATE_CARD_MISMATCH" in body["approval"]["current"]["reason_codes"]


def test_unverified_rate_card_not_authoritative(c, admin_headers, user_headers_factory, monkeypatch):
    seed_basic_library()
    _rate_card(verify=False)
    _event_vendor_po(monkeypatch, price=18000.0)
    tid, body = _run(c, admin_headers, user_headers_factory("alice"))
    assert _rv(tid)[0]["validation_status"] == "needs_review"
    assert "RATE_CARD_UNVERIFIED" in body["approval"]["current"]["reason_codes"]


@pytest.mark.parametrize("kw,reason", [({"currency": "USD"}, "Currency mismatch"),
                                       ({"unit": "person"}, "Unit mismatch")])
def test_currency_and_unit_mismatch_not_equal(c, admin_headers, user_headers_factory, monkeypatch, kw, reason):
    seed_basic_library()
    _rate_card()
    _event_vendor_po(monkeypatch, price=18000.0, **kw)
    tid, body = _run(c, admin_headers, user_headers_factory("alice"))
    rv = _rv(tid)[0]
    assert rv["validation_status"] != "pass" and reason in rv["reason"]
    assert "RATE_CARD_UNVERIFIED" in body["approval"]["current"]["reason_codes"]


def test_missing_line_fields_are_listed_not_guessed(c, admin_headers, user_headers_factory, monkeypatch):
    seed_basic_library()
    _rate_card()
    _event_vendor_po(monkeypatch, price=18000.0, unit=None)
    tid, body = _run(c, admin_headers, user_headers_factory("alice"))
    rv = _rv(tid)[0]
    assert rv["validation_status"] == "unknown" and rv["missing_fields"] == ["unit"]
    assert "INSUFFICIENT_POLICY_EVIDENCE" in body["approval"]["current"]["reason_codes"]


def test_catering_vendor_is_not_applicable(c, admin_headers, user_headers_factory):
    seed_basic_library()
    _rate_card()
    tid, body = _run(c, admin_headers, user_headers_factory("alice"))  # real workflow: catering VEN-00x
    ev = snaps.get_latest(tid)["evaluation"]
    assert ev["rate_validations"] == [] and ev["rate_applicable"] is False
    assert "evt-*" in ev["rate_not_applicable_reason"]
    f = next(f for f in ev["findings"] if f["rule_id"] == "rate_card_applicability")
    assert f["status"] == "not_applicable"


def test_rate_facts_feed_the_rule_engine(c, admin_headers, user_headers_factory, monkeypatch):
    monkeypatch.setenv("POLICY_RULES_ENABLED", "1")
    assert registry.bootstrap_from_seed() >= 1  # legacy-equivalent seed rules (system:bootstrap)
    seed_basic_library()
    _rate_card()
    _event_vendor_po(monkeypatch, price=20000.0)
    tid, body = _run(c, admin_headers, user_headers_factory("alice"))
    eng = snaps.get_latest(tid)["evaluation"]["rule_engine"]
    assert eng["facts_used"]["rate.any_fail"] is True and eng["facts_used"]["rate.applicable"] is True
    assert "RULE-RATE-CARD-MISMATCH@1" in eng["review_rule_refs"]
    assert eng["rate_validations"][0]["validation_status"] == "fail"
    assert "RATE_CARD_MISMATCH" in body["approval"]["current"]["reason_codes"]


def test_rate_card_change_between_runs_is_versioned(c, admin_headers, user_headers_factory, monkeypatch):
    seed_basic_library()
    v1 = ingest(rate_card_pages(), "rates.pdf", document_type="rate_card", logical_key="vendor-a-rates",
                version_label="v1")
    for e in rate_repo.list_entries(document_id=v1.document_id):
        rate_cards.verify_entry(e["rate_id"], APPROVER, approve=True)
    _event_vendor_po(monkeypatch, price=19000.0)
    alice = user_headers_factory("alice")
    tid, _ = _run(c, admin_headers, alice)
    first = _rv(tid)[0]
    assert first["validation_status"] == "fail" and first["rate_card_version"] == "v1"
    v2 = ingest(rate_card_pages(amount="₹19,000"), "rates.pdf", document_type="rate_card",
                logical_key="vendor-a-rates", version_label="v2")
    for e in rate_repo.list_entries(document_id=v2.document_id):
        rate_cards.verify_entry(e["rate_id"], APPROVER, approve=True)
    r2 = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY}, headers=admin_headers)
    assert r2.status_code == 200
    second = snaps.get_for_run(tid, 2)["evaluation"]["rate_validations"][0]
    assert second["validation_status"] == "pass" and second["rate_card_version"] == "v2"
    assert second["rate_id"] != first["rate_id"]
    assert snaps.get_for_run(tid, 1)["evaluation"]["rate_validations"][0] == first  # old run untouched


def test_evaluate_ticket_rate_quotes_list_and_backward_compat():
    seed_basic_library()
    _rate_card()
    from app.policy_rag.schemas import RateQuote
    q = lambda price: RateQuote(vendor_key=EVT, item_or_service_code="team_gathering",  # noqa: E731
                                quoted_unit_rate=price, quantity=1, as_of=dt.date(2026, 10, 23))
    t = {"ticket_id": "REQ-20261017-AAAA0001", "run_count": 1, "submission": HAPPY}
    wr = {"final_status": "pass", "request": {"date": "2026-10-23", "attendees": 100},
          "procurement": {"po": {"po_id": "P", "vendor_id": EVT, "total": 1.0, "line_items": []}}}
    multi = evaluation.evaluate_ticket(t, wr, rate_quotes=[q("18000"), q("25000")])
    assert [r.validation_status for r in multi.rate_validations] == ["pass", "fail"]
    assert multi.rate_validation.validation_status == "fail"  # first non-pass is the summary
    single = evaluation.evaluate_ticket(t, wr, rate_quote=q("18000"))
    assert single.rate_validation.validation_status == "pass" and len(single.rate_validations) == 1
