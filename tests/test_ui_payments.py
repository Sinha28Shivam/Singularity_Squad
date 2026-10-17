"""Payments (Simulation) UI tests (AppTest) against an in-memory fake backend. SIMULATION ONLY."""
from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest

pytest.importorskip("streamlit.testing.v1")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from test_ui_portal import APP, NOW, FakeBackend, FakeResponse, texts  # noqa: E402

BANNER = "SIMULATED — NO MONEY TRANSFERRED"
CONFIRM = ("You are about to create a **simulated payment instruction** for ₹76,125.00. This is a demo only. "
           "No bank will be contacted and no money will be transferred.")
REPLAY = "Existing simulated payment returned (idempotent replay)"
BASE = "/api/payments/simulation"


class PaymentBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.payments: dict[str, dict] = {}
        self.events: dict[str, list] = {}
        self.by_key: dict[str, str] = {}
        self.not_eligible: list = []           # reasons -> eligibility False
        self.create_error = None                # (status, body) for POST ""
        self.profile = "normal"

    def approved_ticket(self, invoice_id="INV-5001", total=76125.0) -> str:
        wr = {"final_status": "pass", "reconciliation": {"invoice_id": invoice_id, "status": "match",
                                                         "invoiced_total": total},
              "procurement": {"po": {"po_id": "DRAFT-PO-REQ-1", "total": total, "currency": "INR"}}}
        return self.add_ticket(status="approved", run_count=1, result=wr)

    def eligibility(self, tid, inv):
        t = self.tickets[tid]
        po = t["workflow_result"]["procurement"]["po"]
        existing = next((p["payment_id"] for p in self.payments.values()
                         if p["ticket_id"] == tid and p["invoice_id"] == inv), None)
        return {"mode": "SIMULATION", "money_transferred": False, "disclaimer": BANNER,
                "eligible": not self.not_eligible, "ticket_id": tid, "invoice_id": inv,
                "purchase_order_id": po["po_id"], "amount": f"{po['total']:.2f}", "currency": "INR",
                "invoice_match_status": "match", "approval_id": "APR-20261017-0000abcd",
                "approval_status": "approved", "approved_by": "carol", "approval_request_version": 2,
                "policy_set_version": 7, "reasons": list(self.not_eligible), "existing_payment_id": existing,
                "checked_at": NOW, "eligibility_fingerprint": "sha256:" + "a" * 64}

    def _event(self, pid, etype, status=None, codes=()):
        evs = self.events.setdefault(pid, [])
        evs.append({"event_id": f"{pid}-{len(evs)}", "payment_id": pid, "event_type": etype, "status": status,
                    "actor": "admin", "at": f"2026-10-17T04:00:{len(evs):02d}+00:00",
                    "reason_codes": list(codes), "detail": None, "mode": "SIMULATION",
                    "money_transferred": False})

    def _payments_api(self, method, path, params, body, user):
        if not path.startswith(BASE):
            return None
        if user["role"] not in ("admin", "approver"):
            return FakeResponse(403, {"error": "CALLER_NOT_AUTHORIZED", "message": "Not allowed.", "reasons": [],
                                      "mode": "SIMULATION", "money_transferred": False})
        rest = path[len(BASE):]
        m = re.match(r"^/eligibility/([^/]+)$", rest)
        if m and method == "GET":
            return FakeResponse(200, self.eligibility(params["ticket_id"], m.group(1)))
        if rest == "" and method == "GET":
            rows = [p for p in self.payments.values()
                    if not params.get("ticket_id") or p["ticket_id"] == params["ticket_id"]]
            return FakeResponse(200, rows)
        if rest == "" and method == "POST":
            if user["role"] != "admin":
                return FakeResponse(403, {"error": "CALLER_NOT_AUTHORIZED", "message": "Admins only.",
                                          "reasons": [], "mode": "SIMULATION", "money_transferred": False})
            if self.create_error:
                return FakeResponse(*self.create_error)
            key = body["idempotency_key"]
            if key in self.by_key:
                return FakeResponse(200, {"payment": self.payments[self.by_key[key]], "replayed": True,
                                          "mode": "SIMULATION", "money_transferred": False,
                                          "message": "Replayed."})
            pid = f"psim_{len(self.payments) + 1:012x}"
            failed = self.profile == "transient_failure"
            p = {"mode": "SIMULATION", "money_transferred": False, "disclaimer": BANNER, "payment_id": pid,
                 "provider_reference": None if failed else "PAY-SIM-ABC123", "ticket_id": body["ticket_id"],
                 "invoice_id": body["invoice_id"], "purchase_order_id": body["purchase_order_id"],
                 "amount": body["amount"], "currency": body["currency"], "method": "SIMULATED_NEFT",
                 "status": "failed_simulated" if failed else "accepted_by_simulator",
                 "failure_code": "SIMULATED_PROVIDER_UNAVAILABLE" if failed else None, "attempts": 1,
                 "approval_id": "APR-20261017-0000abcd", "approval_request_version": 2, "policy_set_version": 7,
                 "eligibility_fingerprint": "x", "request_fingerprint": "y", "idempotency_key_hash": "z",
                 "simulator_profile": self.profile, "correlation_id": "c", "created_by": "admin",
                 "created_at": NOW, "updated_at": NOW, "audit_complete": True,
                 "message": "Simulation accepted. No money was transferred."}
            self.payments[pid] = p
            self.by_key[key] = pid
            self._event(pid, "payment_simulation_requested")
            self._event(pid, "payment_simulation_created", "submitted_to_simulator")
            self._event(pid, "payment_simulation_status_changed", p["status"],
                        ["SIMULATED_PROVIDER_UNAVAILABLE"] if failed else ())
            return FakeResponse(201, {"payment": p, "replayed": False, "mode": "SIMULATION",
                                      "money_transferred": False,
                                      "message": "Simulation accepted. No money was transferred."})
        m = re.match(r"^/([^/]+)(/events|/retry)?$", rest)
        if m:
            p = self.payments.get(m.group(1))
            if not p:
                return FakeResponse(404, {"error": "NOT_FOUND", "message": "Unknown payment.", "reasons": []})
            if m.group(2) == "/events":
                return FakeResponse(200, self.events.get(p["payment_id"], []))
            if m.group(2) == "/retry" and method == "POST":
                assert body == {"confirmed_by_user": True}
                p.update(status="accepted_by_simulator", attempts=p["attempts"] + 1,
                         provider_reference="PAY-SIM-RETRY1", failure_code=None)
                self._event(p["payment_id"], "payment_simulation_retry_requested", "submitted_to_simulator")
                self._event(p["payment_id"], "payment_simulation_status_changed", "accepted_by_simulator")
                return FakeResponse(200, {"payment": p, "replayed": False, "mode": "SIMULATION",
                                          "money_transferred": False, "message": "Retried."})
            return FakeResponse(200, {"payment": p})
        return FakeResponse(404, {"detail": "Not found"})

    def _dispatch(self, method, path, params, body, headers):
        m = re.match(r"^/api/admin/tickets/([^/]+)/invoice-match$", path)
        if m and method == "POST":
            t = self.tickets[m.group(1)]
            t["invoice_match"] = {"invoice_id": body["invoice_id"], "status": "match", "invoiced_total": 76125.0,
                                  "post_approval": True}
            t["workflow_result"].setdefault("procurement", {"po": {"po_id": "DRAFT-PO-REQ-1", "total": 76125.0,
                                                                   "currency": "INR"}})
            return FakeResponse(200, t)
        if path.startswith(BASE):
            user = self._user(headers)
            if user is None:
                return FakeResponse(401, {"detail": "Not authenticated."})
            return self._payments_api(method, path, params, body, user)
        return super()._dispatch(method, path, params, body, headers)


