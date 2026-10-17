"""Phase 2 approval safety: spec section 18 demo acceptance against a real uvicorn + real MongoDB.

Starts a real API process on a disposable, uniquely named database and walks the full approval
lifecycle over HTTP (happy path, separation of duties, stale/idempotent/terminal decisions, reject,
request changes + resubmission, hard block). Skipped when no MongoDB server is reachable.
Override the server with WORKPLACEOPS_TEST_MONGODB_URI (never commit credentials).
"""

import os
import tempfile
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.real_mongo

pymongo = pytest.importorskip("pymongo")
httpx = pytest.importorskip("httpx")

REPO_ROOT = Path(__file__).resolve().parent.parent
MONGO_URI = os.getenv("WORKPLACEOPS_TEST_MONGODB_URI") or "mongodb://localhost:27017/"

# test-only credentials for the disposable database
ADMIN_USERNAME = "acc-admin"
ADMIN_PASSWORD = "acc-admin-pass-123"
APPROVERS = {"acc-approver": "acc-approver-pass-1", "acc-approver2": "acc-approver-pass-2"}
APPROVER_ACCOUNTS = ",".join(f"{u}:{p}" for u, p in APPROVERS.items())

SAFETY_NOTICE = "Approval records internal authorization only. No order or payment is executed."
FORBIDDEN_CLAIMS = re.compile(r"\b(ordered|paid|purchased)\b", re.IGNORECASE)
RUN_BODY = {"invoice_id": "INV-5001", "today": "2026-10-17"}

HAPPY = {
    "title": "Quarterly team lunch",
    "event_category": "team_lunch",
    "service": "lunch",
    "event_date": "2026-10-23",
    "attendees": 100,
    "budget_inr": 100000,
    "location": "Bengaluru",
    "approved_vendor_only": True,
}
BLOCKED = {**HAPPY, "title": "Oversized team lunch", "attendees": 120, "budget_inr": 60000}


def _mongo_available() -> bool:
    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        try:
            client.admin.command("ping")
            return True
        finally:
            client.close()
    except Exception:
        return False


if not _mongo_available():
    pytest.skip(
        "Real MongoDB not reachable (set WORKPLACEOPS_TEST_MONGODB_URI or start a local "
        "server on localhost:27017); skipping approval acceptance test.",
        allow_module_level=True,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, db_name: str):
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        env = os.environ.copy()
        env.update({
            "MONGODB_URI": MONGO_URI,
            "MONGODB_DATABASE": db_name,
            "WORKPLACEOPS_PERSISTENCE": "required",
            "WORKPLACEOPS_OFFLINE": "1",
            "POLICY_RAG_SEED": "0",
            "POLICY_STORE_DIR": tempfile.mkdtemp(prefix="wops_store_"),
            "ADMIN_USERNAME": ADMIN_USERNAME,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
            "APPROVER_ACCOUNTS": APPROVER_ACCOUNTS,
        })
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(REPO_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def wait_ready(self, timeout: float = 30.0) -> dict:
        deadline = time.monotonic() + timeout
        with httpx.Client(base_url=self.base_url, timeout=2) as c:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"uvicorn exited early with code {self.proc.returncode}")
                try:
                    r = c.get("/api/health")
                    if r.status_code == 200:
                        return r.json()
                except httpx.HTTPError:
                    pass
                time.sleep(0.3)
        raise RuntimeError("uvicorn did not become ready within timeout")

    def client(self, token: str | None = None) -> "httpx.Client":
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.Client(base_url=self.base_url, timeout=60, headers=headers)

    def login(self, username: str, password: str) -> "httpx.Client":
        with self.client() as c:
            r = c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return self.client(r.json()["token"])

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


@pytest.fixture(scope="module")
def env():
    db_name = f"workplaceops_approval_acc_{uuid4().hex[:8]}"
    assert db_name != "workplaceops"
    mongo = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    server = _Server(db_name)
    clients = []
    try:
        health = server.wait_ready()
        assert health.get("database") == "connected", health
        admin = server.login(ADMIN_USERNAME, ADMIN_PASSWORD)
        approver = server.login("acc-approver", APPROVERS["acc-approver"])
        approver2 = server.login("acc-approver2", APPROVERS["acc-approver2"])
        with server.client() as c:
            r = c.post("/api/auth/signup", json={"username": "acc-requester", "name": "Acc Requester",
                                                 "email": "acc-requester@example.com",
                                                 "password": "acc-requester-pass-1"})
        assert r.status_code == 201, r.text
        assert r.json()["user"]["role"] == "user"
        user = server.client(r.json()["token"])
        clients = [admin, approver, approver2, user]
        yield {"server": server, "db": mongo[db_name], "admin": admin, "approver": approver,
               "approver2": approver2, "user": user}
    finally:
        for c in clients:
            c.close()
        server.stop()
        try:
            mongo.drop_database(db_name)
        finally:
            mongo.close()


