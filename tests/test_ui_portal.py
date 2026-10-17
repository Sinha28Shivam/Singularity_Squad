"""Streamlit portal UI tests (ui/app.py) against an in-memory fake backend (requests is mocked)."""
from __future__ import annotations

import json
import os
import pathlib
import re
from datetime import date
from unittest.mock import patch

import pytest

pytest.importorskip("streamlit.testing.v1")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = str(ROOT / "ui" / "app.py")
SHIM = str(ROOT / "ui" / "streamlit_app.py")
HEALTH = {"status": "ok", "llm_enabled": False, "model": "gpt-4o-mini",
          "database": "connected", "persistence": "required"}
INVOICES = [{"invoice_id": "INV-5001"}, {"invoice_id": "INV-5002"}, {"invoice_id": "INV-4987"}]
NOW = "2026-10-17T04:00:00+00:00"


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("No JSON")
        return self._body


_WF_CACHE: dict = {}


def workflow_result(sample: str, ticket_id: str, invoice_id=None) -> dict:
    key = (sample, invoice_id)
    if key not in _WF_CACHE:
        os.environ["WORKPLACEOPS_OFFLINE"] = "1"
        from app.orchestrator import run_workflow
        samples = json.loads((ROOT / "data" / "sample_requests.json").read_text(encoding="utf-8"))
        _WF_CACHE[key] = run_workflow(samples[sample], date(2026, 10, 17), invoice_id,
                                      request_id="REQ-X").model_dump(mode="json")
    wr = json.loads(json.dumps(_WF_CACHE[key]))
    wr["ticket_id"] = ticket_id
    wr["persisted"] = True
    return wr


def _path(url: str) -> str:
    p = url.split("://", 1)[-1]
    return p[p.find("/"):] if "/" in p else "/"


