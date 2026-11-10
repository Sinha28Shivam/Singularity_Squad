"""Read-only spend analytics API: role access, aggregations over an approved ticket, CSV export + injection guard."""
import csv
import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.analytics.service import CSV_COLUMNS, csv_safe
from app.db import get_database
from app.main import app
from tests.test_payment_api import BASE, HAPPY, TODAY, approved_ticket, body_for

SUMMARY = "/api/analytics/summary"
EXPORT = "/api/analytics/export.csv"


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def actors(admin_headers, user_headers_factory, approver_headers_factory):
    return admin_headers, user_headers_factory("alice"), approver_headers_factory("carol")


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


# ---------- role access ----------
def test_requires_auth(c):
    assert c.get(SUMMARY).status_code == 401
    assert c.get(EXPORT).status_code == 401


def test_user_forbidden_admin_and_approver_allowed(c, actors):
    admin, alice, carol = actors
    for path in (SUMMARY, EXPORT):
        assert c.get(path, headers=alice).status_code == 403
        assert c.get(path, headers=admin).status_code == 200
        assert c.get(path, headers=carol).status_code == 200


def test_empty_summary_shape(c, actors):
    admin, _, _ = actors
    data = c.get(SUMMARY, headers=admin).json()
    assert data["total_requests"] == 0 and data["approved_count"] == 0
    assert data["draft_po_total"] == "0.00" and data["currency"] == "INR"
    assert data["spend_by_vendor"] == [] and data["top_policy_blocks"] == []
    assert data["invoice_exception_rate"] is None
    assert data["simulated_payments"] == {"count": 0, "total": "0.00"}
    assert data["cycle_time_hours"] == {"submit_to_approve_median": None, "approve_to_payment_sim_median": None}


def test_invalid_range_rejected(c, actors):
    admin, _, _ = actors
    assert c.get(SUMMARY, params={"from": "2026-12-01", "to": "2026-11-01"}, headers=admin).status_code == 422
    assert c.get(SUMMARY, params={"from": "not-a-date"}, headers=admin).status_code == 422


# ---------- aggregations ----------
def test_summary_aggregates_approved_ticket_blocks_and_payment(c, actors, monkeypatch):
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "1")
    admin, alice, carol = actors
    tid, ticket = approved_ticket(c, admin, alice, carol)
    po = ticket["workflow_result"]["procurement"]["po"]
    r = c.post(BASE, json=body_for(tid, ticket), headers=admin)
    assert r.status_code == 201, r.text
    # A hard-blocked ticket (lead time + no vendor) feeds top_policy_blocks.
    bad = dict(HAPPY, event_date="2026-10-18", attendees=500)
    tid2 = c.post("/api/my/tickets", json=bad, headers=alice).json()["ticket_id"]
    run = c.post(f"/api/admin/tickets/{tid2}/run", json={"today": TODAY}, headers=admin)
    assert run.status_code == 200 and run.json()["workflow_result"]["final_status"] == "blocked"

    for headers in (admin, carol):
        data = c.get(SUMMARY, params={"from": today(), "to": today()}, headers=headers).json()
        assert data["total_requests"] == 2 and data["approved_count"] == 1
        assert data["tickets_by_status"]["approved"] == 1
        expected = f"{float(po['total']):.2f}"
        assert data["draft_po_total"] == expected and data["currency"] == "INR"
        assert data["spend_by_vendor"] == [{"vendor_id": po["vendor_id"], "vendor_name": po["vendor_name"],
                                            "total": expected, "count": 1}]
        assert data["spend_by_category"] == [{"category": "team_lunch", "total": expected, "count": 1}]
        assert data["spend_by_month"] == [{"month": today()[:7], "total": expected, "count": 1}]
        assert data["budget_vs_actual"] == [{"category": "team_lunch", "budget_total": "100000.00",
                                             "po_total": expected}]
        assert data["invoice_exception_rate"] == 0.0 and data["invoices_reconciled"] == 1
        codes = {b["code"]: b["count"] for b in data["top_policy_blocks"]}
        assert codes.get("policy:lead_time") == 1, codes
        assert data["simulated_payments"] == {"count": 1, "total": expected}
        ct = data["cycle_time_hours"]
        assert ct["submit_to_approve_median"] is not None and ct["submit_to_approve_median"] >= 0
        assert ct["approve_to_payment_sim_median"] is not None and ct["approve_to_payment_sim_median"] >= 0
        text = str(data)
        assert "alice@example.com" not in text and "Organize lunch" not in text

    past = c.get(SUMMARY, params={"from": "2000-01-01", "to": "2000-01-31"}, headers=admin).json()
    assert past["total_requests"] == 0 and past["simulated_payments"]["count"] == 0


def test_exception_rate_counts_mismatch(c, actors):
    admin, alice, carol = actors
    tid, _ = approved_ticket(c, admin, alice, carol)
    tid2, _ = approved_ticket(c, admin, alice, carol, invoice_id="INV-5002")
    get_database().tickets.update_one({"ticket_id": tid2},
                                      {"$set": {"workflow_result.reconciliation.status": "mismatch"}})
    data = c.get(SUMMARY, headers=admin).json()
    assert data["invoices_reconciled"] == 2 and data["invoice_exceptions"] == 1
    assert data["invoice_exception_rate"] == 0.5


# ---------- CSV export ----------
def test_export_csv_headers_rows_and_injection_guard(c, actors, monkeypatch):
    monkeypatch.setenv("PAYMENT_SIMULATION_ENABLED", "1")
    admin, alice, carol = actors
    tid, ticket = approved_ticket(c, admin, alice, carol)
    assert c.post(BASE, json=body_for(tid, ticket), headers=admin).status_code == 201
    get_database().tickets.update_one({"ticket_id": tid}, {"$set": {
        "workflow_result.procurement.po.vendor_id": "=HYPERLINK(\"http://evil\")",
        "submission.event_category": "+cmd|' /C calc'!A0"}})
    r = c.get(EXPORT, params={"from": today(), "to": today()}, headers=carol)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == CSV_COLUMNS == ["ticket_id", "category", "event_date", "vendor_id", "po_id", "po_total",
                                      "currency", "status", "invoice_status", "simulated_payment_status"]
    assert len(rows) == 2
    row = dict(zip(rows[0], rows[1]))
    assert row["ticket_id"] == tid and row["status"] == "approved"
    assert row["vendor_id"] == "'=HYPERLINK(\"http://evil\")"
    assert row["category"].startswith("'+")
    assert row["po_total"] == f"{float(ticket['workflow_result']['procurement']['po']['total']):.2f}"
    assert row["currency"] == "INR" and row["invoice_status"] == "match"
    assert row["simulated_payment_status"] == "accepted_by_simulator"
    assert "alice" not in r.text and "Organize" not in r.text


def test_export_excludes_unapproved_and_out_of_range(c, actors):
    admin, alice, carol = actors
    approved_ticket(c, admin, alice, carol, approve=False)
    rows = list(csv.reader(io.StringIO(c.get(EXPORT, headers=admin).text)))
    assert rows == [CSV_COLUMNS]


@pytest.mark.parametrize("raw,expected", [
    ("=1+1", "'=1+1"), ("+1", "'+1"), ("-1", "'-1"), ("@SUM(A1)", "'@SUM(A1)"),
    ("VEN-002", "VEN-002"), (None, ""), ("76125.00", "76125.00"),
])
def test_csv_safe(raw, expected):
    assert csv_safe(raw) == expected