# ---------- helpers ----------
def _submit_and_run(env, submission: dict) -> tuple[str, dict]:
    r = env["user"].post("/api/my/tickets", json=submission)
    assert r.status_code == 201, r.text
    ticket_id = r.json()["ticket_id"]
    assert r.json()["status"] == "submitted"
    r = env["admin"].post(f"/api/admin/tickets/{ticket_id}/run", json=RUN_BODY)
    assert r.status_code == 200, r.text
    return ticket_id, r.json()


def _ticket_approval(client, ticket_id: str) -> dict:
    r = client.get(f"/api/tickets/{ticket_id}/approval")
    assert r.status_code == 200, r.text
    return r.json()


def _pending(env, ticket_id: str) -> dict:
    current = _ticket_approval(env["approver"], ticket_id)["current"]
    assert current is not None and current["status"] == "pending", current
    r = env["approver"].get(f"/api/approvals/{current['approval_id']}")
    assert r.status_code == 200, r.text
    return r.json()


def _decision(view: dict, action: str, comment: str | None = None, key: str | None = None, **over) -> dict:
    body = {"action": action, "idempotency_key": key or f"key-{uuid4().hex}",
            "request_version": view["request_version"], "workflow_snapshot_hash": view["workflow_snapshot_hash"]}
    if comment is not None:
        body["comment"] = comment
    body.update(over)
    return body


def _post_decision(client, approval_id: str, body: dict):
    return client.post(f"/api/approvals/{approval_id}/decision", json=body)


def _db_approval(env, approval_id: str) -> dict:
    return env["db"].approval_requests.find_one({"approval_id": approval_id})


def _ticket_status(env, ticket_id: str) -> str:
    r = env["admin"].get(f"/api/admin/tickets/{ticket_id}")
    assert r.status_code == 200, r.text
    return r.json()["status"]


