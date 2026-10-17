"""Payment-simulation eligibility gate: every reason code, happy path, staleness and fail-closed reads."""
import copy
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db import PersistenceError, get_database
from app.main import app
from app.payments import eligibility as elig
from app.payments.config import load_settings
from app.payments.eligibility import evaluate_eligibility
from app.portal_schemas import UserPublic

TODAY = "2026-10-17"
HAPPY = {"title": "Quarterly team lunch", "event_category": "team_lunch", "service": "lunch",
         "event_date": "2026-10-23", "attendees": 100, "budget_inr": 100000, "location": "Bengaluru",
         "approved_vendor_only": True}
ADMIN = UserPublic(username="admin", name="Admin", email="admin@example.com", role="admin")
OPS = UserPublic(username="opsadmin", name="Ops", email="ops@example.com", role="admin")
USER = UserPublic(username="alice", name="Alice", email="alice@example.com", role="user")
CAROL = UserPublic(username="carol", name="Carol", email="carol@example.com", role="approver")


@pytest.fixture(autouse=True)
def _payment_env(monkeypatch):
    monkeypatch.setenv("PAYMENT_MODE", "simulation")
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "1")
    monkeypatch.setenv("PAYMENT_ALLOWED_CURRENCIES", "INR")
    monkeypatch.setenv("PAYMENT_SIMULATOR_PROFILE", "normal")
    for var in ("PAYMENT_INITIATOR_ROLES", "APP_ENV", "ENVIRONMENT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def actors(admin_headers, user_headers_factory, approver_headers_factory):
    return {"admin": admin_headers, "alice": user_headers_factory("alice"), "carol": approver_headers_factory("carol")}


def _submit_and_run(c, actors, body=HAPPY, invoice_id="INV-5001"):
    r = c.post("/api/my/tickets", json=body, headers=actors["alice"])
    assert r.status_code == 201, r.text
    tid = r.json()["ticket_id"]
    run = {"today": TODAY}
    if invoice_id:
        run["invoice_id"] = invoice_id
    r = c.post(f"/api/admin/tickets/{tid}/run", json=run, headers=actors["admin"])
    assert r.status_code == 200, r.text
    return tid, r.json()


def _approve(c, actors, cur, key="key-0000-0001"):
    r = c.post(f"/api/approvals/{cur['approval_id']}/decision",
               json={"action": "approve", "idempotency_key": key, "request_version": cur["request_version"],
                     "workflow_snapshot_hash": cur["workflow_snapshot_hash"]}, headers=actors["carol"])
    assert r.status_code == 200, r.text


def _approved_ticket(c, actors, **kw):
    tid, out = _submit_and_run(c, actors, **kw)
    assert out["status"] == "pending_approval", out["status"]
    _approve(c, actors, out["approval"]["current"])
    return tid


def _ticket(tid):
    return get_database().tickets.find_one({"ticket_id": tid}, {"_id": 0})


def _set_ticket(tid, **fields):
    get_database().tickets.update_one({"ticket_id": tid}, {"$set": fields})


def _set_approval(tid, **fields):
    get_database().approval_requests.update_one({"ticket_id": tid}, {"$set": fields})


def _codes(decision):
    return {r.code for r in decision.view.reasons}


def _po_id(tid):
    return _ticket(tid)["workflow_result"]["procurement"]["po"]["po_id"]


@pytest.fixture
def approved(c, actors):
    return _approved_ticket(c, actors)


# ---------- happy path ----------

def test_happy_path_eligible_with_canonical_values(approved):
    tid = approved
    d = evaluate_eligibility(tid, "INV-5001", ADMIN, purchase_order_id=_po_id(tid), amount="76125.00",
                             currency="INR")
    assert d.eligible, d.view.reasons
    v = d.view
    assert v.mode == "SIMULATION" and v.money_transferred is False
    assert v.amount == "76125.00" and d.canonical_amount == Decimal("76125.00")
    assert isinstance(d.canonical_amount, Decimal)
    assert v.currency == "INR" and v.invoice_match_status == "match"
    assert v.purchase_order_id == _po_id(tid) and v.invoice_id == "INV-5001"
    assert v.approval_status == "approved" and v.approved_by == "carol" and v.approval_request_version == 1
    assert v.approval_id and v.eligibility_fingerprint.startswith("sha256:")
    assert d.approval["approval_id"] == v.approval_id and d.ticket["ticket_id"] == tid


def test_happy_path_without_client_values_and_fingerprint_stable(approved):
    d1 = evaluate_eligibility(approved, "INV-5001", ADMIN)
    d2 = evaluate_eligibility(approved, "INV-5001", OPS)
    assert d1.eligible and d2.eligible
    assert d1.view.eligibility_fingerprint == d2.view.eligibility_fingerprint


def test_fingerprint_changes_with_canonical_facts(approved):
    before = evaluate_eligibility(approved, "INV-5001", ADMIN).view.eligibility_fingerprint
    _set_ticket(approved, run_count=5)
    assert evaluate_eligibility(approved, "INV-5001", ADMIN).view.eligibility_fingerprint != before


# ---------- config / caller ----------

def test_simulation_disabled(approved, monkeypatch):
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "0")
    d = evaluate_eligibility(approved, "INV-5001", ADMIN)
    assert not d.eligible and _codes(d) == {"SIMULATION_DISABLED"}


