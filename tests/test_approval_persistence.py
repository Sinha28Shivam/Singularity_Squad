"""Phase 2 approval safety: restart durability, indexes and HTTP concurrency against real MongoDB.

Real uvicorn processes on a disposable, uniquely named database. Skipped when no MongoDB server is
reachable. Override the server with WORKPLACEOPS_TEST_MONGODB_URI (never commit credentials).
"""

import os
import tempfile
import socket
import subprocess
import sys
import threading
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
REQUESTER = {"username": "persist-requester", "name": "Persist Requester",
             "email": "persist-requester@example.com", "password": "persist-requester-pass-1"}
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
        "server on localhost:27017); skipping approval persistence tests.",
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

    def token(self, username: str, password: str) -> str:
        with self.client() as c:
            r = c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return r.json()["token"]

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


def _start(db_name: str) -> _Server:
    server = _Server(db_name)
    try:
        health = server.wait_ready()
        assert health.get("database") == "connected", health
    except Exception:
        server.stop()
        raise
    return server


@pytest.fixture(scope="module")
def test_db():
    db_name = f"workplaceops_approval_persist_{uuid4().hex[:8]}"
    assert db_name != "workplaceops"
    mongo = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        yield db_name, mongo[db_name]
    finally:
        try:
            mongo.drop_database(db_name)
        finally:
            mongo.close()


@pytest.fixture(scope="module")
def server(test_db):
    """One shared server for the concurrency tests (the restart test manages its own processes)."""
    db_name, _ = test_db
    srv = _start(db_name)
    try:
        yield srv
    finally:
        srv.stop()


def _requester_token(server: _Server) -> str:
    with server.client() as c:
        r = c.post("/api/auth/signup", json=REQUESTER)
        if r.status_code == 201:
            return r.json()["token"]
        assert r.status_code in (400, 409), r.text  # already registered
    return server.token(REQUESTER["username"], REQUESTER["password"])


def _create_pending(server: _Server, title: str) -> tuple[str, dict]:
    """Requester submits, admin runs; returns (ticket_id, approval view as seen by an approver)."""
    with server.client(_requester_token(server)) as user:
        r = user.post("/api/my/tickets", json={**HAPPY, "title": title})
        assert r.status_code == 201, r.text
        ticket_id = r.json()["ticket_id"]
    with server.client(server.token(ADMIN_USERNAME, ADMIN_PASSWORD)) as admin:
        r = admin.post(f"/api/admin/tickets/{ticket_id}/run", json=RUN_BODY)
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "pending_approval"
    with server.client(server.token("acc-approver", APPROVERS["acc-approver"])) as approver:
        r = approver.get(f"/api/tickets/{ticket_id}/approval")
        assert r.status_code == 200, r.text
        current = r.json()["current"]
        assert current and current["status"] == "pending"
        r = approver.get(f"/api/approvals/{current['approval_id']}")
        assert r.status_code == 200, r.text
        return ticket_id, r.json()


def _body(view: dict, key: str) -> dict:
    return {"action": "approve", "idempotency_key": key, "request_version": view["request_version"],
            "workflow_snapshot_hash": view["workflow_snapshot_hash"]}


def _audit_actions(db, ticket_id: str) -> list[str]:
    return [e["action"] for e in db.agent_logs.find({"ticket_id": ticket_id}).sort("sequence", 1)]


