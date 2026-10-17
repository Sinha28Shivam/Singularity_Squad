from datetime import date

import pytest

from app.agents.billing import get_delivery, get_invoice, load_history, reconcile
from app.agents.procurement import build_draft_po
from app.agents.vendor_matcher import match_vendors
from app.data_loader import load_json
from app.schemas import EventRequest, PolicyRules


def make_req(**kw):
    # schemas.EventRequest's `date` field shadows the `date` type (annotation resolves to None); bypass validation.
    try:
        return EventRequest(**kw)
    except Exception:
        return EventRequest(**kw)


@pytest.fixture
def rules():
    return PolicyRules(**load_json("policy_rules.json"))


@pytest.fixture
def po(rules):
    req = make_req(request_id="REQ-1001", service="lunch", date=date(2026, 10, 23), attendees=100,
                       budget_limit=100000)
    return build_draft_po(req, match_vendors(req, rules).selected, rules).po


def status_of(rep, name):
    return next(c for c in rep.checks if c.name == name).status


def test_clean_invoice_matches(po, rules):
    rep = reconcile(get_invoice("INV-5001"), po, get_delivery("DEL-7001"), load_history(), rules)
    assert rep.status == "match"
    assert all(c.status == "pass" for c in rep.checks)
    assert rep.expected_total == 76125 and rep.invoiced_total == 76125 and rep.variance == 0
    assert "no payment executed" in rep.recommendation


def test_mismatched_invoice(po, rules):
    rep = reconcile(get_invoice("INV-5002"), po, get_delivery("DEL-7002"), load_history(), rules)
    assert rep.status == "mismatch"
    assert status_of(rep, "line_item:Lunch") == "fail"
    assert status_of(rep, "delivery_check") == "fail"
    assert status_of(rep, "total_variance") == "fail"
    assert status_of(rep, "duplicate_check") == "pass"
    assert status_of(rep, "arithmetic_check") == "pass"
    assert rep.variance == 2100
    assert "720" in next(c for c in rep.checks if c.name == "line_item:Lunch").detail
    assert "95" in next(c for c in rep.checks if c.name == "delivery_check").detail
    assert "Hold payment" in rep.recommendation and "2,000.00" in rep.recommendation
    assert "5 undelivered" in rep.recommendation


def test_duplicate_invoice(po, rules):
    rep = reconcile(get_invoice("INV-4987"), po, get_delivery("DEL-7001"), load_history(), rules)
    assert rep.status == "duplicate"
    dup = next(c for c in rep.checks if c.name == "duplicate_check")
    assert dup.status == "fail" and any(e.source == "policy:FIN-5.4" for e in dup.evidence)


def test_missing_delivery(po, rules):
    rep = reconcile(get_invoice("INV-5001"), po, None, [], rules)
    assert rep.status == "mismatch" and status_of(rep, "delivery_check") == "fail"


def test_unexpected_extra_line_and_arithmetic(po, rules):
    inv = get_invoice("INV-5001").model_copy(deep=True)
    from app.schemas import InvoiceLine
    inv.line_items.append(InvoiceLine(description="Service charge", quantity=1, unit_price=500, line_total=400))
    rep = reconcile(inv, po, get_delivery("DEL-7001"), [], rules)
    assert rep.status == "mismatch"
    assert status_of(rep, "unexpected_line:Service charge") == "fail"
    assert status_of(rep, "arithmetic_check") == "fail"