@pytest.fixture(autouse=True)
def _clear_caches():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.fixture
def backend():
    be = PaymentBackend()
    with patch("requests.get", side_effect=be.get), patch("requests.post", side_effect=be.post), \
            patch("requests.put", side_effect=be.put):
        yield be


def run(at: AppTest) -> AppTest:
    at.run()
    assert not at.exception, at.exception
    return at


def _render_payments():
    from ui.pages_portal import payments
    payments.render()


def page(backend: PaymentBackend, user: str) -> AppTest:
    at = AppTest.from_function(_render_payments, default_timeout=60)
    at.session_state["auth"] = backend.login_token(user)
    return run(at)


def page_titles(at: AppTest) -> list:
    return [str(info.get("page_name")) for info in at._registered_pages.values()]


def all_text(at: AppTest) -> str:
    body = texts(at)
    for df in at.dataframe:
        body += "\n" + df.value.to_string()
    return body


def assert_safe(at: AppTest) -> None:
    body = all_text(at)
    assert BANNER in body
    assert not re.search(r"\bpaid\b", body, re.IGNORECASE), "the word 'Paid' must never be rendered"
    assert not at.success, "no success badge for simulated payments"


def select_ticket(at: AppTest, tid: str) -> AppTest:
    at.selectbox(key="pay_ticket_pick").select(tid)
    return run(at)


