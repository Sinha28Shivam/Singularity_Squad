"""Dummy payment initiator security regressions (SIMULATION ONLY): roles, server-controlled mode/markers,
canonical amounts, safe errors, the narrow tool and the no-network guarantee."""
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.db import get_database
from app.main import app
from app.payments import metrics
from tests import test_no_external_execution as _guard
from tests.test_payment_api import BASE, approved_ticket, body_for, facts, payment_count


no_network = _guard.no_network  # reuse the loopback-only socket guard fixture


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "1")
    metrics.reset()


@pytest.fixture
def admin(admin_headers):
    return admin_headers


@pytest.fixture
def alice(user_headers_factory):
    return user_headers_factory("alice")


@pytest.fixture
def carol(approver_headers_factory):
    return approver_headers_factory("carol")


ROUTES = [("get", "/eligibility/INV-5001?ticket_id=REQ-X1"), ("post", ""), ("get", ""),
          ("get", "/psim_0000000000000000"), ("get", "/psim_0000000000000000/events"),
          ("post", "/psim_0000000000000000/retry"), ("post", "/psim_0000000000000000/reconcile"), ("get", "/metrics")]


def _call(c, method, path, headers=None):
    kw = {"headers": headers} if headers else {}
    if method == "post":
        kw["json"] = {}
    return getattr(c, method)(BASE + path, **kw)


@pytest.mark.parametrize("method,path", ROUTES)
def test_unauthenticated_401_and_user_403(c, alice, method, path):
    assert _call(c, method, path).status_code == 401
    assert _call(c, method, path, alice).status_code == 403


