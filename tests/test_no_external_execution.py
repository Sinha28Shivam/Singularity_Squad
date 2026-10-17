"""Draft-only boundary (spec §10.4): approval and policy modules never call billing/procurement execution or
the network; approving with all outbound I/O disabled still succeeds and the PO stays a draft."""
import ast
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import get_database
from app.main import app
from tests.policy_rag_helpers import HAPPY, TODAY, seed_basic_library

ROOT = Path(__file__).resolve().parents[1]
GUARDED = [ROOT / "app" / "approvals", ROOT / "app" / "policy_rag", ROOT / "app" / "routers" / "policy_search.py",
           ROOT / "app" / "routers" / "approvals.py"]
FORBIDDEN_MODULES = {"app.agents.billing", "app.agents.procurement", "requests", "httpx", "urllib.request",
                     "smtplib", "http.client"}
FORBIDDEN_NAMES = {"send_po", "submit_order", "place_order", "pay_invoice", "create_payment", "transfer_funds",
                   "send_email"}


def _files():
    for p in GUARDED:
        yield from ([p] if p.is_file() else sorted(p.rglob("*.py")))


def test_guarded_modules_import_no_execution_paths():
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            else:
                continue
            assert not (set(mods) & FORBIDDEN_MODULES), f"{path.name} imports {mods}"
            names = {a.name for a in node.names}
            assert not (names & FORBIDDEN_NAMES), f"{path.name} imports {names}"


@pytest.fixture
def no_network(monkeypatch):
    """Fail any non-loopback connection (asyncio on Windows uses loopback socketpairs internally)."""
    calls = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _external(address) -> bool:
        host = address[0] if isinstance(address, tuple) and address else address
        return str(host) not in ("127.0.0.1", "::1", "localhost")

    def connect(self, address):
        if _external(address):
            calls.append(address)
            raise AssertionError("external network call attempted")
        return real_connect(self, address)

    def connect_ex(self, address):
        if _external(address):
            calls.append(address)
            raise AssertionError("external network call attempted")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    return calls


def test_approve_with_policy_evidence_executes_nothing_external(admin_headers, user_headers_factory,
                                                                 approver_headers_factory, monkeypatch, no_network):
    monkeypatch.setenv("POLICY_RAG_ENABLED", "1")
    seed_basic_library()
    alice, carol = user_headers_factory("alice"), approver_headers_factory("carol")
    from app.agents import billing, procurement
    for mod, name in ((billing, "reconcile"), (procurement, "create_draft_po")):
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError("executed")))
    with TestClient(app) as c:
        tid = c.post("/api/my/tickets", json=HAPPY, headers=alice).json()["ticket_id"]
        r = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY}, headers=admin_headers)
        assert r.status_code == 200, r.text
        cur = r.json()["approval"]["current"]
        assert cur["review_snapshot"]["policy_evaluation"]["evaluation_hash"]
        body = {"action": "approve", "idempotency_key": "noexec-0001", "request_version": cur["request_version"],
                "workflow_snapshot_hash": cur["workflow_snapshot_hash"]}
        d = c.post(f"/api/approvals/{cur['approval_id']}/decision", json=body, headers=carol)
        assert d.status_code == 200, d.text
        assert d.json()["approval"]["status"] == "approved"
        assert "no order or" in d.json()["message"].lower()
        po = get_database().purchase_orders.find_one({"ticket_id": tid})
        assert po["status"] == "draft" and po.get("approval_status") == "approved"
    assert no_network == []
