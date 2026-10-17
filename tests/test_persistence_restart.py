"""Real-MongoDB API restart acceptance test (MONGODB_PHASE_1 section 5).

Starts real uvicorn processes against a disposable, uniquely named database on a
real MongoDB server, then verifies tickets, audit history and draft POs survive
an API restart. Skipped automatically when no MongoDB server is reachable.
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
MONGO_URI = os.getenv("WORKPLACEOPS_TEST_MONGODB_URI") or "mongodb://localhost:27017"

HAPPY_TEXT = (
    "Organize lunch for 100 employees next Friday. Keep the total under Rs 1,00,000, "
    "use an approved vendor, and follow the workplace event policy."
)
OVER_BUDGET_TEXT = (
    "Organize lunch for 120 employees next Friday at the Bengaluru office. "
    "Budget is Rs 60,000. Approved vendors only."
)
TICKET_RE = re.compile(r"^REQ-\d{8}-[0-9A-F]{8}$")
ADMIN_USERNAME = "restart-admin"
ADMIN_PASSWORD = "restart-admin-pass-123"  # test-only credential for the disposable database


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
        "server on localhost:27017); skipping API restart acceptance test.",
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
        env.update(
            {
                "MONGODB_URI": MONGO_URI,
                "MONGODB_DATABASE": db_name,
                "WORKPLACEOPS_PERSISTENCE": "required",
                "WORKPLACEOPS_OFFLINE": "1",
                "POLICY_RAG_SEED": "0",
                "POLICY_STORE_DIR": tempfile.mkdtemp(prefix="wops_store_"),
                "ADMIN_USERNAME": ADMIN_USERNAME,
                "ADMIN_PASSWORD": ADMIN_PASSWORD,
            }
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.client = httpx.Client(base_url=self.base_url, timeout=60)

    def wait_ready(self, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"uvicorn exited early with code {self.proc.returncode}")
            try:
                r = self.client.get("/api/health", timeout=2)
                if r.status_code == 200:
                    return r.json()
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise RuntimeError("uvicorn did not become ready within timeout")

    def stop(self) -> None:
        self.client.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


@pytest.fixture(scope="module")
def test_db():
    db_name = f"workplaceops_restart_test_{uuid4().hex[:8]}"
    assert db_name != "workplaceops"
    client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        yield db_name, client[db_name]
    finally:
        try:
            client.drop_database(db_name)
        finally:
            client.close()


def _run_server(db_name: str):
    server = _Server(db_name)
    try:
        health = server.wait_ready()
    except Exception:
        server.stop()
        raise
    try:
        assert server.client.get("/api/tickets").status_code == 401
        r = server.client.post("/api/auth/login",
                               json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        server.client.headers["Authorization"] = f"Bearer {r.json()['token']}"
    except Exception:
        server.stop()
        raise
    return server, health


def test_workflow_persists_across_api_restart(test_db):
    db_name, db = test_db

    # --- Server #1 ---
    server, health = _run_server(db_name)
    try:
        assert health.get("database") == "connected"

        r = server.client.post(
            "/api/workflow",
            json={"text": HAPPY_TEXT, "today": "2026-10-17", "invoice_id": "INV-5001"},
        )
        assert r.status_code == 200, r.text
        result = r.json()
        ticket_id = result["ticket_id"]
        assert TICKET_RE.match(ticket_id), ticket_id
        assert result["persisted"] is True
        assert result["final_status"] == "pass"
        assert result["reconciliation"]["status"] == "match"
        assert result["procurement"]["po"]["po_id"] == "DRAFT-PO-" + ticket_id[4:]

        r = server.client.get(f"/api/tickets/{ticket_id}")
        assert r.status_code == 200, r.text
        ticket1 = r.json()
        assert ticket1["ticket_id"] == ticket_id
        assert ticket1["status"] == "pass"
        assert ticket1["workflow_result"]["final_status"] == "pass"
        assert ticket1["workflow_result"]["persisted"] is True

        r = server.client.get(f"/api/tickets/{ticket_id}/audit")
        assert r.status_code == 200, r.text
        audit1 = r.json()
        events1 = audit1["events"]
        n = len(events1)
        assert n == len(result["audit_trail"]) and n > 0
        assert [e["sequence"] for e in events1] == list(range(1, n + 1))

        pos = list(db.purchase_orders.find({"ticket_id": ticket_id}))
        assert len(pos) == 1
        assert pos[0]["status"] == "draft"
        assert pos[0]["requires_human_approval"] is True
        assert db.tickets.count_documents({"ticket_id": ticket_id}) == 1
        assert db.agent_logs.count_documents({"ticket_id": ticket_id}) == n

        index_names = set()
        for coll in ("tickets", "purchase_orders", "agent_logs"):
            index_names.update(db[coll].index_information().keys())
        assert {"ticket_id_unique", "po_id_unique", "ticket_id_sequence_unique"} <= index_names
    finally:
        server.stop()

    # --- Server #2: new process, same database ---
    server, health = _run_server(db_name)
    try:
        assert health.get("database") == "connected"

        r = server.client.get(f"/api/tickets/{ticket_id}")
        assert r.status_code == 200, r.text
        ticket2 = r.json()
        assert ticket2["status"] == "pass"
        assert ticket2["workflow_result"] == ticket1["workflow_result"]

        r = server.client.get(f"/api/tickets/{ticket_id}/audit")
        assert r.status_code == 200, r.text
        assert r.json()["events"] == events1

        pos = list(db.purchase_orders.find({"ticket_id": ticket_id}))
        assert len(pos) == 1
        assert pos[0]["status"] == "draft"

        # Over-budget request on the restarted server: blocked, persisted, no PO.
        r = server.client.post(
            "/api/workflow",
            json={"text": OVER_BUDGET_TEXT, "today": "2026-10-17", "invoice_id": "INV-5001"},
        )
        assert r.status_code == 200, r.text
        blocked = r.json()
        blocked_id = blocked["ticket_id"]
        assert TICKET_RE.match(blocked_id), blocked_id
        assert blocked_id != ticket_id
        assert blocked["final_status"] == "blocked"
        assert blocked["persisted"] is True
        assert db.purchase_orders.count_documents({"ticket_id": blocked_id}) == 0
        assert db.tickets.count_documents({"ticket_id": blocked_id}) == 1
    finally:
        server.stop()