def test_approver_cannot_initiate_retry_or_reconcile(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    r = c.post(BASE, json=body_for(tid, ticket), headers=carol)
    assert r.status_code == 403
    assert c.get(f"{BASE}/eligibility/INV-5001", params={"ticket_id": tid}, headers=carol).status_code == 403
    assert payment_count() == 0
    pid = c.post(BASE, json=body_for(tid, ticket), headers=admin).json()["payment"]["payment_id"]
    assert c.post(f"{BASE}/{pid}/retry", json={"confirmed_by_user": True}, headers=carol).status_code == 403
    assert c.post(f"{BASE}/{pid}/reconcile", headers=carol).status_code == 403


def test_configured_approver_initiator_who_approved_is_blocked(c, admin, alice, carol, monkeypatch):
    """Even if ops configure approvers as initiators, the approving approver can never initiate (SoD)."""
    monkeypatch.setenv("PAYMENT_INITIATOR_ROLES", "admin,approver")
    from app.payments import service
    from app.payments.schemas import PaymentSimulationIn
    from app.payments.service import PaymentServiceError
    from app.portal_schemas import UserPublic

    tid, ticket = approved_ticket(c, admin, alice, carol)
    actor = UserPublic(username="carol", name="Carol", email="carol@example.com", role="approver")
    with pytest.raises(PaymentServiceError) as exc:
        service.create_simulation(PaymentSimulationIn(**body_for(tid, ticket)), actor)
    assert exc.value.status_code == 422
    assert "CALLER_NOT_AUTHORIZED" in [r.code for r in exc.value.reasons]
    assert payment_count() == 0


@pytest.mark.parametrize("extra", [{"money_transferred": True}, {"mode": "LIVE"}, {"provider": "stripe"},
                                   {"provider_url": "https://bank.example.com"}, {"account_number": "1234"},
                                   {"simulator_profile": "normal"}, {"status": "accepted_by_simulator"}])
def test_client_cannot_send_server_controlled_fields(c, admin, alice, carol, extra):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    r = c.post(BASE, json=body_for(tid, ticket, **extra), headers=admin)
    assert r.status_code == 422, r.text
    data = r.json()
    assert data["error"] == "INVALID_REQUEST" and data["mode"] == "SIMULATION" and data["money_transferred"] is False
    assert "Traceback" not in r.text and "pydantic" not in r.text.lower()
    for v in extra.values():
        if isinstance(v, str) and len(v) > 3:
            assert v not in r.text  # input is never echoed
    assert payment_count() == 0


@pytest.mark.parametrize("over", [{"simulation_only": False}, {"confirmed_by_user": False}, {"amount": "-5.00"},
                                  {"amount": "NaN"}, {"amount": "Infinity"}, {"amount": "10.001"},
                                  {"amount": 48500.0}, {"currency": "inr"}, {"idempotency_key": "short"}])
def test_invalid_payloads_rejected(c, admin, alice, carol, over):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    r = c.post(BASE, json=body_for(tid, ticket, **over), headers=admin)
    assert r.status_code == 422, r.text
    assert r.json()["money_transferred"] is False
    assert payment_count() == 0


def test_client_cannot_override_canonical_amount(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    _, amount, _ = facts(ticket)
    for wrong in ("1.00", "999999.00"):
        r = c.post(BASE, json=body_for(tid, ticket, amount=wrong, key=f"amt-key-{wrong}"), headers=admin)
        assert r.status_code == 422 and "AMOUNT_MISMATCH" in [x["code"] for x in r.json()["reasons"]]
    assert payment_count() == 0
    ok = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert ok.status_code == 201 and ok.json()["payment"]["amount"] == amount


def test_tampered_stored_markers_are_never_returned(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    pid = c.post(BASE, json=body_for(tid, ticket), headers=admin).json()["payment"]["payment_id"]
    get_database().payment_simulations.update_one({"payment_id": pid},
                                                  {"$set": {"money_transferred": True, "mode": "LIVE"}})
    got = c.get(f"{BASE}/{pid}", headers=admin).json()
    assert got["mode"] == "SIMULATION" and got["money_transferred"] is False


def test_error_bodies_never_leak_tracebacks(c, admin, alice, carol, monkeypatch):
    from app.repositories import payment_repository as repo

    tid, ticket = approved_ticket(c, admin, alice, carol)

    class _Broken:
        def __getattr__(self, name):
            from pymongo.errors import PyMongoError
            raise PyMongoError("mongodb://secret-user:secret-pass@host internal detail")

    class _BrokenDb:
        payment_simulations = _Broken()
        payment_simulation_events = _Broken()
    monkeypatch.setattr(repo, "_db", lambda db=None: _BrokenDb())
    r = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert r.status_code == 503
    assert "secret" not in r.text and "Traceback" not in r.text and "mongodb://" not in r.text
    assert set(r.json()) >= {"error", "message", "reasons", "mode", "money_transferred"}


# ---------- narrow tool ----------
def _admin_user():
    from app.portal_schemas import UserPublic
    return UserPublic(username="admin", name="Admin", email="admin@example.com", role="admin")


def test_tool_input_is_narrow():
    from app.payments.tool import CreateSimulatedPaymentToolInput
    fields = set(CreateSimulatedPaymentToolInput.model_fields)
    assert fields == {"ticket_id", "invoice_id", "purchase_order_id", "idempotency_key"}
    base = {"ticket_id": "REQ-1", "invoice_id": "INV-5001", "purchase_order_id": "DRAFT-PO-1",
            "idempotency_key": "tool-key-0001"}
    for extra in ({"amount": "1.00"}, {"url": "https://x"}, {"provider": "razorpay"}, {"mode": "live"},
                  {"account_number": "1"}, {"actor": "admin"}, {"money_transferred": True}):
        with pytest.raises(ValidationError):
            CreateSimulatedPaymentToolInput(**base, **extra)


def test_tool_cannot_bypass_approval(c, admin, alice, carol):
    from app.payments.service import PaymentServiceError
    from app.payments.tool import CreateSimulatedPaymentToolInput, create_simulated_payment

    tid, ticket = approved_ticket(c, admin, alice, carol, approve=False)
    po_id = ticket["workflow_result"]["procurement"]["po"]["po_id"]
    ti = CreateSimulatedPaymentToolInput(ticket_id=tid, invoice_id="INV-5001", purchase_order_id=po_id,
                                         idempotency_key="tool-key-0001")
    with pytest.raises(PaymentServiceError) as exc:
        create_simulated_payment(ti, actor=_admin_user(), confirmed_by_user=True)
    assert exc.value.status_code == 422
    with pytest.raises(PaymentServiceError):
        create_simulated_payment(ti, actor=_admin_user(), confirmed_by_user=False)
    from app.portal_schemas import UserPublic
    user = UserPublic(username="alice", name="Alice", email="alice@example.com", role="user")
    with pytest.raises(PaymentServiceError):
        create_simulated_payment(ti, actor=user, confirmed_by_user=True)
    assert payment_count() == 0


def test_tool_uses_canonical_amount(c, admin, alice, carol):
    from app.payments.tool import CreateSimulatedPaymentToolInput, create_simulated_payment

    tid, ticket = approved_ticket(c, admin, alice, carol)
    po_id, amount, _ = facts(ticket)
    out = create_simulated_payment(
        CreateSimulatedPaymentToolInput(ticket_id=tid, invoice_id="INV-5001", purchase_order_id=po_id,
                                        idempotency_key="tool-key-0002"),
        actor=_admin_user(), confirmed_by_user=True)
    assert out.payment.amount == amount and out.money_transferred is False and out.mode == "SIMULATION"
    assert out.payment.status == "accepted_by_simulator"


# ---------- no network during simulation ----------
def test_simulation_makes_no_external_network_calls(c, admin, alice, carol, no_network, monkeypatch):
    monkeypatch.setenv("PAYMENT_SIMULATOR_PROFILE", "transient_failure")
    tid, ticket = approved_ticket(c, admin, alice, carol)
    r = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert r.status_code == 201, r.text
    pid = r.json()["payment"]["payment_id"]
    assert c.post(f"{BASE}/{pid}/retry", json={"confirmed_by_user": True}, headers=admin).status_code == 200
    assert c.get(f"{BASE}/{pid}/events", headers=admin).status_code == 200
    po = get_database().purchase_orders.find_one({"ticket_id": tid})
    assert po["status"] == "draft"
    assert no_network == []


def test_existing_tests_default_simulation_disabled(monkeypatch):
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "0")
    from app.payments.config import load_settings
    s = load_settings()
    assert s.available is False and not s.errors