def _race(base_url: str, approval_id: str, jobs: list[tuple[str, dict]]) -> list:
    """Fire all decisions at once from separate threads/clients; returns httpx responses (or exceptions)."""
    barrier = threading.Barrier(len(jobs))
    results: list = [None] * len(jobs)

    def worker(i: int, token: str, body: dict) -> None:
        with httpx.Client(base_url=base_url, timeout=60, headers={"Authorization": f"Bearer {token}"}) as c:
            barrier.wait()
            try:
                results[i] = c.post(f"/api/approvals/{approval_id}/decision", json=body)
            except Exception as exc:  # noqa: BLE001
                results[i] = exc

    threads = [threading.Thread(target=worker, args=(i, t, b)) for i, (t, b) in enumerate(jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return results


# ---------- restart durability ----------
def test_approval_survives_api_restart(test_db):
    db_name, db = test_db

    server = _start(db_name)
    try:
        ticket_id, view = _create_pending(server, "Restart durability lunch")
        approval_id = view["approval_id"]
        with server.client(server.token("acc-approver", APPROVERS["acc-approver"])) as approver:
            body = _body(view, f"restart-{uuid4().hex}")
            r = approver.post(f"/api/approvals/{approval_id}/decision", json=body)
            assert r.status_code == 200, r.text
            assert r.json()["approval"]["status"] == "approved"
            decision1 = r.json()["approval"]["decisions"]
        with server.client(server.token(ADMIN_USERNAME, ADMIN_PASSWORD)) as admin:
            r = admin.get(f"/api/admin/tickets/{ticket_id}/audit")
            assert r.status_code == 200, r.text
            events1 = r.json()["events"]
    finally:
        server.stop()
    assert server.proc.poll() is not None

    server = _start(db_name)
    try:
        # sessions are durable too, but log in fresh to prove auth works on the new process
        with server.client(server.token("acc-approver2", APPROVERS["acc-approver2"])) as approver2:
            r = approver2.get(f"/api/approvals/{approval_id}")
            assert r.status_code == 200, r.text
            v = r.json()
            assert v["status"] == "approved"
            assert v["decisions"] == decision1
            assert len(v["decisions"]) == 1
            assert v["decisions"][0]["actor_id"] == "acc-approver"
            assert v["can_decide"] is False

            # replay on the new process: same key → stored decision; new key → 409
            with server.client(server.token("acc-approver", APPROVERS["acc-approver"])) as approver:
                r = approver.post(f"/api/approvals/{approval_id}/decision", json=body)
                assert r.status_code == 200, r.text
                assert r.json()["replayed"] is True
            r = approver2.post(f"/api/approvals/{approval_id}/decision", json=_body(view, f"k-{uuid4().hex}"))
            assert r.status_code == 409, r.text

        with server.client(server.token(ADMIN_USERNAME, ADMIN_PASSWORD)) as admin:
            r = admin.get(f"/api/admin/tickets/{ticket_id}")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "approved"
            r = admin.get(f"/api/admin/tickets/{ticket_id}/audit")
            assert r.status_code == 200, r.text
            events2 = r.json()["events"]
        # restart adds nothing; the replay and the 409 add at most approval_denied events
        assert events2[:len(events1)] == events1
        actions = [e["action"] for e in events2]
        assert actions.count("approval_requested") == 1
        assert actions.count("approval_approved") == 1
        seqs = [e["sequence"] for e in events2]
        assert seqs == list(range(1, len(seqs) + 1))

        with server.client(_requester_token(server)) as user:
            r = user.get(f"/api/my/tickets/{ticket_id}")
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "approved"
            assert r.json()["approval"]["current"]["status"] == "approved"
    finally:
        server.stop()

    doc = db.approval_requests.find_one({"approval_id": approval_id})
    assert doc["status"] == "approved"
    assert len(doc["decisions"]) == 1
    assert doc.get("reconciliation_required") is False
    pos = list(db.purchase_orders.find({"ticket_id": ticket_id}))
    assert len(pos) == 1
    assert pos[0]["status"] == "draft"
    assert pos[0]["approval_status"] == "approved"
    assert db.tickets.find_one({"ticket_id": ticket_id})["status"] == "approved"

    idx = set(db.approval_requests.index_information())
    assert {"approval_id_unique", "ticket_version_unique", "status_created", "requester_created"} <= idx
    assert db.approval_requests.index_information()["approval_id_unique"].get("unique") is True
    assert db.approval_requests.index_information()["ticket_version_unique"].get("unique") is True
    assert {"username_unique", "email_unique"} <= set(db.users.index_information())
    assert {"token_hash_unique", "expires_at_ttl"} <= set(db.sessions.index_information())
    assert {"ticket_id_unique", "owner_username_updated", "status_updated"} <= set(db.tickets.index_information())
    assert "ticket_id_sequence_unique" in db.agent_logs.index_information()


# ---------- concurrency ----------
def test_concurrent_distinct_keys_exactly_one_winner(server, test_db):
    _, db = test_db
    ticket_id, view = _create_pending(server, "Concurrent approval lunch")
    approval_id = view["approval_id"]
    tokens = [server.token("acc-approver", APPROVERS["acc-approver"]),
              server.token("acc-approver2", APPROVERS["acc-approver2"])]
    jobs = [(tokens[i % 2], _body(view, f"race-{i}-{uuid4().hex}")) for i in range(6)]

    results = _race(server.base_url, approval_id, jobs)
    errors = [r for r in results if not isinstance(r, httpx.Response)]
    assert not errors, errors
    codes = sorted(r.status_code for r in results)
    assert codes == [200] + [409] * 5, [(r.status_code, r.text) for r in results]
    winner = next(r for r in results if r.status_code == 200).json()
    assert winner["replayed"] is False

    doc = db.approval_requests.find_one({"approval_id": approval_id})
    assert doc["status"] == "approved"
    assert len(doc["decisions"]) == 1
    assert doc["decisions"][0]["idempotency_key"] == winner["approval"]["decisions"][0]["idempotency_key"]
    actions = _audit_actions(db, ticket_id)
    assert actions.count("approval_approved") == 1
    seqs = [e["sequence"] for e in db.agent_logs.find({"ticket_id": ticket_id}).sort("sequence", 1)]
    assert len(seqs) == len(set(seqs))
    assert db.tickets.find_one({"ticket_id": ticket_id})["status"] == "approved"
    po = db.purchase_orders.find_one({"ticket_id": ticket_id})
    assert po["status"] == "draft" and po["approval_status"] == "approved"


def test_duplicate_clicks_same_key_single_decision(server, test_db):
    _, db = test_db
    ticket_id, view = _create_pending(server, "Double click lunch")
    approval_id = view["approval_id"]
    token = server.token("acc-approver", APPROVERS["acc-approver"])
    body = _body(view, f"dblclick-{uuid4().hex}")

    results = _race(server.base_url, approval_id, [(token, dict(body)) for _ in range(5)])
    errors = [r for r in results if not isinstance(r, httpx.Response)]
    assert not errors, errors
    assert all(r.status_code == 200 for r in results), [(r.status_code, r.text) for r in results]
    replayed = [r.json()["replayed"] for r in results]
    assert replayed.count(False) == 1, replayed
    decision_ids = {r.json()["approval"]["decisions"][0]["decision_id"] for r in results}
    assert len(decision_ids) == 1

    doc = db.approval_requests.find_one({"approval_id": approval_id})
    assert doc["status"] == "approved"
    assert len(doc["decisions"]) == 1
    assert _audit_actions(db, ticket_id).count("approval_approved") == 1