class FakeBackend:
    """Implements the portal HTTP contract in memory and records every call."""

    def __init__(self):
        self.users = {
            "alice": {"username": "alice", "name": "Alice A", "email": "alice@example.com", "role": "user",
                      "password": "alice-pass-1"},
            "admin": {"username": "admin", "name": "Ops Admin", "email": "admin@example.com", "role": "admin",
                      "password": "admin-pass-123"},
        }
        self.tokens: dict[str, str] = {}
        self.tickets: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.run_sample = "happy_path"
        self.expire_all = False
        self._n = 0

    # ---- helpers
    def login_token(self, username: str) -> dict:
        tok = f"tok-{username}-{len(self.tokens)}"
        self.tokens[tok] = username
        u = self.users[username]
        return {"token": tok, "expires_at": "2026-10-17T12:00:00+00:00",
                "user": {k: u[k] for k in ("username", "name", "email", "role")}}

    def _user(self, headers):
        auth = (headers or {}).get("Authorization", "")
        if self.expire_all or not auth.startswith("Bearer "):
            return None
        name = self.tokens.get(auth[7:])
        return self.users.get(name) if name else None

    def add_ticket(self, owner="alice", status="submitted", sub=None, result=None, run_count=0, **extra) -> str:
        self._n += 1
        tid = f"REQ-20261017-{self._n:08X}"
        u = self.users[owner]
        self.tickets[tid] = {
            "ticket_id": tid, "status": status, "owner_username": owner, "owner_name": u["name"],
            "owner_email": u["email"],
            "submission": sub or {"title": "Quarterly team lunch", "event_category": "team_lunch", "service": "lunch",
                                  "event_date": "2026-10-23", "attendees": 100, "budget_inr": 100000.0,
                                  "location": "Bengaluru office", "approved_vendor_only": True, "notes": None},
            "raw_text": "Organize lunch for 100 employees on 2026-10-23.",
            "workflow_result": result, "notes": [], "decision": None, "run_count": run_count,
            "last_run_by": "admin" if run_count else None, "error": None,
            "created_at": NOW, "updated_at": NOW, **extra,
        }
        return tid

    def list_item(self, t, admin):
        sub = t.get("submission") or {}
        item = {"ticket_id": t["ticket_id"], "title": sub.get("title"), "event_category": sub.get("event_category"),
                "event_date": sub.get("event_date"), "status": t["status"],
                "final_status": (t.get("workflow_result") or {}).get("final_status"),
                "run_count": t["run_count"], "created_at": t["created_at"], "updated_at": t["updated_at"]}
        if admin:
            item.update(owner_username=t["owner_username"], owner_name=t["owner_name"])
        return item

    def user_view(self, t):
        wr = t.get("workflow_result") or {}
        po = (wr.get("procurement") or {}).get("po")
        outcomes = {"submitted": "Waiting for an operations admin to review your request.",
                    "needs_info": "We need more information before we can continue.",
                    "reviewed": "Your request has been reviewed and awaits a decision.",
                    "approved": "Approved — the draft PO was approved. No purchase has been made.",
                    "rejected": "Your request was rejected."}
        return {"ticket_id": t["ticket_id"], "status": t["status"], "submission": t["submission"],
                "outcome": outcomes.get(t["status"], t["status"]),
                "clarification_questions": wr.get("clarification_questions") or [],
                "po_summary": ({"po_id": po["po_id"], "vendor_name": po["vendor_name"], "total": po["total"],
                                "currency": "INR", "status": "draft", "approval_status": None} if po else None),
                "notes": t["notes"], "decision": t["decision"], "created_at": t["created_at"],
                "updated_at": t["updated_at"]}

    def admin_view(self, t):
        return {k: t.get(k) for k in ("ticket_id", "status", "owner_username", "owner_name", "owner_email",
                                      "submission", "raw_text", "workflow_result", "notes", "decision",
                                      "run_count", "last_run_by", "error", "created_at", "updated_at")}

    # ---- transport
    def get(self, url, params=None, headers=None, timeout=None, **kw):
        path = _path(url)
        self.calls.append(("GET", path, params, headers))
        if path == "/api/health":
            return FakeResponse(200, HEALTH)
        if path == "/api/invoices":
            return FakeResponse(200, INVOICES)
        return self._dispatch("GET", path, params or {}, None, headers)

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        path = _path(url)
        self.calls.append(("POST", path, json, headers))
        return self._dispatch("POST", path, {}, json, headers)

    def put(self, url, json=None, headers=None, timeout=None, **kw):
        path = _path(url)
        self.calls.append(("PUT", path, json, headers))
        return self._dispatch("PUT", path, {}, json, headers)

    def _dispatch(self, method, path, params, body, headers):
        if path == "/api/auth/login" and method == "POST":
            u = self.users.get((body or {}).get("username", ""))
            if not u or u["password"] != body.get("password"):
                return FakeResponse(401, {"detail": "Invalid username or password."})
            return FakeResponse(200, self.login_token(u["username"]))
        if path == "/api/auth/signup" and method == "POST":
            if body["username"] in self.users or any(u["email"] == body["email"] for u in self.users.values()):
                return FakeResponse(409, {"detail": "Username or email already registered."})
            self.users[body["username"]] = dict(body, role="user")
            return FakeResponse(201, self.login_token(body["username"]))

        user = self._user(headers)
        if user is None:
            return FakeResponse(401, {"detail": "Not authenticated."})
        if path == "/api/auth/logout":
            return FakeResponse(204, None)

        m = re.match(r"^/api/my/tickets(?:/([^/]+))?(/notes)?$", path)
        if m:
            tid, notes = m.group(1), m.group(2)
            if tid is None:
                if method == "POST":
                    new_id = self.add_ticket(owner=user["username"], sub=body)
                    return FakeResponse(201, self.user_view(self.tickets[new_id]))
                own = [self.list_item(t, False) for t in self.tickets.values()
                       if t["owner_username"] == user["username"]]
                return FakeResponse(200, {"tickets": own[::-1]})
            t = self.tickets.get(tid)
            if not t or t["owner_username"] != user["username"]:
                return FakeResponse(404, {"detail": f"Ticket {tid} not found."})
            if notes:
                t["notes"].append({"author": user["username"], "role": user["role"], "text": body["text"],
                                   "created_at": NOW})
            elif method == "PUT":
                if t["status"] not in ("submitted", "needs_info"):
                    return FakeResponse(409, {"detail": "Ticket can no longer be edited."})
                t["submission"] = body
                t["status"] = "submitted"
            return FakeResponse(200, self.user_view(t))

        m = re.match(r"^/api/admin/tickets(?:/([^/]+))?(/run|/decision|/notes|/audit)?$", path)
        if m:
            if user["role"] != "admin":
                return FakeResponse(403, {"detail": "Admin access required."})
            tid, action = m.group(1), m.group(2)
            if tid is None:
                counts: dict = {}
                for t in self.tickets.values():
                    counts[t["status"]] = counts.get(t["status"], 0) + 1
                rows = [self.list_item(t, True) for t in self.tickets.values()
                        if not params.get("status") or t["status"] == params["status"]]
                return FakeResponse(200, {"tickets": rows, "counts": counts})
            t = self.tickets.get(tid)
            if not t:
                return FakeResponse(404, {"detail": f"Ticket {tid} not found."})
            if action == "/run":
                if t["status"] in ("approved", "rejected", "processing"):
                    return FakeResponse(409, {"detail": f"Ticket is {t['status']}; cannot run."})
                wr = workflow_result(self.run_sample, tid, body.get("invoice_id"))
                t.update(workflow_result=wr, run_count=t["run_count"] + 1, last_run_by=user["username"],
                         status="needs_info" if wr["final_status"] == "needs_clarification" else "reviewed")
            elif action == "/decision":
                if t["status"] != "reviewed":
                    return FakeResponse(409, {"detail": "Ticket is not awaiting a decision."})
                t["decision"] = {"decision": body["decision"], "by": user["username"], "at": NOW,
                                 "note": body.get("note")}
                t["status"] = "approved" if body["decision"] == "approve" else "rejected"
            elif action == "/notes":
                t["notes"].append({"author": user["username"], "role": "admin", "text": body["text"],
                                   "created_at": NOW})
            elif action == "/audit":
                events = [dict(a, sequence=a.get("step")) for a in (t.get("workflow_result") or {}).get(
                    "audit_trail") or []]
                return FakeResponse(200, {"ticket_id": tid, "events": events})
            return FakeResponse(200, self.admin_view(t))

        if path == "/api/reconcile":
            return FakeResponse(200, {"invoice_id": body.get("invoice_id"), "status": "match",
                                      "expected_total": 1, "invoiced_total": 1, "variance": 0, "checks": []})
        return FakeResponse(404, {"detail": "Not found"})

    def find(self, method, path):
        return [c for c in self.calls if c[0] == method and c[1] == path]