def test_simulation_disabled_when_mode_unset_or_production(approved, monkeypatch):
    monkeypatch.delenv("PAYMENT_MODE")
    assert "SIMULATION_DISABLED" in _codes(evaluate_eligibility(approved, "INV-5001", ADMIN))
    monkeypatch.setenv("PAYMENT_MODE", "simulation")
    monkeypatch.setenv("APP_ENV", "production")
    assert "SIMULATION_DISABLED" in _codes(evaluate_eligibility(approved, "INV-5001", ADMIN))


def test_settings_unreadable_is_disabled(approved, monkeypatch):
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(elig, "load_settings", boom)
    d = evaluate_eligibility(approved, "INV-5001", ADMIN)
    assert {"SIMULATION_DISABLED", "CALLER_NOT_AUTHORIZED"} <= _codes(d)


def test_explicit_settings_are_used(approved, monkeypatch):
    s = load_settings()
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "0")
    assert evaluate_eligibility(approved, "INV-5001", ADMIN, settings=s).eligible


@pytest.mark.parametrize("actor", [USER, CAROL])
def test_non_initiator_roles_not_authorized(approved, actor):
    d = evaluate_eligibility(approved, "INV-5001", actor)
    assert "CALLER_NOT_AUTHORIZED" in _codes(d) and not d.eligible


def test_user_never_authorized_even_if_configured(approved, monkeypatch):
    monkeypatch.setenv("PAYMENT_INITIATOR_ROLES", "admin,user")
    assert "CALLER_NOT_AUTHORIZED" in _codes(evaluate_eligibility(approved, "INV-5001", USER))


def test_approving_approver_cannot_initiate(approved, monkeypatch):
    monkeypatch.setenv("PAYMENT_INITIATOR_ROLES", "admin,approver")
    d = evaluate_eligibility(approved, "INV-5001", CAROL)
    assert _codes(d) == {"CALLER_NOT_AUTHORIZED"}
    other = UserPublic(username="dave", name="Dave", email="d@example.com", role="approver")
    assert evaluate_eligibility(approved, "INV-5001", other).eligible


def test_admin_same_username_as_approver_blocked(approved):
    impostor = UserPublic(username="carol", name="C", email="c2@example.com", role="admin")
    assert _codes(evaluate_eligibility(approved, "INV-5001", impostor)) == {"CALLER_NOT_AUTHORIZED"}


# ---------- client-supplied values ----------

@pytest.mark.parametrize("amount", ["76125.01", "1.00", "76124.99", "NaN", "-76125.00", "0"])
def test_amount_mismatch(approved, amount):
    d = evaluate_eligibility(approved, "INV-5001", ADMIN, amount=amount)
    assert _codes(d) == {"AMOUNT_MISMATCH"}
    assert d.view.amount == "76125.00"   # canonical, never the client's


def test_amount_equivalent_representation_ok(approved):
    assert evaluate_eligibility(approved, "INV-5001", ADMIN, amount="76125").eligible


def test_currency_mismatch(approved):
    assert _codes(evaluate_eligibility(approved, "INV-5001", ADMIN, currency="USD")) == {"CURRENCY_NOT_ALLOWED"}