def simulate(at: AppTest) -> AppTest:
    at.checkbox(key="pay_confirm_check").check()
    run(at)
    at.button(key="pay_simulate_btn").click()
    return run(at)


def creates(backend):
    return backend.find("POST", BASE)


# --------------------------------------------------------------------------- navigation
def test_navigation_per_role(backend):
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["auth"] = backend.login_token("admin")
    run(at)
    assert "Payments (Simulation)" in page_titles(at)
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["auth"] = backend.login_token("carol")
    run(at)
    assert "Payments (Simulation, read-only)" in page_titles(at)
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["auth"] = backend.login_token("alice")
    run(at)
    assert not any("Payments" in t for t in page_titles(at))
    assert not any(c[1].startswith(BASE) for c in backend.calls)


def test_user_role_rendering_page_directly_is_refused(backend):
    at = page(backend, "alice")
    assert BANNER in texts(at)
    assert any("admins and approvers only" in str(e.value) for e in at.error)
    assert not any(c[1].startswith(BASE) for c in backend.calls)


# --------------------------------------------------------------------------- admin flow
def test_banner_and_empty_state(backend):
    at = page(backend, "admin")
    assert_safe(at)
    assert backend.find("GET", "/api/admin/tickets")[-1][2] == {"limit": 100, "status": "approved"}
    assert "No approved tickets yet" in texts(at)


def test_eligibility_panel_and_confirmation(backend):
    tid = backend.approved_ticket()
    backend.add_ticket(status="pending_approval")
    at = page(backend, "admin")
    assert at.selectbox(key="pay_ticket_pick").options == ["(select an approved ticket)",
                                                           f"{tid} · Quarterly team lunch"]
    select_ticket(at, tid)
    elig = backend.find("GET", f"{BASE}/eligibility/INV-5001")
    assert elig and elig[-1][2] == {"ticket_id": tid}
    body = all_text(at)
    for s in ("INV-5001", "DRAFT-PO-REQ-1", "₹76,125.00", "match", "APR-20261017-0000abcd", "carol",
              "Eligible", CONFIRM):
        assert s in body, s
    assert "Policy set version" in body and "7" in body
    assert at.button(key="pay_simulate_btn").disabled
    at.checkbox(key="pay_confirm_check").check()
    run(at)
    assert not at.button(key="pay_simulate_btn").disabled
    assert not creates(backend)
    assert_safe(at)


def test_simulate_creates_receipt_and_replays(backend):
    tid = backend.approved_ticket()
    at = select_ticket(page(backend, "admin"), tid)
    simulate(at)
    posts = creates(backend)
    assert len(posts) == 1
    sent = posts[0][2]
    assert sent["simulation_only"] is True and sent["confirmed_by_user"] is True
    assert sent["amount"] == "76125.00" and sent["currency"] == "INR"
    assert sent["purchase_order_id"] == "DRAFT-PO-REQ-1" and sent["invoice_id"] == "INV-5001"
    body = all_text(at)
    for s in ("psim_000000000001", "PAY-SIM-ABC123", "₹76,125.00 INR", "INV-5001", "DRAFT-PO-REQ-1",
              "Accepted by simulator", "2026-10-17 04:00:00 UTC", "payment simulation created"):
        assert s in body, s
    assert "pay_retry_btn" not in {b.key for b in at.button}
    assert at.button(key="pay_simulate_btn").disabled  # checkbox reset after submit
    assert_safe(at)

    dl = at.get("download_button")
    assert dl
    simulate(at)
    posts = creates(backend)
    assert len(posts) == 2 and posts[1][2]["idempotency_key"] == sent["idempotency_key"]
    assert REPLAY in texts(at)
    assert len(backend.payments) == 1
    assert_safe(at)


def test_receipt_json_keeps_disclaimer():
    from ui.pages_portal.payments import receipt_json
    doc = json.loads(receipt_json({"payment_id": "psim_1", "status": "accepted_by_simulator"}, []))
    assert BANNER in doc["disclaimer"] and doc["money_transferred"] is False and doc["mode"] == "SIMULATION"


def test_not_eligible_shows_reasons_and_no_confirm(backend):
    tid = backend.approved_ticket()
    backend.not_eligible = [{"code": "APPROVAL_STALE", "message": "The approval no longer matches the PO."},
                            {"code": "POLICY_BLOCK", "message": "Hard policy block."}]
    at = select_ticket(page(backend, "admin"), tid)
    err = "\n".join(str(e.value) for e in at.error)
    assert "Not eligible" in err and "APPROVAL_STALE" in err and "POLICY_BLOCK" in err
    assert "Hard policy block." in err
    assert "pay_simulate_btn" not in {b.key for b in at.button}
    assert CONFIRM not in texts(at)
    assert_safe(at)


