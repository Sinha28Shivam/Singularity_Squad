import os

os.environ["WORKPLACEOPS_OFFLINE"] = "1"

from datetime import date

from fastapi.testclient import TestClient

from app.data_loader import load_json
from app.main import app
from app.orchestrator import run_workflow

TODAY = date(2026, 10, 17)
S = load_json("sample_requests.json")


def test_happy_path_replans_and_builds_po():
    r = run_workflow(S["happy_path"], TODAY)
    assert r.vendors.selected.vendor_id == "VEN-002"
    assert r.vendors.replanned
    assert r.procurement.po.total == 76125
    assert r.procurement.po.requires_human_approval
    assert r.final_status == "pass"
    assert r.audit_trail[-1].action == "human_approval_gate"


def test_happy_path_clean_invoice():
    r = run_workflow(S["happy_path"], TODAY, "INV-5001")
    assert r.reconciliation.status == "match"
    assert r.final_status == "pass"


def test_mismatched_invoice_blocks():
    r = run_workflow(S["happy_path"], TODAY, "INV-5002")
    assert r.reconciliation.status == "mismatch"
    assert r.final_status == "blocked"


def test_duplicate_invoice_blocks():
    r = run_workflow(S["happy_path"], TODAY, "INV-4987")
    assert r.reconciliation.status == "duplicate"
    assert r.final_status == "blocked"


def test_over_budget_no_po():
    r = run_workflow(S["over_budget"], TODAY)
    assert r.procurement.po is None
    assert r.final_status == "blocked"


def test_missing_info_asks_clarification():
    r = run_workflow(S["missing_info"], TODAY)
    assert r.final_status == "needs_clarification"
    assert r.clarification_questions
    assert r.policy is None


def test_short_lead_time_withholds_po():
    r = run_workflow(S["too_soon"], TODAY)
    assert r.final_status == "blocked"
    assert r.procurement.po is None


def test_api_endpoints(admin_headers):
    c = TestClient(app, headers=admin_headers)
    assert c.get("/api/health").json()["status"] == "ok"
    res = c.post("/api/workflow", json={"text": S["happy_path"], "today": "2026-10-17"}).json()
    assert res["final_status"] == "pass"
    rec = c.post("/api/reconcile", json={"po": res["procurement"]["po"], "invoice_id": "INV-5002"}).json()
    assert rec["status"] == "mismatch"
    assert c.post("/api/reconcile", json={"po": res["procurement"]["po"], "invoice_id": "NOPE"}).status_code == 404