def test_currency_not_in_allowlist(approved, monkeypatch):
    monkeypatch.setenv("PAYMENT_ALLOWED_CURRENCIES", "USD")
    d = evaluate_eligibility(approved, "INV-5001", ADMIN, currency="INR")
    assert _codes(d) == {"CURRENCY_NOT_ALLOWED"}


def test_po_mismatch(approved):
    d = evaluate_eligibility(approved, "INV-5001", ADMIN, purchase_order_id="DRAFT-PO-OTHER-1")
    assert _codes(d) == {"INVOICE_PO_MISMATCH"}


def test_invoice_not_reconciled_on_ticket(approved):
    d = evaluate_eligibility(approved, "INV-5002", ADMIN)
    assert "INVOICE_PO_MISMATCH" in _codes(d) and "APPROVAL_STALE" in _codes(d) and not d.eligible


def test_invoice_not_found(approved):
    d = evaluate_eligibility(approved, "INV-9999", ADMIN)
    assert {"INVOICE_NOT_FOUND", "INVOICE_PO_MISMATCH"} <= _codes(d)


# ---------- ticket / reconciliation / PO state ----------

def test_reconciliation_statuses(approved):
    t = _ticket(approved)
    wr = copy.deepcopy(t["workflow_result"])
    wr["reconciliation"]["status"] = "duplicate"
    _set_ticket(approved, workflow_result=wr)
    assert {"DUPLICATE_INVOICE", "APPROVAL_STALE"} <= _codes(evaluate_eligibility(approved, "INV-5001", ADMIN))
    wr["reconciliation"]["status"] = "mismatch"
    _set_ticket(approved, workflow_result=wr)
    assert {"INVOICE_MATCH_FAILED", "APPROVAL_STALE"} <= _codes(evaluate_eligibility(approved, "INV-5001", ADMIN))


def test_invoice_in_payment_history_is_duplicate(approved):
    t = _ticket(approved)
    wr = copy.deepcopy(t["workflow_result"])
    wr["reconciliation"]["invoice_id"] = "INV-4987"   # INV-4987 already paid per invoice_history.json
    _set_ticket(approved, workflow_result=wr)
    d = evaluate_eligibility(approved, "INV-4987", ADMIN)
    assert "DUPLICATE_INVOICE" in _codes(d)


def test_reconciliation_required_without_invoice(c, actors):
    tid, out = _submit_and_run(c, actors, invoice_id=None)
    if out["status"] == "pending_approval":
        _approve(c, actors, out["approval"]["current"])
    d = evaluate_eligibility(tid, "INV-5001", ADMIN)
    assert "RECONCILIATION_REQUIRED" in _codes(d) and not d.eligible


def test_po_not_found(approved):
    t = _ticket(approved)
    wr = copy.deepcopy(t["workflow_result"])
    wr["procurement"]["po"] = None
    _set_ticket(approved, workflow_result=wr)
    d = evaluate_eligibility(approved, "INV-5001", ADMIN)
    assert {"PO_NOT_FOUND", "APPROVAL_STALE"} <= _codes(d) and not d.eligible


def test_invoice_total_differs_from_po_total(approved):
    t = _ticket(approved)
    wr = copy.deepcopy(t["workflow_result"])
    wr["reconciliation"]["invoiced_total"] = 80000.0
    _set_ticket(approved, workflow_result=wr)
    d = evaluate_eligibility(approved, "INV-5001", ADMIN)
    assert _codes(d) == {"AMOUNT_MISMATCH"}
    assert d.view.amount == "80000.00"


def test_policy_block(approved):
    t = _ticket(approved)
    wr = copy.deepcopy(t["workflow_result"])
    wr["final_status"] = "blocked"
    _set_ticket(approved, workflow_result=wr)
    assert "POLICY_BLOCK" in _codes(evaluate_eligibility(approved, "INV-5001", ADMIN))


def test_policy_engine_hard_block_from_evidence_snapshot(approved, monkeypatch):
    import app.approvals.service as approval_service
    monkeypatch.setattr(approval_service, "_policy_evaluation_for",
                        lambda ticket: {"approval_reason_codes": [], "blocking_findings": ["FIN-9.9"]})
    assert _codes(evaluate_eligibility(approved, "INV-5001", ADMIN)) == {"POLICY_BLOCK"}


