"""Deterministic decision explanations: derived from stored checks only, consistent with the outcomes."""
import pytest
from fastapi.testclient import TestClient

from app.explanations import build_explanations
from app.main import app
from tests.test_payment_api import approved_ticket


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def actors(admin_headers, user_headers_factory, approver_headers_factory):
    return admin_headers, user_headers_factory("alice"), approver_headers_factory("carol")


def _by_topic(view):
    return {e["topic"]: e for e in view["explanations"]}


def test_admin_view_explains_matched_run(c, actors):
    admin, alice, carol = actors
    tid, _ = approved_ticket(c, admin, alice, carol, invoice_id="INV-5001")
    ex = _by_topic(c.get(f"/api/admin/tickets/{tid}", headers=admin).json())
    assert set(ex) == {"policy", "vendor", "cost", "invoice", "final"}
    assert ex["invoice"]["outcome"] == "pass" and "INV-5001" in ex["invoice"]["summary"]
    assert ex["vendor"]["outcome"] in {"pass", "warning"}
    selected = [f for f in ex["vendor"]["factors"] if "(selected)" in f["label"]]
    assert len(selected) == 1 and selected[0]["status"] == "pass"
    assert ex["vendor"]["factors"][0] is not None and "(selected)" in ex["vendor"]["factors"][0]["label"]
    assert ex["cost"]["outcome"] == "pass" and "INR 76,125.00" in ex["cost"]["summary"]
    assert any(f["evidence"] == ["calc"] for f in ex["cost"]["factors"])


def test_mismatch_lists_failed_checks_first_with_evidence(c, actors):
    admin, alice, carol = actors
    tid, _ = approved_ticket(c, admin, alice, carol, invoice_id=None)
    c.post(f"/api/admin/tickets/{tid}/invoice-match", json={"invoice_id": "INV-5002"}, headers=admin)
    inv = _by_topic(c.get(f"/api/admin/tickets/{tid}", headers=admin).json())["invoice"]
    assert inv["outcome"] == "fail"
    assert inv["factors"][0]["status"] == "fail"
    assert any("delivered 95" in f["detail"] for f in inv["factors"])
    assert all(f["evidence"] for f in inv["factors"] if f["status"] == "fail")


def test_exception_shown_in_invoice_explanation():
    doc = {"workflow_result": {"final_status": "pass"},
           "invoice_match": {"invoice_id": "INV-5001", "status": "match", "post_approval": True, "checks": [],
                             "invoiced_total": 10, "expected_total": 10},
           "invoice_exception": {"invoice_id": "INV-5001", "state": "on_hold", "reason": "Waiting on delivery"}}
    inv = {e.topic: e for e in build_explanations(doc)}["invoice"]
    assert inv.outcome == "warning" and inv.factors[0].label == "Invoice on hold"


def test_no_run_means_no_explanations():
    assert build_explanations({"workflow_result": None}) == []
    ex = {e.topic: e for e in build_explanations({"workflow_result": {"final_status": "needs_clarification"}})}
    assert ex["policy"].outcome == "not_run" and ex["final"].outcome == "needs_clarification"
