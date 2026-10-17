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
SAFETY = "Approval records internal authorization only. No order or payment is executed."


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
            "carol": {"username": "carol", "name": "Carol Approver", "email": "carol@example.com",
                      "role": "approver", "password": "carol-pass-123"},
        }
        self.tokens: dict[str, str] = {}
        self.tickets: dict[str, dict] = {}
        self.approvals: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.run_sample = "happy_path"
        self.expire_all = False
        # Fault injection for POST /api/approvals/{id}/decision: each entry is popped per call.
        #   int status  -> respond with that error and do not commit
        #   "commit_503" -> commit the decision but answer 503 (lost response)
        self.decision_faults: list = []
        self.reconcile_on_decide = False
        self._n = 0
        self._a = 0

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

    # ---- approvals (mirrors app/approvals/schemas.py + the Phase 2 HTTP contract)
    def add_approval(self, tid: str, status: str = "pending", amount: float = 59000.0,
                     reason_codes=("PO_REQUIRES_HUMAN_APPROVAL", "AMOUNT_THRESHOLD"), risk_level="medium") -> str:
        t = self.tickets[tid]
        for a in self.approvals.values():
            if a["ticket_id"] == tid and a["status"] == "pending":
                a["status"] = "superseded"
        version = 1 + max([a["request_version"] for a in self.approvals.values() if a["ticket_id"] == tid],
                          default=0)
        self._a += 1
        aid = f"APR-20261017-{self._a:08x}"
        texts_ = {"PO_REQUIRES_HUMAN_APPROVAL": "Every system-generated draft PO requires human sign-off (PROC-4.1).",
                  "AMOUNT_THRESHOLD": "Draft PO total exceeds the approval threshold (PROC-4.1).",
                  "HIGH_VALUE": "Draft PO total exceeds the high-value threshold (PROC-4.1)."}
        sub = t.get("submission") or {}
        self.approvals[aid] = {
            "approval_id": aid, "ticket_id": tid, "request_version": version, "ticket_version": t["run_count"],
            "status": status, "requester_id": t["owner_username"], "required_role": "approver",
            "assigned_approver_id": None, "reason_codes": list(reason_codes),
            "reasons": [texts_.get(c, c) for c in reason_codes], "risk_level": risk_level,
            "amount": amount, "currency": "INR", "policy_version": "approval-policy-v1",
            "workflow_snapshot_hash": (f"{self._a:02x}" * 32)[:64],
            "review_snapshot": {"ticket_id": tid, "run_count": t["run_count"], "final_status": "pass",
                                "request": {"date": sub.get("event_date"), "attendees": sub.get("attendees"),
                                            "budget_limit": sub.get("budget_inr"), "service": sub.get("service"),
                                            "location": sub.get("location")},
                                "po": {"po_id": "PO-TEST-0001", "vendor_id": "V-001", "vendor_name": "Spice Route",
                                       "line_items": [{"description": "Lunch", "quantity": 100,
                                                       "unit_price": 500.0, "line_total": 50000.0}],
                                       "subtotal": 50000.0, "tax": 9000.0, "total": amount, "currency": "INR",
                                       "required_approver": "Finance"}},
            "created_at": NOW, "expires_at": "2026-10-20T04:00:00+00:00", "decided_at": None,
            "decisions": [], "reconciliation_required": False, "schema_version": 1,
        }
        if status == "pending":
            t["status"] = "pending_approval"
        return aid

    def approval_view(self, a: dict, viewer: dict) -> dict:
        reason = None
        if viewer["role"] != "approver":
            reason = "Only users with the approver role can decide approval requests."
        elif viewer["username"] == a["requester_id"]:
            reason = "You cannot decide your own request."
        elif a["status"] != "pending":
            reason = f"This request is already {a['status']}."
        return dict(json.loads(json.dumps(a)), can_decide=reason is None, cannot_decide_reason=reason,
                    safety_notice=SAFETY)

    def ticket_approval_view(self, tid: str, viewer: dict):
        hist = sorted([a for a in self.approvals.values() if a["ticket_id"] == tid],
                      key=lambda a: a["request_version"])
        if not hist:
            return None
        views = [self.approval_view(a, viewer) for a in hist]
        return {"ticket_id": tid, "requirement": None, "current": views[-1], "history": views,
                "safety_notice": SAFETY}

    def _visible(self, a: dict, user: dict) -> bool:
        return user["role"] in ("approver", "admin") or a["requester_id"] == user["username"]

    def _approvals_api(self, method, path, params, body, user):
        m = re.match(r"^/api/admin/approvals/([^/]+)/reconcile$", path)
        if m:
            if user["role"] != "admin":
                return FakeResponse(403, {"detail": "Admin access required."})
            a = self.approvals.get(m.group(1))
            if not a:
                return FakeResponse(404, {"detail": "Approval request not found."})
            a["reconciliation_required"] = False
            return FakeResponse(200, self.approval_view(a, user))
        m = re.match(r"^/api/tickets/([^/]+)/approval$", path)
        if m:
            t = self.tickets.get(m.group(1))
            if not t or (user["role"] == "user" and t["owner_username"] != user["username"]):
                return FakeResponse(404, {"detail": "Ticket not found."})
            tav = self.ticket_approval_view(t["ticket_id"], user)
            return FakeResponse(200, tav or {"ticket_id": t["ticket_id"], "current": None, "history": [],
                                             "safety_notice": SAFETY})
        if path == "/api/approvals" and method == "GET":
            if user["role"] not in ("approver", "admin"):
                return FakeResponse(403, {"detail": "Approver or admin access required."})
            counts: dict = {}
            for a in self.approvals.values():
                counts[a["status"]] = counts.get(a["status"], 0) + 1
            rows = [{k: a[k] for k in ("approval_id", "ticket_id", "request_version", "status", "requester_id",
                                       "amount", "currency", "risk_level", "reason_codes", "created_at",
                                       "expires_at")}
                    | {"summary": "Team lunch · 100 attendees · 2026-10-23",
                       "can_decide": self.approval_view(a, user)["can_decide"]}
                    for a in self.approvals.values() if not params.get("status") or a["status"] == params["status"]]
            return FakeResponse(200, {"approvals": rows, "counts": counts})
        m = re.match(r"^/api/approvals/([^/]+)(/decision)?$", path)
        if not m:
            return None
        a = self.approvals.get(m.group(1))
        if not a or not self._visible(a, user):
            return FakeResponse(404, {"detail": "Approval request not found."})
        if not m.group(2):
            return FakeResponse(200, self.approval_view(a, user))
        fault = self.decision_faults.pop(0) if self.decision_faults else None
        if isinstance(fault, int):
            return FakeResponse(fault, {"detail": "Database unavailable. Please try again later."
                                        if fault == 503 else "The request has changed; reload and review the "
                                                             "latest version."})
        allowed = {"action", "comment", "idempotency_key", "request_version", "workflow_snapshot_hash"}
        if set(body) - allowed or not body.get("idempotency_key"):
            return FakeResponse(422, {"detail": "Invalid decision body."})
        if user["role"] != "approver":
            return FakeResponse(403, {"detail": "Only an eligible approver can decide approval requests."})
        if user["username"] == a["requester_id"]:
            return FakeResponse(403, {"detail": "You cannot decide your own request."})
        comment = (body.get("comment") or "").strip() or None
        if body["action"] in ("reject", "request_changes") and not comment:
            return FakeResponse(422, {"detail": "A comment is required to reject or request changes."})
        prior = next((d for d in a["decisions"] if d["idempotency_key"] == body["idempotency_key"]), None)
        if prior is not None:
            same = (prior["action"] == body["action"] and prior["comment"] == comment
                    and prior["request_version"] == body["request_version"]
                    and prior["workflow_snapshot_hash"] == body["workflow_snapshot_hash"]
                    and prior["actor_id"] == user["username"])
            if not same:
                return FakeResponse(409, {"detail": "Idempotency key was already used with a different request."})
            return FakeResponse(200, {"approval": self.approval_view(a, user), "replayed": True,
                                      "reconciliation_required": a["reconciliation_required"],
                                      "message": "Decision already recorded."})
        if a["status"] != "pending":
            return FakeResponse(409, {"detail": "This approval has already been decided or the request has changed."})
        if (body["request_version"] != a["request_version"]
                or body["workflow_snapshot_hash"] != a["workflow_snapshot_hash"]):
            return FakeResponse(409, {"detail": "The request has changed; reload and review the latest version."})
        decided_at = "2026-10-17T05:30:00+00:00"
        a["decisions"].append({"decision_id": f"DEC-{len(a['decisions']) + 1:012x}", "action": body["action"],
                               "actor_id": user["username"], "actor_role": user["role"], "comment": comment,
                               "decided_at": decided_at, "idempotency_key": body["idempotency_key"],
                               "request_version": body["request_version"],
                               "workflow_snapshot_hash": body["workflow_snapshot_hash"]})
        a["status"] = {"approve": "approved", "reject": "rejected", "request_changes": "changes_requested"}[
            body["action"]]
        a["decided_at"] = decided_at
        a["reconciliation_required"] = self.reconcile_on_decide
        self.tickets[a["ticket_id"]]["status"] = a["status"]
        if fault == "commit_503":
            return FakeResponse(503, {"detail": "Database unavailable. Please try again later."})
        return FakeResponse(200, {"approval": self.approval_view(a, user), "replayed": False,
                                  "reconciliation_required": a["reconciliation_required"],
                                  "message": "Decision recorded."})

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
                    "pending_approval": "Waiting for approval by an approver.",
                    "changes_requested": "Changes requested — please update your request.",
                    "approval_expired": "Approval request expired — an admin will re-run it.",
                    "approved": "Approved — the draft PO was approved. No purchase has been made.",
                    "rejected": "Your request was rejected."}
        return {"ticket_id": t["ticket_id"], "status": t["status"], "submission": t["submission"],
                "outcome": outcomes.get(t["status"], t["status"]),
                "clarification_questions": wr.get("clarification_questions") or [],
                "po_summary": ({"po_id": po["po_id"], "vendor_name": po["vendor_name"], "total": po["total"],
                                "currency": "INR", "status": "draft", "approval_status": None} if po else None),
                "notes": t["notes"], "decision": t["decision"], "created_at": t["created_at"],
                "updated_at": t["updated_at"],
                "approval": self.ticket_approval_view(t["ticket_id"], self.users[t["owner_username"]])}

    def admin_view(self, t):
        view = {k: t.get(k) for k in ("ticket_id", "status", "owner_username", "owner_name", "owner_email",
                                      "submission", "raw_text", "workflow_result", "notes", "decision",
                                      "run_count", "last_run_by", "error", "created_at", "updated_at",
                                      "invoice_match")}
        view["approval"] = self.ticket_approval_view(t["ticket_id"], self.users["admin"])
        return view

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
                if t["status"] not in ("submitted", "needs_info", "changes_requested", "approval_expired"):
                    return FakeResponse(409, {"detail": "Ticket can no longer be edited."})
                t["submission"] = body
                t["status"] = "submitted"
                for a in self.approvals.values():
                    if a["ticket_id"] == tid and a["status"] == "pending":
                        a["status"] = "superseded"
            return FakeResponse(200, self.user_view(t))

        resp = self._approvals_api(method, path, params, body, user)
        if resp is not None:
            return resp

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
                if t["status"] in ("approved", "rejected", "processing", "changes_requested"):
                    return FakeResponse(409, {"detail": f"Ticket is {t['status']}; cannot run."})
                wr = workflow_result(self.run_sample, tid, body.get("invoice_id"))
                t.update(workflow_result=wr, run_count=t["run_count"] + 1, last_run_by=user["username"],
                         status="needs_info" if wr["final_status"] == "needs_clarification" else "reviewed")
                po = (wr.get("procurement") or {}).get("po")
                if wr["final_status"] in ("pass", "warning") and po:
                    self.add_approval(tid, amount=float(po["total"]))
            elif action == "/decision":
                if body["decision"] == "approve":
                    return FakeResponse(403, {"detail": "Approvals must be decided by an eligible approver via "
                                                        "/api/approvals."})
                if t["status"] != "reviewed":
                    return FakeResponse(409, {"detail": "Ticket is not awaiting a decision."})
                t["decision"] = {"decision": body["decision"], "by": user["username"], "at": NOW,
                                 "note": body.get("note")}
                t["status"] = "rejected"
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
    assert "Draft — no order placed, nothing paid" in body
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