# ---------- spec section 18 ----------
def test_happy_path_approval_lifecycle(env):
    ticket_id, run = _submit_and_run(env, HAPPY)
    assert run["status"] == "pending_approval", run.get("status")
    assert run["workflow_result"]["final_status"] in ("pass", "warning")

    view = _pending(env, ticket_id)
    approval_id = view["approval_id"]
    assert approval_id.startswith("APR-")
    assert {"PO_REQUIRES_HUMAN_APPROVAL", "AMOUNT_THRESHOLD"} <= set(view["reason_codes"])
    assert view["amount"] == pytest.approx(76125)
    assert view["currency"] == "INR"
    assert view["risk_level"] == "medium"
    assert view["policy_version"] == "approval-policy-v1"
    assert view["request_version"] == 1
    assert view["requester_id"] == "acc-requester"
    assert view["required_role"] == "approver"
    assert view["can_decide"] is True
    assert view["decisions"] == []
    assert view["review_snapshot"]["po"]["total"] == pytest.approx(76125)

    # requester: sees the pending approval but cannot decide it
    r = env["user"].get(f"/api/my/tickets/{ticket_id}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"
    assert r.json()["approval"]["current"]["status"] == "pending"
    r = env["user"].get(f"/api/approvals/{approval_id}")
    assert r.status_code == 200, r.text
    assert r.json()["can_decide"] is False
    r = _post_decision(env["user"], approval_id, _decision(view, "approve"))
    assert r.status_code == 403, r.text
    assert _db_approval(env, approval_id)["status"] == "pending"
    assert _db_approval(env, approval_id)["decisions"] == []

    # unauthenticated
    with env["server"].client() as anon:
        assert _post_decision(anon, approval_id, _decision(view, "approve")).status_code == 401

    # admin (operations) is not an approver: both the approvals API and the legacy endpoint deny
    r = _post_decision(env["admin"], approval_id, _decision(view, "approve"))
    assert r.status_code == 403, r.text
    r = env["admin"].post(f"/api/admin/tickets/{ticket_id}/decision", json={"decision": "approve"})
    assert r.status_code == 403, r.text
    assert _db_approval(env, approval_id)["status"] == "pending"

    # client-supplied identity is rejected
    r = _post_decision(env["approver"], approval_id, _decision(view, "approve", actor_id="acc-approver2",
                                                               role="approver"))
    assert r.status_code == 422, r.text

    # stale version / hash, and comment rule
    r = _post_decision(env["approver"], approval_id, _decision(view, "approve", request_version=2))
    assert r.status_code == 409, r.text
    r = _post_decision(env["approver"], approval_id, _decision(view, "approve", workflow_snapshot_hash="0" * 64))
    assert r.status_code == 409, r.text
    r = _post_decision(env["approver"], approval_id, _decision(view, "reject"))
    assert r.status_code == 422, r.text
    r = _post_decision(env["approver"], approval_id, _decision(view, "reject", comment="   "))
    assert r.status_code == 422, r.text
    doc = _db_approval(env, approval_id)
    assert doc["status"] == "pending" and doc["decisions"] == []
    assert _ticket_status(env, ticket_id) == "pending_approval"

    # eligible approver approves
    key = f"approve-{uuid4().hex}"
    body = _decision(view, "approve", key=key)
    r = _post_decision(env["approver"], approval_id, body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["replayed"] is False
    assert out["reconciliation_required"] is False
    approval = out["approval"]
    assert approval["status"] == "approved"
    assert len(approval["decisions"]) == 1
    dec = approval["decisions"][0]
    assert dec["actor_id"] == "acc-approver"
    assert dec["actor_role"] == "approver"
    assert dec["action"] == "approve"
    assert dec["idempotency_key"] == key
    assert dec["request_version"] == 1
    assert approval["decided_at"]
    assert approval["safety_notice"] == SAFETY_NOTICE
    assert SAFETY_NOTICE in r.text
    assert not FORBIDDEN_CLAIMS.search(r.text), FORBIDDEN_CLAIMS.search(r.text).group(0)
    assert "draft" in out["message"].lower()

    assert _ticket_status(env, ticket_id) == "approved"
    pos = list(env["db"].purchase_orders.find({"ticket_id": ticket_id}))
    assert len(pos) == 1
    assert pos[0]["status"] == "draft"
    assert pos[0]["approval_status"] == "approved"
    assert pos[0].get("approval_by") == "acc-approver"

    r = env["user"].get(f"/api/my/tickets/{ticket_id}")
    assert r.status_code == 200, r.text
    uv = r.json()
    assert uv["status"] == "approved"
    assert uv["po_summary"]["status"] == "draft"
    assert not FORBIDDEN_CLAIMS.search(uv["outcome"])

    # replay with the same key and payload: stored decision, no duplicate
    r = _post_decision(env["approver"], approval_id, body)
    assert r.status_code == 200, r.text
    assert r.json()["replayed"] is True
    assert r.json()["approval"]["decisions"][0]["decision_id"] == dec["decision_id"]
    assert len(_db_approval(env, approval_id)["decisions"]) == 1

    # same key, different payload; different key; second approver
    r = _post_decision(env["approver"], approval_id, {**body, "comment": "changed"})
    assert r.status_code == 409, r.text
    r = _post_decision(env["approver"], approval_id, _decision(view, "approve"))
    assert r.status_code == 409, r.text
    r = _post_decision(env["approver2"], approval_id, _decision(view, "reject", comment="Too late"))
    assert r.status_code == 409, r.text
    doc = _db_approval(env, approval_id)
    assert doc["status"] == "approved" and len(doc["decisions"]) == 1

    # admin cannot re-run an approved ticket
    r = env["admin"].post(f"/api/admin/tickets/{ticket_id}/run", json=RUN_BODY)
    assert r.status_code == 409, r.text

    r = env["admin"].get(f"/api/admin/tickets/{ticket_id}/audit")
    assert r.status_code == 200, r.text
    actions = [e["action"] for e in r.json()["events"]]
    assert actions.count("approval_requested") == 1
    assert actions.count("approval_approved") == 1


def test_rejected_path(env):
    ticket_id, run = _submit_and_run(env, {**HAPPY, "title": "Lunch to be rejected"})
    assert run["status"] == "pending_approval"
    view = _pending(env, ticket_id)
    comment = "Budget not justified for this quarter."
    r = _post_decision(env["approver"], view["approval_id"], _decision(view, "reject", comment=comment))
    assert r.status_code == 200, r.text
    assert r.json()["approval"]["status"] == "rejected"
    assert r.json()["approval"]["decisions"][0]["comment"] == comment
    assert not FORBIDDEN_CLAIMS.search(r.text)

    assert _ticket_status(env, ticket_id) == "rejected"
    po = env["db"].purchase_orders.find_one({"ticket_id": ticket_id})
    assert po["status"] == "draft" and po["approval_status"] == "rejected"

    r = env["user"].get(f"/api/my/tickets/{ticket_id}")
    assert r.status_code == 200, r.text
    uv = r.json()
    assert uv["status"] == "rejected"
    assert comment in uv["outcome"]
    assert uv["approval"]["current"]["decisions"][0]["comment"] == comment
    assert "workflow_snapshot_hash" not in uv["approval"]["current"]

    r = _post_decision(env["approver2"], view["approval_id"], _decision(view, "approve"))
    assert r.status_code == 409, r.text
    r = env["admin"].post(f"/api/admin/tickets/{ticket_id}/run", json=RUN_BODY)
    assert r.status_code == 409, r.text


def test_request_changes_and_resubmission(env):
    ticket_id, run = _submit_and_run(env, {**HAPPY, "title": "Lunch needing changes"})
    assert run["status"] == "pending_approval"
    v1 = _pending(env, ticket_id)
    r = _post_decision(env["approver"], v1["approval_id"],
                       _decision(v1, "request_changes", comment="Please make the title more specific."))
    assert r.status_code == 200, r.text
    assert r.json()["approval"]["status"] == "changes_requested"
    assert _ticket_status(env, ticket_id) == "changes_requested"

    r = env["user"].get(f"/api/my/tickets/{ticket_id}")
    assert r.json()["status"] == "changes_requested"
    r = env["user"].put(f"/api/my/tickets/{ticket_id}",
                        json={**HAPPY, "title": "Quarterly engineering team lunch"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"

    # the old version cannot be revived
    r = _post_decision(env["approver"], v1["approval_id"], _decision(v1, "approve"))
    assert r.status_code == 409, r.text

    r = env["admin"].post(f"/api/admin/tickets/{ticket_id}/run", json=RUN_BODY)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending_approval"

    tav = _ticket_approval(env["approver"], ticket_id)
    assert [a["request_version"] for a in tav["history"]] == [1, 2]
    assert tav["history"][0]["status"] == "changes_requested"
    v2 = tav["current"]
    assert v2["request_version"] == 2 and v2["status"] == "pending"
    assert v2["approval_id"] != v1["approval_id"]
    assert v2["workflow_snapshot_hash"] != v1["workflow_snapshot_hash"]

    # a client holding v1's version/hash cannot approve v2
    r = _post_decision(env["approver"], v2["approval_id"], _decision(v1, "approve"))
    assert r.status_code == 409, r.text
    assert _db_approval(env, v2["approval_id"])["status"] == "pending"


def test_hard_blocked_ticket_has_no_approval(env):
    ticket_id, run = _submit_and_run(env, BLOCKED)
    assert run["workflow_result"]["final_status"] == "blocked"
    assert run["status"] == "reviewed"
    assert (run["workflow_result"].get("procurement") or {}).get("po") is None

    for client in (env["approver"], env["admin"], env["user"]):
        assert _ticket_approval(client, ticket_id)["current"] is None
    assert env["db"].approval_requests.count_documents({"ticket_id": ticket_id}) == 0
    assert env["db"].purchase_orders.count_documents({"ticket_id": ticket_id}) == 0

    r = _post_decision(env["approver"], "APR-20261017-deadbeef",
                       {"action": "approve", "idempotency_key": f"key-{uuid4().hex}", "request_version": 1,
                        "workflow_snapshot_hash": "a" * 64})
    assert r.status_code == 404, r.text
    r = env["admin"].post(f"/api/admin/tickets/{ticket_id}/decision", json={"decision": "approve"})
    assert r.status_code == 403, r.text
    assert _ticket_status(env, ticket_id) == "reviewed"

    r = env["admin"].post(f"/api/admin/tickets/{ticket_id}/decision",
                          json={"decision": "reject", "note": "Over budget and over attendee limit."})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    assert env["db"].approval_requests.count_documents({"ticket_id": ticket_id}) == 0


def test_queue_visibility(env):
    r = env["user"].get("/api/approvals")
    assert r.status_code == 403, r.text
    r = env["approver"].get("/api/approvals", params={"status": "pending"})
    assert r.status_code == 200, r.text
    for item in r.json()["approvals"]:
        assert item["status"] == "pending"
    r = env["admin"].get("/api/approvals")
    assert r.status_code == 200, r.text
    assert "counts" in r.json()