def test_unverifiable_policy_evaluation_blocks(approved, monkeypatch):
    import app.approvals.service as approval_service
    monkeypatch.setattr(approval_service, "_policy_evaluation_for", lambda ticket: "garbage")
    assert _codes(evaluate_eligibility(approved, "INV-5001", ADMIN)) == {"POLICY_BLOCK"}


@pytest.mark.parametrize("change", [{"final_status": "needs_clarification"}, {"missing_fields": ["date"]},
                                    {"clarification_questions": ["When?"]}])
def test_clarification_required(approved, change):
    wr = copy.deepcopy(_ticket(approved)["workflow_result"])
    wr.update(change)
    _set_ticket(approved, workflow_result=wr)
    assert "CLARIFICATION_REQUIRED" in _codes(evaluate_eligibility(approved, "INV-5001", ADMIN))


def test_pending_ticket_not_approved(c, actors):
    tid, out = _submit_and_run(c, actors)
    assert out["status"] == "pending_approval"
    d = evaluate_eligibility(tid, "INV-5001", ADMIN)
    assert {"TICKET_NOT_APPROVED", "APPROVAL_MISSING"} <= _codes(d)
    assert d.view.approval_status == "pending"


def test_unknown_ticket(c):
    d = evaluate_eligibility("REQ-NOPE", "INV-5001", ADMIN)
    assert {"TICKET_NOT_APPROVED", "APPROVAL_MISSING", "PO_NOT_FOUND", "RECONCILIATION_REQUIRED"} <= _codes(d)
    assert d.ticket is None and d.approval is None and d.canonical_amount is None


def test_approval_missing_when_no_approval_doc(approved):
    get_database().approval_requests.delete_many({"ticket_id": approved})
    assert _codes(evaluate_eligibility(approved, "INV-5001", ADMIN)) == {"APPROVAL_MISSING"}


def test_approval_rejected_status(approved):
    _set_approval(approved, status="rejected")
    assert "APPROVAL_MISSING" in _codes(evaluate_eligibility(approved, "INV-5001", ADMIN))


def test_approval_decided_by_non_approver(approved):
    doc = get_database().approval_requests.find_one({"ticket_id": approved})
    decisions = doc["decisions"]
    decisions[-1]["actor_role"] = "admin"
    _set_approval(approved, decisions=decisions)
    assert "APPROVAL_MISSING" in _codes(evaluate_eligibility(approved, "INV-5001", ADMIN))


# ---------- stale approval ----------

def test_stale_when_run_count_changes(approved):
    _set_ticket(approved, run_count=2)
    assert _codes(evaluate_eligibility(approved, "INV-5001", ADMIN)) == {"APPROVAL_STALE"}


def test_stale_when_po_changes(approved):
    wr = copy.deepcopy(_ticket(approved)["workflow_result"])
    wr["procurement"]["po"]["line_items"][0]["unit_price"] = 650.0
    _set_ticket(approved, workflow_result=wr)
    assert _codes(evaluate_eligibility(approved, "INV-5001", ADMIN)) == {"APPROVAL_STALE"}


def test_stale_when_po_id_changes(approved):
    wr = copy.deepcopy(_ticket(approved)["workflow_result"])
    wr["procurement"]["po"]["po_id"] = "DRAFT-PO-CHANGED-1"
    _set_ticket(approved, workflow_result=wr)
    d = evaluate_eligibility(approved, "INV-5001", ADMIN, purchase_order_id="DRAFT-PO-CHANGED-1")
    assert {"APPROVAL_STALE", "INVOICE_PO_MISMATCH"} <= _codes(d)


def test_stale_when_po_currency_changes(approved, monkeypatch):
    monkeypatch.setenv("PAYMENT_ALLOWED_CURRENCIES", "INR,USD")
    wr = copy.deepcopy(_ticket(approved)["workflow_result"])
    wr["procurement"]["po"]["currency"] = "USD"
    _set_ticket(approved, workflow_result=wr)
    assert _codes(evaluate_eligibility(approved, "INV-5001", ADMIN)) == {"APPROVAL_STALE"}