def _button_keys(at: AppTest) -> set:
    return {b.key for b in at.button}


def test_admin_run_creates_pending_approval_without_admin_approve(backend):
    tid = backend.add_ticket()
    at = logged_in(backend, "admin")
    at.selectbox(key="admin_pick").select(tid)
    at.run()
    assert not at.exception
    assert button(at, "admin_run_btn").label == "▶️ Run workflow"
    # Approval moved to approvers: no approve control at all; reject only for `reviewed` tickets.
    assert "admin_approve_btn" not in _button_keys(at) and "admin_reject_btn" not in _button_keys(at)
    at.selectbox(key="admin_invoice").select("INV-5001")
    click(at, "admin_run_btn")
    sent = backend.find("POST", f"/api/admin/tickets/{tid}/run")[-1][2]
    assert sent == {"invoice_id": "INV-5001", "today": "2026-10-17"}
    labels = [t.label for t in at.tabs]
    for lbl in ("1. Request", "2. Policy", "3. Vendors", "4. Draft PO", "5. Invoice check", "6. Critic & Audit"):
        assert lbl in labels
    assert button(at, "admin_run_btn").label == "🔁 Re-run workflow"
    assert backend.tickets[tid]["status"] == "pending_approval"
    assert "admin_approve_btn" not in _button_keys(at) and "admin_reject_btn" not in _button_keys(at)
    body = texts(at)
    assert "Stored result is persisted" in body
    assert "pending approval" in body and "PO_REQUIRES_HUMAN_APPROVAL" in body
    assert SAFETY in body
    assert "Draft — no order placed, nothing paid" in body

    click(at, "admin_audit_btn")
    assert backend.find("GET", f"/api/admin/tickets/{tid}/audit")
    assert "Saved audit history" in texts(at)
    assert not backend.find("POST", f"/api/admin/tickets/{tid}/decision")


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
    assert not backend.approvals  # hard block: no approval request, cannot be approved
    assert "admin_approve_btn" not in _button_keys(at)
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
