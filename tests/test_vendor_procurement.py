from datetime import date

import pytest

from app.agents.procurement import build_draft_po, calculate_cost, required_approver
from app.agents.vendor_matcher import load_vendors, match_vendors
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


def happy_req(**kw):
    base = dict(request_id="REQ-1001", service="lunch", date=date(2026, 10, 23), attendees=100, budget_limit=100000)
    base.update(kw)
    return make_req(**base)


def test_happy_path_vendor_match(rules):
    res = match_vendors(happy_req(), rules)
    ev = {e.vendor_id: e for e in res.evaluations}
    assert not ev["VEN-001"].eligible and any("unavailable" in r for r in ev["VEN-001"].reasons)
    assert not ev["VEN-003"].eligible and any("approved" in r for r in ev["VEN-003"].reasons)
    assert not ev["VEN-004"].eligible and any("80" in r for r in ev["VEN-004"].reasons)
    assert not ev["VEN-005"].eligible and any("950" in r and "900" in r for r in ev["VEN-005"].reasons)
    assert ev["VEN-002"].eligible
    assert res.selected.vendor_id == "VEN-002"
    assert res.replanned is True
    assert "Saffron" in res.explanation and "GreenLeaf" in res.explanation and "PROC-3.2" in res.explanation
    assert all(e.estimated_total is not None for e in res.evaluations)
    assert ev["VEN-002"].estimated_total == 76125.0


def test_happy_path_po(rules):
    req = happy_req()
    vendor = match_vendors(req, rules).selected
    res = build_draft_po(req, vendor, rules)
    po = res.po
    assert po is not None and po.po_id == "DRAFT-PO-1001" and po.status == "draft"
    assert [(li.description, li.quantity, li.unit_price, li.line_total) for li in po.line_items] == [
        ("Lunch", 100, 700, 70000), ("Delivery & setup", 1, 2500, 2500)]
    assert (po.subtotal, po.tax, po.total) == (72500, 3625, 76125)
    assert po.required_approver == "Department Head + Finance Business Partner"
    assert po.requires_human_approval is True
    assert {c.name: c.status for c in res.checks} == {"vendor_selected": "pass", "budget": "pass", "approval_tier": "pass"}
    assert "Lunch: 100 x 700.00 = 70,000.00" in res.calculation_steps


def test_calculate_cost_omits_zero_fee():
    v = next(v for v in load_vendors() if v.vendor_id == "VEN-003")
    items, sub, tax, total, _ = calculate_cost(v, 10)
    assert len(items) == 1 and sub == 4800 and tax == 240 and total == 5040


def test_over_budget(rules):
    req = happy_req(attendees=120, budget_limit=60000)
    vendor = match_vendors(req, rules).selected
    assert vendor is not None
    res = build_draft_po(req, vendor, rules)
    assert res.po is None
    budget = next(c for c in res.checks if c.name == "budget")
    assert budget.status == "fail" and "exceeds" in budget.detail
    # 120*700 + 2500 = 86500; *1.05 = 90825; over 30825
    assert "30,825.00" in budget.detail


def test_no_eligible_vendor(rules):
    req = happy_req(date=date(2026, 12, 25))
    res = match_vendors(req, rules)
    assert res.selected is None and not any(e.eligible for e in res.evaluations)
    assert "No eligible vendor" in res.explanation
    proc = build_draft_po(req, None, rules)
    assert proc.po is None and proc.checks[0].status == "fail"


@pytest.mark.parametrize("amount,approver", [
    (50000, "Team Manager"),
    (50000.01, "Department Head + Finance Business Partner"),
    (150000, "Department Head + Finance Business Partner"),
    (150000.01, "CFO"),
])
def test_approver_tiers(rules, amount, approver):
    assert required_approver(amount, rules) == approver
