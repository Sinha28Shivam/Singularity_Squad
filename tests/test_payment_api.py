"""Dummy payment initiator API (SIMULATION ONLY): creation, idempotency, concurrency, eligibility blocks, retry,
audit reconciliation and role checks. No money is ever transferred."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.db import get_database
from app.main import app
from app.payments import metrics

TODAY = "2026-10-17"
HAPPY = {"title": "Quarterly team lunch", "event_category": "team_lunch", "service": "lunch",
         "event_date": "2026-10-23", "attendees": 100, "budget_inr": 100000, "location": "Bengaluru",
         "approved_vendor_only": True}
BASE = "/api/payments/simulation"


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


def approved_ticket(c, admin, owner, approver, invoice_id="INV-5001", approve=True):
    r = c.post("/api/my/tickets", json=HAPPY, headers=owner)
    assert r.status_code == 201, r.text
    tid = r.json()["ticket_id"]
    r = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY, "invoice_id": invoice_id}, headers=admin)
    assert r.status_code == 200, r.text
    run = r.json()
    cur = (run.get("approval") or {}).get("current")
    if approve and cur and cur.get("status") == "pending":
        body = {"action": "approve", "idempotency_key": f"appr-{tid}", "request_version": cur["request_version"],
                "workflow_snapshot_hash": cur["workflow_snapshot_hash"]}
        d = c.post(f"/api/approvals/{cur['approval_id']}/decision", json=body, headers=approver)
        assert d.status_code == 200, d.text
    ticket = get_database().tickets.find_one({"ticket_id": tid}, {"_id": 0})
    return tid, ticket


def facts(ticket):
    wr = ticket["workflow_result"]
    po = wr["procurement"]["po"]
    return po["po_id"], f"{float(wr['reconciliation']['invoiced_total']):.2f}", po["currency"]


def body_for(tid, ticket, key="idem-key-0001", invoice_id="INV-5001", **over):
    po_id, amount, currency = facts(ticket)
    b = {"ticket_id": tid, "invoice_id": invoice_id, "purchase_order_id": po_id, "amount": amount,
         "currency": currency, "simulation_only": True, "confirmed_by_user": True, "idempotency_key": key}
    b.update(over)
    return b


def payment_count():
    return get_database().payment_simulations.count_documents({})




# ---------- creation / idempotency ----------
def test_create_returns_201_simulation_markers_and_po_stays_draft(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    r = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert r.status_code == 201, r.text
    data = r.json()
    p = data["payment"]
    assert data["mode"] == p["mode"] == "SIMULATION"
    assert data["money_transferred"] is False and p["money_transferred"] is False
    assert data["replayed"] is False
    assert p["status"] == "accepted_by_simulator" and p["payment_id"].startswith("psim_")
    assert p["provider_reference"].startswith("PAY-SIM-")
    assert p["amount"] == facts(ticket)[1] and p["currency"] == "INR" and p["method"] == "SIMULATED_NEFT"
    assert p["attempts"] == 1 and p["audit_complete"] is True and p["created_by"] == "admin"
    assert "No money was transferred" in data["message"]
    assert "paid" not in data["message"].lower().replace("no money", "")
    assert payment_count() == 1
    stored = get_database().payment_simulations.find_one({}, {"_id": 0})
    assert stored["mode"] == "SIMULATION" and stored["money_transferred"] is False
    assert isinstance(stored["amount"], str) and stored["outbox"] == []
    po = get_database().purchase_orders.find_one({"ticket_id": tid})
    assert po["status"] == "draft"
    ev = c.get(f"{BASE}/{p['payment_id']}/events", headers=admin).json()["events"]
    types = [e["event_type"] for e in ev]
    for t in ("payment_simulation_requested", "payment_simulation_eligibility_passed",
              "payment_simulation_created", "payment_simulation_status_changed"):
        assert t in types, types
    assert all(e["mode"] == "SIMULATION" and e["money_transferred"] is False for e in ev)
    audit = c.get(f"/api/tickets/{tid}/audit", headers=admin).json()["events"]
    lines = [e for e in audit if e["agent"] == "Payments (simulation)"]
    assert len(lines) == 1 and "no money was transferred" in lines[0]["detail"].lower()
    got = c.get(f"{BASE}/{p['payment_id']}", headers=admin)
    assert got.status_code == 200 and got.json()["payment_id"] == p["payment_id"]
    assert "outbox" not in got.json() and "intent_key" not in got.json()


def test_replay_same_key_returns_same_payment(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    first = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert first.status_code == 201, first.text
    again = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert again.status_code == 200, again.text
    assert again.json()["replayed"] is True
    assert again.json()["payment"]["payment_id"] == first.json()["payment"]["payment_id"]
    assert payment_count() == 1
    assert metrics.snapshot()[metrics.REPLAYS] == 1
    types = [e["event_type"] for e in c.get(f"{BASE}/{first.json()['payment']['payment_id']}/events",
                                            headers=admin).json()["events"]]
    assert "payment_simulation_idempotent_replay" in types


def test_same_key_different_payload_conflicts(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    assert c.post(BASE, json=body_for(tid, ticket), headers=admin).status_code == 201
    r = c.post(BASE, json=body_for(tid, ticket, amount="1.00"), headers=admin)
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "IDEMPOTENCY_CONFLICT"
    assert r.json()["mode"] == "SIMULATION" and r.json()["money_transferred"] is False
    assert payment_count() == 1


def test_second_key_same_intent_already_simulated(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    first = c.post(BASE, json=body_for(tid, ticket), headers=admin).json()
    r = c.post(BASE, json=body_for(tid, ticket, key="another-key-0002"), headers=admin)
    assert r.status_code == 409, r.text
    assert r.json()["error"] == "ALREADY_SIMULATED"
    assert r.json()["payment_id"] == first["payment"]["payment_id"]
    assert payment_count() == 1
    elig = c.get(f"{BASE}/eligibility/INV-5001", params={"ticket_id": tid}, headers=admin).json()
    assert elig["eligible"] is False and elig["existing_payment_id"] == first["payment"]["payment_id"]


def _race(n, fn):
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        try:
            return fn(i)
        except Exception as exc:  # noqa: BLE001
            return exc

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_requests_create_one_record(c, admin, alice, carol, same_key):
    from app.payments import service
    from app.payments.schemas import PaymentSimulationIn
    from app.payments.service import PaymentServiceError
    from app.portal_schemas import UserPublic

    tid, ticket = approved_ticket(c, admin, alice, carol)
    actor = UserPublic(username="admin", name="Admin", email="admin@example.com", role="admin")
    n = 8
    results = _race(n, lambda i: service.create_simulation(
        PaymentSimulationIn(**body_for(tid, ticket, key="race-key-0001" if same_key else f"race-key-{i:04d}")),
        actor))
    created = [r for r in results if isinstance(r, tuple) and r[1] == 201]
    replays = [r for r in results if isinstance(r, tuple) and r[1] == 200]
    errors = [r for r in results if isinstance(r, PaymentServiceError)]
    assert len(created) == 1, results
    assert len(created) + len(replays) + len(errors) == n, results
    if same_key:
        assert errors == [] and all(r[0].payment.payment_id == created[0][0].payment.payment_id for r in replays)
    else:
        assert all(e.status_code == 409 and e.error == "ALREADY_SIMULATED" for e in errors), results
    assert payment_count() == 1
    pid = created[0][0].payment.payment_id
    created_events = get_database().payment_simulation_events.count_documents(
        {"payment_id": pid, "event_type": "payment_simulation_created"})
    assert created_events == 1


# ---------- eligibility blocks: no record ----------
def _assert_blocked(r, code):
    assert r.status_code == 422, r.text
    data = r.json()
    assert data["error"] == "PAYMENT_NOT_ELIGIBLE"
    assert code in [x["code"] for x in data["reasons"]], data
    assert data["mode"] == "SIMULATION" and data["money_transferred"] is False
    assert payment_count() == 0


def test_unapproved_ticket_blocked(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol, approve=False)
    assert ticket["status"] != "approved"
    _assert_blocked(c.post(BASE, json=body_for(tid, ticket), headers=admin), "TICKET_NOT_APPROVED")
    blocked = get_database().payment_simulation_events.find_one({"event_type": "payment_simulation_blocked"})
    assert blocked and blocked["payment_id"] is None and "TICKET_NOT_APPROVED" in blocked["reason_codes"]
    assert metrics.snapshot()["payment_simulation_blocked_total"]["TICKET_NOT_APPROVED"] == 1


def test_mismatched_invoice_blocked(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol, invoice_id="INV-5002")
    _assert_blocked(c.post(BASE, json=body_for(tid, ticket, invoice_id="INV-5002"), headers=admin),
                    "INVOICE_MATCH_FAILED")


def test_duplicate_invoice_blocked(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol, invoice_id="INV-4987")
    _assert_blocked(c.post(BASE, json=body_for(tid, ticket, invoice_id="INV-4987"), headers=admin),
                    "DUPLICATE_INVOICE")


def test_other_invoice_on_approved_ticket_blocked(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    _assert_blocked(c.post(BASE, json=body_for(tid, ticket, invoice_id="INV-5002"), headers=admin),
                    "INVOICE_PO_MISMATCH")


def test_amount_mismatch_blocked(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    _assert_blocked(c.post(BASE, json=body_for(tid, ticket, amount="1000.00"), headers=admin), "AMOUNT_MISMATCH")


def test_currency_not_allowed_blocked(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    _assert_blocked(c.post(BASE, json=body_for(tid, ticket, currency="USD"), headers=admin), "CURRENCY_NOT_ALLOWED")


def test_disabled_simulation_returns_503_and_no_record(c, admin, alice, carol, monkeypatch):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "0")
    r = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert r.status_code == 503, r.text
    assert r.json()["error"] == "SIMULATION_DISABLED" and r.json()["money_transferred"] is False
    assert payment_count() == 0


@pytest.mark.parametrize("env", [{"PAYMENT_MODE": "live"}, {"APP_ENV": "production"}])
def test_invalid_mode_or_production_fails_closed(c, admin, alice, carol, monkeypatch, env):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    r = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert r.status_code == 503 and r.json()["error"] == "SIMULATION_DISABLED"
    assert payment_count() == 0


def test_stale_policy_set_blocked(c, admin, alice, carol, monkeypatch):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    get_database().approval_requests.update_many({"ticket_id": tid},
                                                 {"$set": {"review_snapshot.policy_set_version": 3}})
    import app.policy_rules.engine as engine
    monkeypatch.setattr(engine, "active_version", lambda: 4)
    _assert_blocked(c.post(BASE, json=body_for(tid, ticket), headers=admin), "APPROVAL_STALE")


def test_policy_registry_unavailable_fails_closed(c, admin, alice, carol, monkeypatch):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    get_database().approval_requests.update_many({"ticket_id": tid},
                                                 {"$set": {"review_snapshot.policy_set_version": 3}})
    import app.policy_rules.engine as engine

    def boom():
        raise RuntimeError("registry down")
    monkeypatch.setattr(engine, "active_version", boom)
    r = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert r.status_code == 503 and r.json()["error"] == "AUDIT_UNAVAILABLE"
    assert "registry down" not in r.text
    assert payment_count() == 0


# ---------- retry ----------
def test_retry_transient_failure_same_payment(c, admin, alice, carol, monkeypatch):
    monkeypatch.setenv("PAYMENT_SIMULATOR_PROFILE", "transient_failure")
    tid, ticket = approved_ticket(c, admin, alice, carol)
    r = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert r.status_code == 201, r.text
    p = r.json()["payment"]
    assert p["status"] == "failed_simulated" and p["failure_code"] == "SIMULATED_PROVIDER_UNAVAILABLE"
    assert p["provider_reference"] is None and p["money_transferred"] is False
    assert metrics.snapshot()[metrics.FAILURES] == 1
    rr = c.post(f"{BASE}/{p['payment_id']}/retry", json={"confirmed_by_user": True}, headers=admin)
    assert rr.status_code == 200, rr.text
    q = rr.json()["payment"]
    assert q["payment_id"] == p["payment_id"] and q["attempts"] == 2
    assert q["status"] == "accepted_by_simulator" and q["failure_code"] is None and q["audit_complete"] is True
    assert q["idempotency_key_hash"] == p["idempotency_key_hash"]
    assert payment_count() == 1
    types = [e["event_type"] for e in c.get(f"{BASE}/{p['payment_id']}/events", headers=admin).json()["events"]]
    assert "payment_simulation_failed" in types and "payment_simulation_retry_requested" in types
    assert types.count("payment_simulation_status_changed") == 2
    again = c.post(f"{BASE}/{p['payment_id']}/retry", json={"confirmed_by_user": True}, headers=admin)
    assert again.status_code == 409 and again.json()["error"] == "ILLEGAL_TRANSITION"


def test_retry_of_accepted_payment_is_illegal(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    pid = c.post(BASE, json=body_for(tid, ticket), headers=admin).json()["payment"]["payment_id"]
    r = c.post(f"{BASE}/{pid}/retry", json={"confirmed_by_user": True}, headers=admin)
    assert r.status_code == 409 and r.json()["error"] == "ILLEGAL_TRANSITION"
    assert c.post(f"{BASE}/psim_0000000000000000/retry", json={"confirmed_by_user": True},
                  headers=admin).status_code == 404


def test_repository_rejects_illegal_transition():
    from app.repositories import payment_repository as repo
    with pytest.raises(repo.IllegalTransitionError):
        repo.check_transition("accepted_by_simulator", "submitted_to_simulator")
    with pytest.raises(repo.IllegalTransitionError):
        repo.check_transition("submitted_to_simulator", "paid")
    repo.check_transition("failed_simulated", "submitted_to_simulator")


# ---------- audit failure + reconciliation ----------
def test_audit_failure_returns_503_then_reconcile(c, admin, alice, carol, monkeypatch):
    from app.db import PersistenceError
    from app.repositories import payment_repository as repo

    tid, ticket = approved_ticket(c, admin, alice, carol)
    real_insert = repo.insert_event

    def boom(*a, **k):
        raise PersistenceError("simulated audit outage")
    monkeypatch.setattr(repo, "insert_event", boom)
    r = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert r.status_code == 503, r.text
    data = r.json()
    assert data["error"] == "AUDIT_UNAVAILABLE" and data["money_transferred"] is False
    assert "Traceback" not in r.text and "simulated audit outage" not in r.text
    pid = data["payment_id"]
    stored = get_database().payment_simulations.find_one({"payment_id": pid}, {"_id": 0})
    assert stored["audit_complete"] is False and stored["outbox"]
    assert metrics.snapshot()[metrics.RECONCILIATION_FAILURES] >= 1
    # reconcile while still failing -> 503, still incomplete
    assert c.post(f"{BASE}/{pid}/reconcile", headers=admin).status_code == 503
    monkeypatch.setattr(repo, "insert_event", real_insert)
    rec = c.post(f"{BASE}/{pid}/reconcile", headers=admin)
    assert rec.status_code == 200, rec.text
    assert rec.json()["payment"]["audit_complete"] is True
    types = [e["event_type"] for e in c.get(f"{BASE}/{pid}/events", headers=admin).json()["events"]]
    assert {"payment_simulation_requested", "payment_simulation_created",
            "payment_simulation_status_changed"} <= set(types)
    # idempotent: reconciling again writes nothing new
    n = get_database().payment_simulation_events.count_documents({"payment_id": pid})
    assert c.post(f"{BASE}/{pid}/reconcile", headers=admin).status_code == 200
    assert get_database().payment_simulation_events.count_documents({"payment_id": pid}) == n
    assert payment_count() == 1


def test_get_reconciles_pending_audit(c, admin, alice, carol, monkeypatch):
    from app.db import PersistenceError
    from app.repositories import payment_repository as repo

    tid, ticket = approved_ticket(c, admin, alice, carol)
    real_insert = repo.insert_event
    monkeypatch.setattr(repo, "insert_event", lambda *a, **k: (_ for _ in ()).throw(PersistenceError()))
    pid = c.post(BASE, json=body_for(tid, ticket), headers=admin).json()["payment_id"]
    monkeypatch.setattr(repo, "insert_event", real_insert)
    got = c.get(f"{BASE}/{pid}", headers=admin)
    assert got.status_code == 200 and got.json()["audit_complete"] is True


def test_persistence_down_on_create_returns_503(c, admin, alice, carol, monkeypatch):
    from app.db import PersistenceError
    from app.repositories import payment_repository as repo

    tid, ticket = approved_ticket(c, admin, alice, carol)
    monkeypatch.setattr(repo, "insert_simulation", lambda *a, **k: (_ for _ in ()).throw(PersistenceError()))
    r = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert r.status_code == 503 and r.json()["error"] == "AUDIT_UNAVAILABLE"
    assert payment_count() == 0


# ---------- reads / metrics ----------
def test_list_and_metrics(c, admin, alice, carol):
    tid, ticket = approved_ticket(c, admin, alice, carol)
    pid = c.post(BASE, json=body_for(tid, ticket), headers=admin).json()["payment"]["payment_id"]
    lst = c.get(BASE, params={"ticket_id": tid}, headers=carol)
    assert lst.status_code == 200 and [p["payment_id"] for p in lst.json()["payments"]] == [pid]
    assert lst.json()["money_transferred"] is False
    assert c.get(f"{BASE}/{pid}", headers=carol).status_code == 200
    assert c.get(f"{BASE}/{pid}/events", headers=carol).status_code == 200
    m = c.get(f"{BASE}/metrics", headers=admin)
    assert m.status_code == 200
    snap = m.json()
    assert snap["mode"] == "SIMULATION" and snap[metrics.REQUESTS] == 1 and snap[metrics.CREATED] == 1
    assert snap["payment_simulation_latency_ms"]["count"] == 1
    assert c.get(f"{BASE}/metrics", headers=carol).status_code == 403
    assert c.get(f"{BASE}/psim_0000000000000000", headers=admin).status_code == 404