def test_422_from_create_renders_reason_codes(backend):
    tid = backend.approved_ticket()
    backend.create_error = (422, {"error": "PAYMENT_NOT_ELIGIBLE", "message": "Payment is not eligible.",
                                  "reasons": [{"code": "AMOUNT_MISMATCH", "message": "Amount differs from PO."}],
                                  "mode": "SIMULATION", "money_transferred": False})
    at = simulate(select_ticket(page(backend, "admin"), tid))
    err = "\n".join(str(e.value) for e in at.error)
    assert "PAYMENT_NOT_ELIGIBLE" in err and "Payment is not eligible." in err
    assert "AMOUNT_MISMATCH" in err and "Amount differs from PO." in err
    assert "Traceback" not in err and "{" not in err
    assert not backend.payments
    assert_safe(at)


@pytest.mark.parametrize("status,error", [(503, "SIMULATION_DISABLED"), (409, "IDEMPOTENCY_CONFLICT")])
def test_other_server_errors_rendered(backend, status, error):
    tid = backend.approved_ticket()
    backend.create_error = (status, {"error": error, "message": "Server says no.", "reasons": [],
                                     "mode": "SIMULATION", "money_transferred": False})
    at = simulate(select_ticket(page(backend, "admin"), tid))
    err = "\n".join(str(e.value) for e in at.error)
    assert error in err and "Server says no." in err
    assert_safe(at)


def test_failed_simulation_retry_flow(backend):
    backend.profile = "transient_failure"
    tid = backend.approved_ticket()
    at = simulate(select_ticket(page(backend, "admin"), tid))
    body = all_text(at)
    assert "Simulation failed (retryable)" in body and "SIMULATED_PROVIDER_UNAVAILABLE" in body
    at.button(key="pay_retry_btn").click()
    run(at)
    pid = next(iter(backend.payments))
    assert backend.find("POST", f"{BASE}/{pid}/retry")[-1][2] == {"confirmed_by_user": True}
    body = all_text(at)
    assert "Accepted by simulator" in body and "PAY-SIM-RETRY1" in body
    assert "pay_retry_btn" not in {b.key for b in at.button}
    assert len(backend.payments) == 1
    assert_safe(at)


def test_ticket_without_reconciliation(backend):
    tid = backend.add_ticket(status="approved", run_count=1, result={"final_status": "pass"})
    at = select_ticket(page(backend, "admin"), tid)
    assert "no reconciled invoice" in texts(at)
    assert not any("/eligibility/" in c[1] for c in backend.calls)
    assert_safe(at)


def test_match_invoice_after_approval_then_eligibility(backend):
    tid = backend.add_ticket(status="approved", run_count=1, result={"final_status": "pass"})
    at = select_ticket(page(backend, "admin"), tid)
    at.selectbox(key="pay_match_invoice").select("INV-5001")
    run(at)
    at.button(key="pay_match_btn").click()
    run(at)
    posts = backend.find("POST", f"/api/admin/tickets/{tid}/invoice-match")
    assert posts and posts[-1][2] == {"invoice_id": "INV-5001"}
    body = texts(at)
    assert "matched against the approved PO" in body
    assert any("/eligibility/INV-5001" in c[1] for c in backend.calls)
    assert_safe(at)


# --------------------------------------------------------------------------- approver
def test_approver_read_only_list_and_receipt(backend):
    tid = backend.approved_ticket()
    admin = simulate(select_ticket(page(backend, "admin"), tid))
    assert backend.payments
    backend.calls.clear()
    at = page(backend, "carol")
    body = all_text(at)
    assert "Read-only" in body and "psim_000000000001" in body and "Accepted by simulator" in body
    assert not backend.find("GET", "/api/admin/tickets")
    assert "pay_simulate_btn" not in {b.key for b in at.button}
    at.selectbox(key="pay_view_pick").select("psim_000000000001")
    run(at)
    assert "PAY-SIM-ABC123" in all_text(at)
    assert "pay_retry_btn" not in {b.key for b in at.button}
    assert not any(c[0] == "POST" for c in backend.calls)
    assert_safe(at)
    assert_safe(admin)


def test_approver_cannot_retry_failed(backend):
    backend.profile = "transient_failure"
    tid = backend.approved_ticket()
    simulate(select_ticket(page(backend, "admin"), tid))
    at = page(backend, "carol")
    at.selectbox(key="pay_view_pick").select("psim_000000000001")
    run(at)
    assert "Simulation failed (retryable)" in all_text(at)
    assert "pay_retry_btn" not in {b.key for b in at.button}
    assert_safe(at)