def test_stale_when_snapshot_po_total_differs(approved):
    doc = get_database().approval_requests.find_one({"ticket_id": approved})
    snap = doc["review_snapshot"]
    snap["po"]["total"] = 70000.0
    _set_approval(approved, review_snapshot=snap)
    assert {"APPROVAL_STALE", "AMOUNT_MISMATCH"} <= _codes(evaluate_eligibility(approved, "INV-5001", ADMIN))


def test_float_noise_in_po_is_not_stale(approved):
    wr = copy.deepcopy(_ticket(approved)["workflow_result"])
    wr["procurement"]["po"]["total"] = 76125     # int vs float representation of the same amount
    wr["procurement"]["po"]["line_items"][0]["quantity"] = 100
    _set_ticket(approved, workflow_result=wr)
    assert evaluate_eligibility(approved, "INV-5001", ADMIN).eligible


def _bind_policy_set(tid, version):
    doc = get_database().approval_requests.find_one({"ticket_id": tid})
    snap = doc["review_snapshot"]
    snap["policy_evaluation"] = {"rule_engine": {"policy_set_version": version}}
    snap["policy_set_version"] = version
    _set_approval(tid, review_snapshot=snap)


def test_stale_when_policy_set_changes(approved, monkeypatch):
    import app.policy_rules.engine as engine
    _bind_policy_set(approved, 3)
    monkeypatch.setattr(engine, "active_version", lambda: 3)
    d = evaluate_eligibility(approved, "INV-5001", ADMIN)
    assert d.eligible and d.view.policy_set_version == 3
    monkeypatch.setattr(engine, "active_version", lambda: 4)
    assert _codes(evaluate_eligibility(approved, "INV-5001", ADMIN)) == {"APPROVAL_STALE"}


def test_unbound_policy_set_skips_registry(approved, monkeypatch):
    import app.policy_rules.engine as engine

    def boom():
        raise RuntimeError("registry down")
    monkeypatch.setattr(engine, "active_version", boom)
    d = evaluate_eligibility(approved, "INV-5001", ADMIN)
    assert d.eligible and d.view.policy_set_version is None


def test_registry_unavailable_fails_closed(approved, monkeypatch):
    import app.policy_rules.engine as engine
    _bind_policy_set(approved, 3)

    def boom():
        raise RuntimeError("registry down")
    monkeypatch.setattr(engine, "active_version", boom)
    with pytest.raises(PersistenceError):
        evaluate_eligibility(approved, "INV-5001", ADMIN)


# ---------- persistence fail-closed ----------

def test_ticket_read_failure_raises(approved, monkeypatch):
    import app.repositories.ticket_repository as tr

    def boom(*a, **k):
        raise PersistenceError()
    monkeypatch.setattr(tr, "get_ticket", boom)
    with pytest.raises(PersistenceError):
        evaluate_eligibility(approved, "INV-5001", ADMIN)


def test_approval_read_failure_raises(approved, monkeypatch):
    import app.repositories.approval_repository as ar

    def boom(*a, **k):
        raise RuntimeError("socket closed")
    monkeypatch.setattr(ar, "get_current_for_ticket", boom)
    with pytest.raises(PersistenceError):
        evaluate_eligibility(approved, "INV-5001", ADMIN)


def test_evidence_snapshot_read_failure_raises(approved, monkeypatch):
    import app.approvals.service as approval_service

    def boom(ticket):
        raise PersistenceError()
    monkeypatch.setattr(approval_service, "_policy_evaluation_for", boom)
    with pytest.raises(PersistenceError):
        evaluate_eligibility(approved, "INV-5001", ADMIN)


def test_all_reasons_collected(approved, monkeypatch):
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "0")
    _set_ticket(approved, run_count=9)
    d = evaluate_eligibility(approved, "INV-5001", USER, purchase_order_id="DRAFT-PO-X-1", amount="1.00",
                             currency="USD")
    assert {"SIMULATION_DISABLED", "CALLER_NOT_AUTHORIZED", "APPROVAL_STALE", "INVOICE_PO_MISMATCH",
            "AMOUNT_MISMATCH", "CURRENCY_NOT_ALLOWED"} <= _codes(d)
    assert len(d.view.reasons) == len(_codes(d))   # no duplicate codes