@pytest.fixture(autouse=True)
def _clear_caches():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.fixture
def backend():
    be = FakeBackend()
    with patch("requests.get", side_effect=be.get), patch("requests.post", side_effect=be.post), \
            patch("requests.put", side_effect=be.put):
        yield be


def new_app(path: str = APP) -> AppTest:
    at = AppTest.from_file(path, default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    return at


def logged_in(backend: FakeBackend, username: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["auth"] = backend.login_token(username)
    at.run()
    assert not at.exception, at.exception
    return at


def texts(at: AppTest) -> str:
    out = []
    for coll in (at.title, at.header, at.subheader, at.error, at.warning, at.success, at.info,
                 at.markdown, at.caption):
        out.extend(str(e.value) for e in coll)
    return "\n".join(out)


def button(at: AppTest, key: str):
    return at.button(key=key)


def click(at: AppTest, key: str) -> AppTest:
    at.button(key=key).click()
    at.run()
    assert not at.exception, at.exception
    return at


def submit_form(at: AppTest, form_key: str) -> AppTest:
    btn = next(b for b in at.get("form_submit_button") if b.form_id == form_key)
    btn.click()
    at.run()
    assert not at.exception, at.exception
    return at


# --------------------------------------------------------------------------- auth
def test_logged_out_shows_login_only(backend):
    at = new_app()
    assert [t.label for t in at.tabs][:2] == ["Log in", "Create account"]
    assert "My requests" not in [t.label for t in at.tabs]
    assert "Admin console" not in texts(at)


def test_shim_still_runs_portal(backend):
    at = new_app(SHIM)
    assert [t.label for t in at.tabs][:2] == ["Log in", "Create account"]


def test_login_invalid_shows_backend_detail(backend):
    at = new_app()
    at.text_input(key="login_username").input("alice")
    at.text_input(key="login_password").input("wrong-password")
    submit_form(at, "login_form")
    assert any("Invalid username or password." in str(e.value) for e in at.error)
    assert "auth" not in at.session_state


def test_login_user_shows_user_portal(backend):
    at = new_app()
    at.text_input(key="login_username").input("alice")
    at.text_input(key="login_password").input("alice-pass-1")
    submit_form(at, "login_form")
    assert at.session_state["auth"]["user"]["role"] == "user"
    assert at.session_state["auth"]["token"].startswith("tok-alice")
    body = texts(at)
    assert "📋 My requests" in body and "Admin console" not in body
    assert "Signed in as **Alice A** (user)" in "\n".join(str(m.value) for m in at.sidebar.markdown)


def test_login_admin_shows_admin_console_only(backend):
    at = new_app()
    at.text_input(key="login_username").input("admin")
    at.text_input(key="login_password").input("admin-pass-123")
    submit_form(at, "login_form")
    body = texts(at)
    assert "🛠️ Admin console" in body
    assert "📋 My requests" not in body


def test_signup_client_validation_and_success(backend):
    at = new_app()
    at.text_input(key="signup_username").input("b!")
    at.text_input(key="signup_name").input("Bob")
    at.text_input(key="signup_email").input("not-an-email")
    at.text_input(key="signup_password").input("short")
    at.text_input(key="signup_confirm").input("other")
    submit_form(at, "signup_form")
    errs = " ".join(str(e.value) for e in at.error)
    assert "Username" in errs and "email" in errs and "8 characters" in errs and "do not match" in errs
    assert not backend.find("POST", "/api/auth/signup")

    at.text_input(key="signup_username").input("bob.b")
    at.text_input(key="signup_email").input("bob@example.com")
    at.text_input(key="signup_password").input("bob-pass-12")
    at.text_input(key="signup_confirm").input("bob-pass-12")
    submit_form(at, "signup_form")
    sent = backend.find("POST", "/api/auth/signup")[-1][2]
    assert sent == {"username": "bob.b", "name": "Bob", "email": "bob@example.com", "password": "bob-pass-12"}
    assert at.session_state["auth"]["user"]["username"] == "bob.b"
    assert "📋 My requests" in texts(at)


def test_signup_duplicate_shows_409_detail(backend):
    at = new_app()
    at.text_input(key="signup_username").input("alice")
    at.text_input(key="signup_name").input("Alice")
    at.text_input(key="signup_email").input("alice@example.com")
    at.text_input(key="signup_password").input("whatever-123")
    at.text_input(key="signup_confirm").input("whatever-123")
    submit_form(at, "signup_form")
    assert any("Username or email already registered." in str(e.value) for e in at.error)
    assert "auth" not in at.session_state


def test_logout_clears_session(backend):
    at = logged_in(backend, "alice")
    click(at, "logout_btn")
    assert backend.find("POST", "/api/auth/logout")
    assert "auth" not in at.session_state
    assert [t.label for t in at.tabs][:2] == ["Log in", "Create account"]


def test_401_clears_auth_and_shows_expired(backend):
    at = logged_in(backend, "alice")
    backend.expire_all = True
    at.run()
    assert not at.exception
    assert "auth" not in at.session_state
    assert any("Session expired — please log in again." in str(i.value) for i in at.info)
    assert [t.label for t in at.tabs][:2] == ["Log in", "Create account"]


def test_requests_carry_bearer_token(backend):
    logged_in(backend, "alice")
    calls = backend.find("GET", "/api/my/tickets")
    assert calls and calls[-1][3]["Authorization"].startswith("Bearer tok-alice")


# --------------------------------------------------------------------------- user portal
def test_user_submits_ticket_and_sees_it(backend):
    at = logged_in(backend, "alice")
    at.text_input(key="new_ticket_form_title").input("Diwali team lunch")
    at.selectbox(key="new_ticket_form_category").select("celebration")
    at.number_input(key="new_ticket_form_attendees").set_value(80)
    at.number_input(key="new_ticket_form_budget").set_value(75000.0)
    at.text_input(key="new_ticket_form_location").input("Bengaluru office")
    submit_form(at, "new_ticket_form")
    payload = backend.find("POST", "/api/my/tickets")[-1][2]
    assert payload["event_date"] == "2026-10-23"
    assert isinstance(payload["attendees"], int) and payload["attendees"] == 80
    assert isinstance(payload["budget_inr"], float) and payload["budget_inr"] == 75000.0
    assert payload["event_category"] == "celebration" and payload["service"] == "lunch"
    assert payload["approved_vendor_only"] is True and payload["location"] == "Bengaluru office"
    assert payload["notes"] is None
    tid = next(iter(backend.tickets))
    assert any(tid in str(s.value) and "submitted" in str(s.value) for s in at.success)
    at.run()
    assert not at.exception
    df = at.dataframe[0].value
    assert tid in list(df["Ticket"]) and "Diwali team lunch" in list(df["Title"])


def test_user_detail_shows_outcome_without_internals(backend):
    tid = backend.add_ticket(status="reviewed", result=workflow_result("happy_path", "X"), run_count=1)
    at = logged_in(backend, "alice")
    assert at.selectbox(key="my_selected_ticket").value == tid
    body = texts(at)
    assert "awaits a decision" in body
    assert "Draft — no purchase made" in body
    assert "₹" in " ".join(str(m.value) for m in at.metric)
    for internal in ("Policy checks", "Critic findings", "Audit trail", "Vendor Matcher", "Billing Guardian"):
        assert internal not in body
    assert not [t for t in at.tabs if t.label.startswith("2. Policy")]
    assert not any(c[1].startswith("/api/admin") for c in backend.calls)
    # Approved / reviewed tickets cannot be edited.
    assert not [f for f in at.get("form_submit_button") if f.label == "Resubmit request"]


def test_user_add_note(backend):
    tid = backend.add_ticket()
    at = logged_in(backend, "alice")
    at.text_area(key=f"user_note_{tid}").input("Vegetarian options please")
    submit_form(at, f"user_note_form_{tid}")
    assert backend.find("POST", f"/api/my/tickets/{tid}/notes")[-1][2] == {"text": "Vegetarian options please"}
    assert "Vegetarian options please" in texts(at)


def test_needs_info_shows_edit_form_and_puts(backend):
    tid = backend.add_ticket(status="needs_info", result=workflow_result("missing_info", "X"), run_count=1)
    at = logged_in(backend, "alice")
    body = texts(at)
    assert "We need a bit more information" in body
    form = f"edit_ticket_form_{tid}"
    assert at.text_input(key=f"{form}_title").value == "Quarterly team lunch"
    at.number_input(key=f"{form}_attendees").set_value(120)
    submit_form(at, form)
    put = backend.find("PUT", f"/api/my/tickets/{tid}")
    assert put, "PUT not sent"
    sent = put[-1][2]
    assert sent["attendees"] == 120 and sent["event_date"] == "2026-10-23" and sent["budget_inr"] == 100000.0
    assert backend.tickets[tid]["status"] == "submitted"
    assert any("resubmitted" in str(s.value) for s in at.success)


# --------------------------------------------------------------------------- admin console
def test_admin_queue_counts_and_filter(backend):
    backend.add_ticket()
    backend.add_ticket()
    backend.add_ticket(status="approved")
    at = logged_in(backend, "admin")
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["⏳ submitted"] == "2" and metrics["✅ approved"] == "1"
    assert len(at.dataframe[0].value) == 3
    assert list(at.dataframe[0].value["Requester"])[0] == "Alice A"
    at.selectbox(key="admin_status_filter").select("approved")
    at.run()
    assert not at.exception
    assert backend.find("GET", "/api/admin/tickets")[-1][2] == {"limit": 100, "status": "approved"}
    assert len(at.dataframe[0].value) == 1


def test_admin_run_and_approve(backend):
    tid = backend.add_ticket()
    at = logged_in(backend, "admin")
    at.selectbox(key="admin_pick").select(tid)
    at.run()
    assert not at.exception
    assert button(at, "admin_run_btn").label == "▶️ Run workflow"
    assert button(at, "admin_approve_btn").disabled and button(at, "admin_reject_btn").disabled
    at.selectbox(key="admin_invoice").select("INV-5001")
    click(at, "admin_run_btn")
    sent = backend.find("POST", f"/api/admin/tickets/{tid}/run")[-1][2]
    assert sent == {"invoice_id": "INV-5001", "today": "2026-10-17"}
    labels = [t.label for t in at.tabs]
    for lbl in ("1. Request", "2. Policy", "3. Vendors", "4. Draft PO", "5. Invoice check", "6. Critic & Audit"):
        assert lbl in labels
    assert button(at, "admin_run_btn").label == "🔁 Re-run workflow"
    assert not button(at, "admin_approve_btn").disabled
    assert "Stored result is persisted" in texts(at)

    click(at, "admin_audit_btn")
    assert backend.find("GET", f"/api/admin/tickets/{tid}/audit")
    assert "Saved audit history" in texts(at)

    at.text_area(key="admin_decision_note").input("Looks good")
    click(at, "admin_approve_btn")
    dec = backend.find("POST", f"/api/admin/tickets/{tid}/decision")[-1][2]
    assert dec == {"decision": "approve", "note": "Looks good"}
    assert backend.tickets[tid]["status"] == "approved"
    assert button(at, "admin_run_btn").disabled
    assert button(at, "admin_approve_btn").disabled


def test_admin_approve_disabled_for_blocked(backend):
    backend.run_sample = "over_budget"
    tid = backend.add_ticket()
    at = logged_in(backend, "admin")
    at.text_input(key="admin_ticket_id_input").input(tid)
    at.run()
    click(at, "admin_load_btn")
    click(at, "admin_run_btn")
    wr = backend.tickets[tid]["workflow_result"]
    assert wr["final_status"] == "blocked"
    assert backend.tickets[tid]["status"] == "reviewed"
    assert button(at, "admin_approve_btn").disabled
    assert not button(at, "admin_reject_btn").disabled
    click(at, "admin_reject_btn")
    assert backend.find("POST", f"/api/admin/tickets/{tid}/decision")[-1][2] == {"decision": "reject", "note": None}
    assert backend.tickets[tid]["status"] == "rejected"


def test_admin_409_is_shown_nicely(backend):
    tid = backend.add_ticket()
    at = logged_in(backend, "admin")
    at.selectbox(key="admin_pick").select(tid)
    at.run()
    backend.tickets[tid]["status"] = "approved"  # changed concurrently by another admin
    click(at, "admin_run_btn")
    assert any("Not allowed right now" in str(e.value) and "cannot run" in str(e.value) for e in at.error)


def test_admin_note(backend):
    tid = backend.add_ticket()
    at = logged_in(backend, "admin")
    at.selectbox(key="admin_pick").select(tid)
    at.run()
    at.text_area(key=f"admin_note_{tid}").input("Please confirm the venue")
    submit_form(at, f"admin_note_form_{tid}")
    assert backend.find("POST", f"/api/admin/tickets/{tid}/notes")[-1][2] == {"text": "Please confirm the venue"}
    assert "Please confirm the venue" in texts(at)


def test_user_cannot_see_admin_console(backend):
    at = logged_in(backend, "alice")
    assert "Admin console" not in texts(at)
    assert not any(c[1].startswith("/api/admin") for c in backend.calls)
