"""Rate cards (spec §18.3): extraction from the synthetic menu, verification, Decimal validation."""
import datetime as dt
from decimal import Decimal

import pytest

from app.policy_rag import rate_cards
from app.policy_rag.errors import PolicyRagError
from app.policy_rag.schemas import RateQuote
from app.portal_schemas import UserPublic
from app.repositories import rate_card_repository as repo
from tests.policy_rag_helpers import ingest, ingest_real, rate_card_pages

APPROVER = UserPublic(username="carol", name="Carol", email="c@example.com", role="approver")
USER = UserPublic(username="alice", name="Alice", email="a@example.com", role="user")
AS_OF = dt.date(2026, 10, 20)


def _q(rate="18000", qty=1, code="team_gathering", vendor="evt-ven-001", **kw):
    return RateQuote(vendor_key=vendor, item_or_service_code=code, quoted_unit_rate=rate, quantity=qty,
                     as_of=kw.pop("as_of", AS_OF), **kw)


def _card(**meta):
    meta.setdefault("document_type", "rate_card")
    r = ingest(rate_card_pages(**{k: meta.pop(k) for k in ("record", "vendor", "amount") if k in meta}),
               meta.pop("filename", "vendor_a_rates.pdf"), **meta)
    assert r.ok, r
    return r


def _verify_all(document_id):
    for e in repo.list_entries(document_id=document_id):
        rate_cards.verify_entry(e["rate_id"], APPROVER, approve=True)


def test_extraction_parses_rows_vendor_alias_tax_and_confidence():
    r = _card()
    rows = {e["item_or_service_code"]: e for e in repo.list_entries(document_id=r.document_id)}
    assert set(rows) >= {"team_gathering", "meeting_essentials", "addon_site_visit"}
    tg = rows["team_gathering"]
    assert tg["vendor_key"] == "evt-ven-001" and tg["unit_rate"] == "18000.00" and tg["unit"] == "event"
    assert tg["tax_treatment"] == "excluded" and tg["extraction_confidence"] == "high"
    assert tg["verification_status"] == "needs_review" and tg["source_page"] == 1
    assert rows["meeting_essentials"]["unit_rate"] == "145000.00"  # Indian digit grouping
    assert rows["meeting_essentials"]["extraction_confidence"] == "low"
    assert rows["addon_site_visit"]["unit_rate"] == "4500.00"


def test_real_menu_extracts_all_vendors_namespaced():
    r = ingest_real("Vander Manu.pdf", document_type="rate_card", logical_key="synthetic-event-menu",
                    version_label="SINGULARITY-RAG-MENU")
    assert r.status == "ready"
    rows = repo.list_entries(document_id=r.document_id)
    assert {e["vendor_key"] for e in rows} == {f"evt-ven-00{i}" for i in range(1, 7)}
    assert all(e["verification_status"] == "needs_review" for e in rows)
    large = [e for e in rows if e["item_or_service_code"] == "large_gathering_desk"]
    assert large and large[0]["unit_rate"] == "145000.00" and large[0]["source_page"] == 1
    assert not any(e["vendor_key"].startswith("cat-") for e in rows)


def test_unverified_entry_needs_review_then_pass_equal_below_fail_above():
    r = _card()
    assert rate_cards.validate_quote(_q()).validation_status == "needs_review"
    _verify_all(r.document_id)
    eq = rate_cards.validate_quote(_q("18000", qty=2))
    assert eq.validation_status == "pass" and eq.expected_total == "36000.00" and eq.difference_amount == "0.00"
    assert eq.source_page == 1 and eq.source_document_id == r.document_id and eq.rate_id
    assert rate_cards.validate_quote(_q("17500")).validation_status == "pass"
    within = rate_cards.validate_quote(_q("18000.50"))
    assert within.validation_status == "pass"  # tolerance INR 1.00
    above = rate_cards.validate_quote(_q("18500", qty=3))
    assert above.validation_status == "fail" and above.difference_amount == "1500.00"
    assert above.difference_percentage == "2.78"


def test_decimal_arithmetic_and_rounding():
    r = _card(amount="₹0.10")
    _verify_all(r.document_id)
    res = rate_cards.validate_quote(_q("0.10", qty=3))
    assert res.expected_total == "0.30" and res.quoted_total == "0.30" and res.validation_status == "pass"
    assert Decimal(res.difference_amount) == 0


def test_currency_unit_quantity_and_unknown_item():
    r = _card()
    _verify_all(r.document_id)
    assert rate_cards.validate_quote(_q(currency="USD")).validation_status == "needs_review"
    assert rate_cards.validate_quote(_q(unit="person")).validation_status == "needs_review"
    assert rate_cards.validate_quote(_q(unit="events")).validation_status == "pass"
    unknown = rate_cards.validate_quote(_q(code="does_not_exist"))
    assert unknown.validation_status == "unknown" and unknown.expected_total is None
    other_vendor = rate_cards.validate_quote(_q(vendor="evt-ven-002"))
    assert other_vendor.validation_status == "unknown"
    rows = repo.list_entries(document_id=r.document_id, item_or_service_code="team_gathering")
    rows[0]["minimum_quantity"] = 5
    assert rate_cards.validate_quote(_q(), entries=rows).validation_status == "needs_review"


def test_expired_and_overlapping_rate_cards():
    a = _card(effective_from=dt.date(2026, 1, 1), effective_to=dt.date(2026, 6, 30), filename="old.pdf",
              logical_key="vendor-a-rates-2026h1")
    _verify_all(a.document_id)
    expired = rate_cards.validate_quote(_q())
    assert expired.validation_status == "needs_review" and "effective" in expired.reason
    b = _card(effective_from=dt.date(2026, 7, 1), amount="₹19,000", filename="b.pdf", logical_key="vendor-a-b")
    c = _card(effective_from=dt.date(2026, 7, 1), amount="₹20,000", filename="c.pdf", logical_key="vendor-a-c")
    _verify_all(b.document_id)
    _verify_all(c.document_id)
    res = rate_cards.validate_quote(_q("19000"))
    assert res.validation_status == "conflict" and len(res.candidate_rate_ids) == 2 and res.rate_id is None
    groups = rate_cards.overlapping_verified_conflicts("evt-ven-001", AS_OF)
    assert any(g["item_or_service_code"] == "team_gathering" for g in groups)


def test_tax_not_invented_and_rejected_rows_ignored():
    r = _card()
    rows = repo.list_entries(document_id=r.document_id)
    tg = next(e for e in rows if e["item_or_service_code"] == "team_gathering")
    rate_cards.verify_entry(tg["rate_id"], APPROVER, approve=False)
    assert rate_cards.validate_quote(_q()).validation_status == "unknown"
    pages = [{"page_number": 1, "text": "Record: VEN-002\nVendor\nIllustrative menu\nPackage Scope Sample price\n"
                                         "Basic Plan Coordination only. ₹1,000 / event"}]
    built = rate_cards.build_entries({"document_id": "doc_aaaaaaaa", "content_hash": "f" * 64}, pages)
    assert built and built[0].tax_treatment == "not_verified" and built[0].vendor_key == "evt-ven-002"


def test_verification_authz_and_terminal_state():
    r = _card()
    rid = repo.list_entries(document_id=r.document_id)[0]["rate_id"]
    with pytest.raises(PolicyRagError) as e:
        rate_cards.verify_entry(rid, USER, approve=True)
    assert e.value.status_code == 403
    row = rate_cards.verify_entry(rid, APPROVER, approve=True)
    assert row["verification_status"] == "verified" and row["verified_by"] == "carol"
    with pytest.raises(PolicyRagError) as e:
        rate_cards.verify_entry(rid, APPROVER, approve=False)
    assert e.value.status_code == 409
    with pytest.raises(PolicyRagError) as e:
        rate_cards.verify_entry("rate_deadbeefdeadbeef", APPROVER, approve=True)
    assert e.value.status_code == 404


def test_reextraction_is_idempotent_and_keeps_decisions():
    r = _card()
    rows = repo.list_entries(document_id=r.document_id)
    rate_cards.verify_entry(rows[0]["rate_id"], APPROVER, approve=True)
    from app.policy_rag import ingestion
    ingestion.reprocess(r.document_id, "admin")
    again = repo.list_entries(document_id=r.document_id)
    assert len(again) == len(rows)
    assert next(e for e in again if e["rate_id"] == rows[0]["rate_id"])["verification_status"] == "verified"
