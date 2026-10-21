"""RFQ lifecycle, outbox, PDF storage, revisions, gates, scoring, human decision and metrics (Phase 2A/2C-lite)."""
import io
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pymongo.errors import PyMongoError
from pypdf import PdfWriter

from app.db.mongo import get_database
from app.main import app
from app.repositories.ticket_repository import create_ticket
from app.schemas import EventRequest, WorkflowResult
from app.vendor_sourcing import metrics, rfq_service, scoring

API = "/api/vendor-sourcing"
GST = {"status": "verified", "evidence_ref": "GSTIN-07AAACG0000A1Z5"}
FOOD = {"status": "verified", "evidence_ref": "FSSAI-10019011000000", "expires_on": "2027-06-30"}


@pytest.fixture(autouse=True)
def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("VENDOR_QUOTE_STORE_DIR", str(tmp_path / "quote_store"))
    metrics.reset()
    yield tmp_path / "quote_store"
    metrics.reset()


@pytest.fixture
def api(admin_headers):
    _seed_ticket()
    return TestClient(app, headers=admin_headers)


def _seed_ticket(request_id="REQ-RFQ-LIFE-1"):
    now = datetime.now(timezone.utc)
    request = EventRequest(request_id=request_id, service="lunch", date=date(2026, 12, 15), attendees=100,
                           budget_limit=100000, currency="INR", location="Delhi NCR office")
    result = WorkflowResult(request=request, final_status="pass")
    create_ticket({"ticket_id": request_id, "created_at": now, "updated_at": now, "status": "pass",
                   "workflow_result": result.model_dump(mode="json")})


def _rfq(api, **overrides):
    payload = {
        "request_id": "REQ-RFQ-LIFE-1", "service": "lunch", "quantity": "100", "budget_limit": "100000.00",
        "currency": "INR", "delivery_date": "2026-12-15", "delivery_location": "Delhi NCR office",
        "warranty_months_required": 6, "compliance_requirements": ["GST", "FoodSafety"],
        "response_deadline": "2026-12-01T12:00:00+05:30", "candidate_ids": ["VEN-001", "VEN-002"],
    }
    payload.update(overrides)
    r = api.post(f"{API}/rfqs", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _terms(**overrides):
    terms = {"currency": "INR", "unit_price": "700.00", "tax_amount": "3500.00", "delivery_fee": "2500.00",
             "other_fees": "0.00", "delivery_date": "2026-12-10", "warranty_months": 12,
             "warranty_scope": "Replacement guarantee", "compliance": {"GST": True, "FoodSafety": True}}
    terms.update(overrides)
    return terms


def _quote(api, rfq_id, vendor="VEN-002", expect=201, **overrides):
    body = {"vendor_id": vendor, "quote_reference": f"Q-{vendor}", **_terms(**overrides)}
    r = api.post(f"{API}/rfqs/{rfq_id}/quotes", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def _verify(api, rfq_id, quote_id, expect=200, **overrides):
    body = _terms(**{"compliance": {"GST": GST, "FoodSafety": FOOD}, **overrides})
    body["reviewer_note"] = body.pop("reviewer_note", "Checked against the source quotation")
    r = api.post(f"{API}/rfqs/{rfq_id}/quotes/{quote_id}/verify", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def _compare(api, rfq_id, expect=200):
    r = api.post(f"{API}/rfqs/{rfq_id}/compare")
    assert r.status_code == expect, r.text
    return r.json()


def _row(comparison, quote_id):
    return next(r for r in comparison["quotes"] if r["quote_id"] == quote_id)


def _pdf(pages=1, encrypt=False) -> bytes:
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=200, height=200)
    if encrypt:
        w.encrypt("secret")
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _upload(api, rfq_id, data, content_type="application/pdf", vendor="VEN-002"):
    return api.post(f"{API}/rfqs/{rfq_id}/quotes/pdf?vendor_id={vendor}&quote_reference=Q-PDF&filename=q%201.pdf",
                    content=data, headers={"Content-Type": content_type})


# ------------------------------------------------------------------------------------- RFQ states

def test_close_and_cancel_require_reason_and_valid_state(api):
    rfq = _rfq(api)
    rid = rfq["rfq_id"]
    assert rfq["quotes_received"] == 0 and rfq["accepting_quotes"] is True
    assert api.post(f"{API}/rfqs/{rid}/close", json={}).status_code == 422
    assert api.post(f"{API}/rfqs/{rid}/close", json={"reason": "    "}).status_code == 422
    assert api.post(f"{API}/rfqs/RFQ-NOPE/close", json={"reason": "No longer needed"}).status_code == 404
    closed = api.post(f"{API}/rfqs/{rid}/close", json={"reason": "Enough quotations received"})
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["status"] == "closed" and body["accepting_quotes"] is False
    assert [h["status"] for h in body["status_history"]] == ["open", "closed"]
    again = api.post(f"{API}/rfqs/{rid}/close", json={"reason": "Enough quotations received"})
    assert again.status_code == 409 and "detail" in again.json()
    assert api.post(f"{API}/rfqs/{rid}/cancel", json={"reason": "Changed my mind"}).status_code == 409
    # Closed: quotes refused, comparison still allowed.
    _quote(api, rid, expect=409)
    assert _compare(api, rid)["rfq_status"] == "closed"


def test_cancelled_rfq_refuses_quotes_verification_and_comparison(api):
    rid = _rfq(api)["rfq_id"]
    quote = _quote(api, rid)
    assert api.post(f"{API}/rfqs/{rid}/cancel", json={"reason": "Event was cancelled"}).status_code == 200
    _quote(api, rid, vendor="VEN-001", expect=409)
    assert _upload(api, rid, _pdf(), vendor="VEN-001").status_code == 409
    _verify(api, rid, quote["quote_id"], expect=409)
    assert _compare(api, rid, expect=409)["detail"]
    assert api.post(f"{API}/rfqs/{rid}/close", json={"reason": "Trying to close"}).status_code == 409


def test_quotes_refused_after_deadline_even_with_naive_stored_datetime(api):
    rid = _rfq(api)["rfq_id"]
    naive_past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    get_database().vendor_rfqs.update_one({"rfq_id": rid}, {"$set": {"response_deadline": naive_past}})
    r = api.post(f"{API}/rfqs/{rid}/quotes", json={"vendor_id": "VEN-002", "quote_reference": "Q", **_terms()})
    assert r.status_code == 409 and "deadline" in r.json()["detail"]
    got = api.get(f"{API}/rfqs/{rid}").json()["rfq"]
    assert got["accepting_quotes"] is False
    assert got["response_deadline"].endswith(("+00:00", "Z"))


def test_list_rfqs_filters_and_paginates(api):
    a = _rfq(api)["rfq_id"]
    b = _rfq(api)["rfq_id"]
    c = _rfq(api)["rfq_id"]
    api.post(f"{API}/rfqs/{b}/cancel", json={"reason": "Duplicate RFQ"})
    all_rfqs = api.get(f"{API}/rfqs").json()
    assert all_rfqs["total"] == 3 and {i["rfq_id"] for i in all_rfqs["items"]} == {a, b, c}
    open_only = api.get(f"{API}/rfqs?status=open").json()
    assert {i["rfq_id"] for i in open_only["items"]} == {a, c}
    page = api.get(f"{API}/rfqs?limit=2&offset=0").json()
    assert len(page["items"]) == 2 and page["next_offset"] == 2
    page2 = api.get(f"{API}/rfqs?limit=2&offset=2").json()
    assert len(page2["items"]) == 1 and page2["next_offset"] is None
    assert api.get(f"{API}/rfqs?request_id=REQ-OTHER").json()["total"] == 0
    assert api.get(f"{API}/rfqs?status=draft").status_code == 422
    assert api.get(f"{API}/rfqs?limit=500").status_code == 422


def test_rfq_routes_are_admin_only(api, user_headers_factory):
    rid = _rfq(api)["rfq_id"]
    user = TestClient(app, headers=user_headers_factory("bob"))
    anon = TestClient(app)
    for client, code in ((user, 403), (anon, 401)):
        assert client.get(f"{API}/rfqs").status_code == code
        assert client.post(f"{API}/rfqs/{rid}/close", json={"reason": "Not allowed here"}).status_code == code
        assert client.get(f"{API}/metrics").status_code == code
        assert client.get(f"{API}/rfqs/{rid}/quotes/QUOTE-X/pdf").status_code == code


# ------------------------------------------------------------------------------------- outbox

def test_rfq_and_quote_events_use_deterministic_ids_and_quote_count(api):
    rid = _rfq(api)["rfq_id"]
    q = _quote(api, rid)
    assert q["audit_pending"] is False
    events = api.get(f"{API}/rfqs/{rid}/events").json()
    ids = [e["event_id"] for e in events["events"]]
    assert f"{rid}:1" in ids and f"{q['quote_id']}:1" in ids
    created = next(e for e in events["events"] if e["event_id"] == f"{rid}:1")
    assert created["actor"] == "admin" and created["to_status"] == "open" and created["correlation_id"]
    assert events["pending_events"] == []
    detail = api.get(f"{API}/rfqs/{rid}").json()
    assert detail["rfq"]["quotes_received"] == 1
    assert [e["event_id"] for e in detail["events"]][:1] == [f"{rid}:1"]
    assert all("_id" not in e for e in detail["events"])


def test_audit_failure_keeps_committed_state_and_repairs(api, monkeypatch):
    real = rfq_service._insert_event

    def broken(event):
        raise PyMongoError("events down")

    monkeypatch.setattr(rfq_service, "_insert_event", broken)
    created = api.post(f"{API}/rfqs", json={
        "request_id": "REQ-RFQ-LIFE-1", "service": "lunch", "quantity": "100", "budget_limit": "100000.00",
        "currency": "INR", "delivery_date": "2026-12-15", "delivery_location": "Delhi NCR office",
        "compliance_requirements": [], "response_deadline": "2026-12-01T00:00:00Z", "candidate_ids": ["VEN-002"]},
        headers={"X-Correlation-ID": "corr-test-1"})
    assert created.status_code == 201, created.text
    rid = created.json()["rfq_id"]
    assert created.json()["audit_pending"] is True
    quote = _quote(api, rid)
    assert quote["audit_pending"] is True
    assert get_database().vendor_rfq_events.count_documents({"rfq_id": rid}) == 0
    pending = api.get(f"{API}/rfqs/{rid}/events").json()["pending_events"]
    assert {e["event_id"] for e in pending} == {f"{rid}:1", f"{quote['quote_id']}:1"}
    assert metrics.snapshot()["persistence_failures"]["by_label"]["audit_delivery"] >= 2

    monkeypatch.setattr(rfq_service, "_insert_event", real)
    first = api.post(f"{API}/rfqs/repair-events").json()
    assert first["delivered"] == 2 and first["remaining"] == 0
    assert api.post(f"{API}/rfqs/repair-events").json()["delivered"] == 0
    events = get_database().vendor_rfq_events
    assert events.count_documents({"rfq_id": rid}) == 2
    assert events.find_one({"event_id": f"{rid}:1"})["correlation_id"] == "corr-test-1"
    assert api.get(f"{API}/rfqs/{rid}").json()["rfq"]["audit_pending"] is False


def test_read_repairs_pending_events_and_redelivery_is_idempotent(api):
    rid = _rfq(api)["rfq_id"]
    coll = get_database().vendor_rfqs
    event = get_database().vendor_rfq_events.find_one({"event_id": f"{rid}:1"}, {"_id": 0})
    # Simulate a crash after delivery but before the outbox was cleared.
    coll.update_one({"rfq_id": rid}, {"$set": {"pending_events": [event]}})
    assert api.get(f"{API}/rfqs/{rid}").status_code == 200
    assert coll.find_one({"rfq_id": rid})["pending_events"] == []
    assert get_database().vendor_rfq_events.count_documents({"event_id": f"{rid}:1"}) == 1


# ------------------------------------------------------------------------------------- PDFs

def test_pdf_upload_stores_file_outside_mongo_and_download_is_safe(api, _store):
    rid = _rfq(api)["rfq_id"]
    data = _pdf(pages=2)
    r = _upload(api, rid, data)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "pending_verification" and body["pdf_page_count"] == 2
    assert body["pdf_size"] == len(data) and "pdf_storage_key" not in body and "pdf_bytes" not in body
    raw = get_database().vendor_quotes.find_one({"quote_id": body["quote_id"]})
    assert "pdf_bytes" not in raw and raw["pdf_storage_key"] and raw["pdf_sha256"] == body["pdf_sha256"]
    assert (Path(_store) / raw["pdf_storage_key"]).read_bytes() == data
    dl = api.get(f"{API}/rfqs/{rid}/quotes/{body['quote_id']}/pdf")
    assert dl.status_code == 200 and dl.content == data
    assert dl.headers["content-type"] == "application/pdf"
    assert dl.headers["content-disposition"] == 'attachment; filename="q_1.pdf"'
    assert dl.headers["x-content-type-options"] == "nosniff"
    # PDF quotes stay unverified and never rank.
    assert _compare(api, rid)["recommended_quote_id"] is None
    structured = _quote(api, rid, vendor="VEN-001")
    assert api.get(f"{API}/rfqs/{rid}/quotes/{structured['quote_id']}/pdf").status_code == 404
    assert api.get(f"{API}/rfqs/{rid}/quotes/QUOTE-MISSING/pdf").status_code == 404


@pytest.mark.parametrize("name,expected", [("../../evil.pdf", "evil.pdf"), ("..\\..\\x\\q.pdf", "q.pdf"),
                                           ("a..b.pdf", "a.b.pdf"), ("....", "quotation.pdf")])
def test_pdf_filename_is_basename_without_dot_runs(api, _store, name, expected):
    rid = _rfq(api)["rfq_id"]
    r = api.post(f"{API}/rfqs/{rid}/quotes/pdf", params={"vendor_id": "VEN-002", "quote_reference": "Q-PDF",
                                                         "filename": name},
                 content=_pdf(), headers={"Content-Type": "application/pdf"})
    assert r.status_code == 201, r.text
    assert r.json()["pdf_filename"] == expected


def test_pdf_download_detects_tampering(api, _store):
    rid = _rfq(api)["rfq_id"]
    qid = _upload(api, rid, _pdf()).json()["quote_id"]
    key = get_database().vendor_quotes.find_one({"quote_id": qid})["pdf_storage_key"]
    (Path(_store) / key).write_bytes(_pdf(pages=3))
    assert api.get(f"{API}/rfqs/{rid}/quotes/{qid}/pdf").status_code == 503


_OVERSIZED = "oversized"


@pytest.mark.parametrize("data,content_type,status", [
    (_OVERSIZED, "application/pdf", 413),
    (b"%PDF-1.4 hello", "text/plain", 415),
    (b"%PDF-1.4 hello", "multipart/form-data; boundary=x", 415),
    (b"not a pdf at all", "application/pdf", 422),
    (b"%PDF-1.7\n%garbage that is not a pdf structure\n", "application/pdf", 422),
    (b"", "application/pdf", 422),
], ids=["oversized-413", "text-415", "multipart-415", "bad-signature-422", "malformed-422", "empty-422"])
def test_pdf_upload_rejections(api, data, content_type, status, _store):
    rid = _rfq(api)["rfq_id"]
    if data == _OVERSIZED:
        data = b"%PDF-" + b"0" * (5 * 1024 * 1024)
    r = _upload(api, rid, data, content_type)
    assert r.status_code == status, r.text
    assert "detail" in r.json()
    assert get_database().vendor_quotes.count_documents({"rfq_id": rid}) == 0


@pytest.mark.parametrize("kwargs", [{"encrypt": True}, {"pages": 0}, {"pages": 21}],
                         ids=["encrypted", "zero-pages", "21-pages"])
def test_pdf_upload_rejects_encrypted_and_page_limits(api, kwargs, _store):
    rid = _rfq(api)["rfq_id"]
    r = _upload(api, rid, _pdf(**kwargs))
    assert r.status_code == 422, r.text
    assert not list(Path(_store).rglob("*.pdf")) if Path(_store).exists() else True


def test_pdf_size_limit_is_enforced_while_streaming(api):
    rid = _rfq(api)["rfq_id"]
    sent = {"bytes": 0}

    def body():
        for _ in range(20):
            chunk = b"%PDF-" + b"0" * (1024 * 1024)
            sent["bytes"] += len(chunk)
            yield chunk

    r = api.post(f"{API}/rfqs/{rid}/quotes/pdf?vendor_id=VEN-002&quote_reference=Q",
                 content=body(), headers={"Content-Type": "application/pdf"})
    assert r.status_code == 413


def test_pdf_quote_can_be_verified_and_verified_values_used(api):
    rid = _rfq(api)["rfq_id"]
    qid = _upload(api, rid, _pdf()).json()["quote_id"]
    v = _verify(api, rid, qid)
    assert v["status"] == "verified" and v["submitted"]["pdf_sha256"]
    assert "unit_price" in v["verification"]["fields_changed"]
    assert _compare(api, rid)["recommended_quote_id"] == qid


# ------------------------------------------------------------------------------------- submitted vs verified

def test_verification_keeps_submitted_values_and_records_diff(api):
    rid = _rfq(api)["rfq_id"]
    q = _quote(api, rid, unit_price="700.00")
    v = _verify(api, rid, q["quote_id"], unit_price="650.00", reviewer_note="Vendor email confirmed discount")
    assert v["submitted"]["unit_price"] == "700.00"
    assert v["verified"]["unit_price"] == "650.00" and v["unit_price"] == "650.00"
    change = next(d for d in v["verification"]["diff"] if d["field"] == "unit_price")
    assert change == {"field": "unit_price", "submitted": "700.00", "verified": "650.00"}
    assert v["verification"]["reviewer"] == "admin" and v["verification"]["note"].startswith("Vendor")
    row = _row(_compare(api, rid), q["quote_id"])
    assert row["unit_price"] == "650.00" and row["total_cost"] == "71000.00"
    # Verified once: a second verification is a conflict.
    _verify(api, rid, q["quote_id"], expect=409)


# ------------------------------------------------------------------------------------- revisions

def test_duplicate_quote_conflicts_and_revision_supersedes(api):
    rid = _rfq(api)["rfq_id"]
    first = _quote(api, rid)
    dup = _quote(api, rid, expect=409)
    assert "revise" in dup["detail"]
    r = api.post(f"{API}/rfqs/{rid}/quotes/{first['quote_id']}/revise",
                 json={"quote_reference": "Q-REV-2", "revision_reason": "Vendor lowered the price",
                       **_terms(unit_price="600.00")})
    assert r.status_code == 201, r.text
    rev = r.json()
    assert rev["revision"] == 2 and rev["revision_of"] == first["quote_id"] and rev["vendor_id"] == "VEN-002"
    quotes = {q["quote_id"]: q for q in api.get(f"{API}/rfqs/{rid}").json()["quotes"]}
    assert quotes[first["quote_id"]]["status"] == "superseded"
    assert quotes[first["quote_id"]]["superseded_by"] == rev["quote_id"]
    assert api.get(f"{API}/rfqs/{rid}").json()["rfq"]["quotes_received"] == 1
    # Superseded quotes can't be revised or verified; only the latest is compared.
    again = api.post(f"{API}/rfqs/{rid}/quotes/{first['quote_id']}/revise",
                     json={"quote_reference": "Q-REV-3", "revision_reason": "Another change",
                           **_terms()})
    assert again.status_code == 409
    _verify(api, rid, first["quote_id"], expect=409)
    _verify(api, rid, rev["quote_id"], unit_price="600.00")
    comparison = _compare(api, rid)
    assert [r["quote_id"] for r in comparison["quotes"]] == [rev["quote_id"]]
    assert comparison["quotes"][0]["revision"] == 2
    # A verified quote can still be revised (new revision returns to pending verification).
    r3 = api.post(f"{API}/rfqs/{rid}/quotes/{rev['quote_id']}/revise",
                  json={"quote_reference": "Q-REV-3", "revision_reason": "Final offer", **_terms()})
    assert r3.status_code == 201 and r3.json()["revision"] == 3
    assert api.post(f"{API}/rfqs/{rid}/quotes/QUOTE-NONE/revise",
                    json={"quote_reference": "Q", "revision_reason": "Missing", **_terms()}).status_code == 404


def test_revision_insert_failure_rolls_back_supersession(api, monkeypatch):
    rid = _rfq(api)["rfq_id"]
    first = _quote(api, rid)
    real = rfq_service._insert_quote

    def broken(doc, label):
        raise rfq_service.PersistenceError("down")

    monkeypatch.setattr(rfq_service, "_insert_quote", broken)
    r = api.post(f"{API}/rfqs/{rid}/quotes/{first['quote_id']}/revise",
                 json={"quote_reference": "Q2", "revision_reason": "Price change", **_terms()})
    assert r.status_code == 503
    monkeypatch.setattr(rfq_service, "_insert_quote", real)
    doc = get_database().vendor_quotes.find_one({"quote_id": first["quote_id"]})
    assert doc["status"] == "pending_verification" and doc["superseded_by"] is None and doc["active"] is True


def test_repair_heals_dangling_supersession(api):
    rid = _rfq(api)["rfq_id"]
    first = _quote(api, rid)
    get_database().vendor_quotes.update_one({"quote_id": first["quote_id"]}, {"$set": {
        "status": "superseded", "active": False, "superseded_by": "QUOTE-GHOST",
        "previous_status": "pending_verification"}})
    assert api.post(f"{API}/rfqs/repair-events").json()["revisions_restored"] == 1
    assert get_database().vendor_quotes.find_one({"quote_id": first["quote_id"]})["status"] == "pending_verification"


# ------------------------------------------------------------------------------------- gates

def test_budget_equal_and_same_day_delivery_are_eligible(api):
    rid = _rfq(api)["rfq_id"]
    q = _quote(api, rid)
    _verify(api, rid, q["quote_id"], unit_price="1000.00", tax_amount="0", delivery_fee="0", other_fees="0",
            delivery_date="2026-12-15", warranty_months=6)
    row = _row(_compare(api, rid), q["quote_id"])
    assert row["total_cost"] == "100000.00" and row["within_budget"] is True
    assert row["delivery_compliant"] is True and row["warranty_compliant"] is True
    assert row["eligible"] is True and row["eligibility"] == "eligible" and row["rank"] == 1


def test_one_paisa_over_budget_and_one_day_late_are_ineligible(api):
    rid = _rfq(api)["rfq_id"]
    a = _quote(api, rid, vendor="VEN-001")
    b = _quote(api, rid, vendor="VEN-002")
    _verify(api, rid, a["quote_id"], unit_price="1000.00", tax_amount="0.01", delivery_fee="0", other_fees="0")
    _verify(api, rid, b["quote_id"], delivery_date="2026-12-16")
    cmp_ = _compare(api, rid)
    assert _row(cmp_, a["quote_id"])["reason_codes"] == ["over_budget"]
    assert _row(cmp_, b["quote_id"])["reason_codes"] == ["delivery_late"]
    assert cmp_["recommended_quote_id"] is None and cmp_["eligible_count"] == 0


def test_mixed_currency_never_ranks(api):
    rid = _rfq(api)["rfq_id"]
    q = _quote(api, rid, currency="USD")
    _verify(api, rid, q["quote_id"], currency="USD", unit_price="8.00", tax_amount="0", delivery_fee="0")
    row = _row(_compare(api, rid), q["quote_id"])
    assert row["eligible"] is False and row["reason_codes"] == ["currency_mismatch"] and "rank" not in row


@pytest.mark.parametrize("field,value", [("unit_price", "700.001"), ("unit_price", "abc"),
                                         ("unit_price", "0"), ("tax_amount", "-1"), ("unit_price", "NaN"),
                                         ("currency", "inr"), ("warranty_months", 121)])
def test_invalid_values_are_422(api, field, value):
    rid = _rfq(api)["rfq_id"]
    body = {"vendor_id": "VEN-002", "quote_reference": "Q", **_terms(**{field: value})}
    assert api.post(f"{API}/rfqs/{rid}/quotes", json=body).status_code == 422


def test_compliance_gate_statuses(api):
    rid = _rfq(api)["rfq_id"]
    q1 = _quote(api, rid, vendor="VEN-001")
    q2 = _quote(api, rid, vendor="VEN-002")
    # Missing requirement entirely + declared only.
    _verify(api, rid, q1["quote_id"], compliance={"GST": {"status": "declared"}})
    # Evidence that expires before the delivery date.
    _verify(api, rid, q2["quote_id"], compliance={
        "GST": GST, "FoodSafety": {"status": "verified", "evidence_ref": "FSSAI-1", "expires_on": "2026-12-14"}})
    cmp_ = _compare(api, rid)
    r1, r2 = _row(cmp_, q1["quote_id"]), _row(cmp_, q2["quote_id"])
    assert r1["compliance_gate"] == {"GST": "review_required", "FoodSafety": "missing"}
    assert r1["missing_compliance"] == ["FoodSafety"] and r1["eligible"] is False
    assert r2["expired_compliance"] == ["FoodSafety"] and r2["reason_codes"] == ["compliance_expired"]
    # verified without evidence_ref is invalid input
    bad = _terms(compliance={"GST": {"status": "verified"}})
    bad["reviewer_note"] = "Trying to verify without evidence"
    assert api.post(f"{API}/rfqs/{rid}/quotes/{q1['quote_id']}/verify", json=bad).status_code == 422


def test_declared_only_compliance_is_review_required(api):
    rid = _rfq(api)["rfq_id"]
    q = _quote(api, rid)
    _verify(api, rid, q["quote_id"], compliance={"GST": True, "FoodSafety": FOOD})
    row = _row(_compare(api, rid), q["quote_id"])
    assert row["eligible"] is False and row["eligibility"] == "review_required"
    assert row["review_required_compliance"] == ["GST"]


def test_evidence_expiring_on_delivery_day_passes(api):
    rid = _rfq(api)["rfq_id"]
    q = _quote(api, rid)
    _verify(api, rid, q["quote_id"], compliance={
        "GST": GST, "FoodSafety": {"status": "verified", "evidence_ref": "F", "expires_on": "2026-12-15"}})
    assert _row(_compare(api, rid), q["quote_id"])["eligible"] is True


def test_insufficient_warranty_and_scope_acceptance(api):
    rid = _rfq(api, warranty_scope_required="Full replacement of spoiled food within 2 hours")["rfq_id"]
    a = _quote(api, rid, vendor="VEN-001")
    b = _quote(api, rid, vendor="VEN-002")
    _verify(api, rid, a["quote_id"], warranty_months=5, warranty_scope_accepted=True)
    _verify(api, rid, b["quote_id"], warranty_months=12)
    cmp_ = _compare(api, rid)
    assert _row(cmp_, a["quote_id"])["reason_codes"] == ["warranty_insufficient"]
    rb = _row(cmp_, b["quote_id"])
    assert rb["reason_codes"] == ["warranty_scope_not_accepted"] and rb["warranty_months_compliant"] is True


# ------------------------------------------------------------------------------------- scoring + decision

def test_ranking_is_lowest_cost_and_score_is_explanatory(api):
    rid = _rfq(api)["rfq_id"]
    cheap = _quote(api, rid, vendor="VEN-001")
    fast = _quote(api, rid, vendor="VEN-002")
    _verify(api, rid, cheap["quote_id"], unit_price="600.00", delivery_date="2026-12-15", warranty_months=6)
    _verify(api, rid, fast["quote_id"], unit_price="700.00", delivery_date="2026-12-05", warranty_months=24)
    cmp_ = _compare(api, rid)
    assert cmp_["recommended_quote_id"] == cheap["quote_id"]
    assert cmp_["scoring_policy_version"] == "vsp-1" and cmp_["scoring_policy_hash"]
    assert len(cmp_["rfq_snapshot_hash"]) == 64
    rc, rf = _row(cmp_, cheap["quote_id"]), _row(cmp_, fast["quote_id"])
    assert rc["rank"] == 1 and rf["rank"] == 2
    assert rc["score"]["components"] == {"cost": "1.0000", "delivery": "0.0000", "warranty": "0.2500"}
    assert rc["score"]["total"] == "0.6500" and rc["score"]["explanatory_only"] is True
    assert Decimal_(rf["score"]["total"]) > Decimal_(rc["score"]["total"])  # higher score, still ranked 2nd
    assert rc["quote_snapshot_hash"] == cmp_["quote_snapshot_hashes"][cheap["quote_id"]]
    # Reproducible: identical snapshots produce identical hashes and scores.
    again = _compare(api, rid)
    assert again["rfq_snapshot_hash"] == cmp_["rfq_snapshot_hash"]
    assert again["quote_snapshot_hashes"] == cmp_["quote_snapshot_hashes"]
    assert _row(again, cheap["quote_id"])["score"] == rc["score"]
    stored = api.get(f"{API}/rfqs/{rid}/comparisons").json()["items"]
    assert len(stored) == 2 and all(c["human_decision"] == "pending" for c in stored)


def Decimal_(value):
    from decimal import Decimal
    return Decimal(value)


def test_scoring_policy_validation(tmp_path):
    good = scoring.load_policy()
    assert good["version"] == "vsp-1" and sum(good["weights"].values()) == 1
    bad = tmp_path / "p.json"
    for payload in ({"version": "x", "weights": {"cost": 0.5, "delivery": 0.2, "warranty": 0.2}},
                    {"version": "x", "weights": {"cost": 1.1, "delivery": -0.1}},
                    {"version": "x", "weights": {"cost": 0.6, "speed": 0.4}},
                    {"version": "", "weights": {"cost": 1}},
                    {"version": "x", "weights": {"cost": True}}):
        bad.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(scoring.ScoringPolicyError):
            scoring.load_policy(bad)
    # 0.1 + 0.2 + 0.7 sums exactly with Decimal even though it doesn't with floats.
    bad.write_text('{"version": "x", "weights": {"cost": 0.7, "delivery": 0.1, "warranty": 0.2}}', encoding="utf-8")
    assert scoring.load_policy(bad)["version"] == "x"


def test_invalid_policy_does_not_break_comparison(api, monkeypatch, tmp_path):
    rid = _rfq(api)["rfq_id"]
    q = _quote(api, rid)
    _verify(api, rid, q["quote_id"])
    bad = tmp_path / "bad.json"
    bad.write_text('{"version": "x", "weights": {"cost": 0.9}}', encoding="utf-8")
    real = scoring.load_policy
    monkeypatch.setattr(scoring, "load_policy", lambda path=None: real(bad))
    cmp_ = _compare(api, rid)
    assert cmp_["recommended_quote_id"] == q["quote_id"]
    assert cmp_["scoring_policy_version"] is None and cmp_["scoring_error"]
    assert cmp_["quotes"][0]["score"] is None


def test_human_decision_accept_closes_rfq_and_is_single_use(api):
    rid = _rfq(api)["rfq_id"]
    a = _quote(api, rid, vendor="VEN-001")
    b = _quote(api, rid, vendor="VEN-002")
    _verify(api, rid, a["quote_id"], unit_price="600.00")
    _verify(api, rid, b["quote_id"], unit_price="650.00")
    cmp_ = _compare(api, rid)
    url = f"{API}/rfqs/{rid}/comparisons/{cmp_['comparison_id']}/decision"
    assert api.post(url, json={"decision": "accept", "reason": "Best value"}).status_code == 422
    assert api.post(url, json={"decision": "award", "quote_id": a["quote_id"], "reason": "x" * 6}).status_code == 422
    assert api.post(url, json={"decision": "accept", "quote_id": "QUOTE-NONE",
                               "reason": "Not in comparison"}).status_code == 409
    assert api.post(f"{API}/rfqs/{rid}/comparisons/RFQCMP-NONE/decision",
                    json={"decision": "reject", "reason": "Unknown comparison"}).status_code == 404
    r = api.post(url, json={"decision": "accept", "quote_id": b["quote_id"], "reason": "Preferred menu quality"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["human_decision"] == "accepted" and body["rfq_status"] == "closed"
    assert body["decision"]["deviates_from_recommendation"] is True
    assert "no award" in body["decision"]["effect"]
    assert body["award_decision"] == "human_review_required"
    assert api.get(f"{API}/rfqs/{rid}").json()["rfq"]["status"] == "closed"
    assert api.post(url, json={"decision": "reject", "reason": "Changed my mind"}).status_code == 409
    # Closed RFQs can still be compared; but a second acceptance is refused.
    cmp2 = _compare(api, rid)
    url2 = f"{API}/rfqs/{rid}/comparisons/{cmp2['comparison_id']}/decision"
    assert api.post(url2, json={"decision": "accept", "quote_id": a["quote_id"],
                                "reason": "Second acceptance"}).status_code == 409
    rej = api.post(url2, json={"decision": "reject", "reason": "Already decided earlier"})
    assert rej.status_code == 200 and rej.json()["human_decision"] == "rejected"
    types = [e["event_type"] for e in api.get(f"{API}/rfqs/{rid}/events").json()["events"]]
    assert {"comparison_accepted", "comparison_rejected", "rfq_closed", "quotes_compared"} <= set(types)
    # No PO / payment side effects.
    assert get_database().purchase_orders.count_documents({}) == 0


def test_stale_comparison_cannot_be_accepted(api):
    rid = _rfq(api)["rfq_id"]
    q = _quote(api, rid)
    _verify(api, rid, q["quote_id"])
    cmp_ = _compare(api, rid)
    rev = api.post(f"{API}/rfqs/{rid}/quotes/{q['quote_id']}/revise",
                   json={"quote_reference": "Q2", "revision_reason": "Updated price", **_terms()})
    assert rev.status_code == 201
    r = api.post(f"{API}/rfqs/{rid}/comparisons/{cmp_['comparison_id']}/decision",
                 json={"decision": "accept", "quote_id": q["quote_id"], "reason": "Accept stale"})
    assert r.status_code == 409 and "stale" in r.json()["detail"].lower()


def test_ineligible_quote_cannot_be_accepted(api):
    rid = _rfq(api)["rfq_id"]
    q = _quote(api, rid)
    cmp_ = _compare(api, rid)  # quote still unverified
    r = api.post(f"{API}/rfqs/{rid}/comparisons/{cmp_['comparison_id']}/decision",
                 json={"decision": "accept", "quote_id": q["quote_id"], "reason": "Accept unverified"})
    assert r.status_code == 409


# ------------------------------------------------------------------------------------- metrics / errors

def test_metrics_endpoint_counts_activity(api):
    rid = _rfq(api)["rfq_id"]
    q = _quote(api, rid)
    _upload(api, rid, _pdf(), vendor="VEN-001")
    _verify(api, rid, q["quote_id"], compliance={"GST": True, "FoodSafety": FOOD})
    _compare(api, rid)
    counters = api.get(f"{API}/metrics").json()["counters"]
    assert counters["rfqs_created"]["total"] == 1
    assert counters["quotes_by_type"]["by_label"] == {"structured": 1, "pdf": 1}
    assert counters["verifications"]["total"] == 1
    assert counters["comparisons"]["total"] == 1
    assert counters["ineligible_reasons"]["by_label"]["compliance_review_required"] == 1
    assert counters["ineligible_reasons"]["by_label"]["not_verified"] == 1


def test_metrics_module_is_thread_safe():
    import threading
    threads = [threading.Thread(target=lambda: [metrics.inc("x", "a") for _ in range(500)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert metrics.snapshot()["x"] == {"total": 4000, "by_label": {"a": 4000}}
    metrics.reset()
    assert metrics.snapshot() == {}


def test_persistence_failure_maps_to_503(api, monkeypatch):
    rid = _rfq(api)["rfq_id"]

    def down(*a, **k):
        raise rfq_service.PersistenceError("down")

    monkeypatch.setattr(rfq_service, "list_rfqs", down)
    r = api.get(f"{API}/rfqs")
    assert r.status_code == 503 and r.json() == {"detail": "RFQ service temporarily unavailable."}
    assert api.get(f"{API}/rfqs/{rid}").status_code == 200
    assert api.get(f"{API}/rfqs/not-an-id").status_code == 404


def test_concurrent_verification_only_one_wins(api):
    import threading
    rid = _rfq(api)["rfq_id"]
    q = _quote(api, rid)
    results = []

    def go():
        body = _terms(compliance={"GST": GST, "FoodSafety": FOOD})
        body["reviewer_note"] = "Concurrent reviewer"
        results.append(api.post(f"{API}/rfqs/{rid}/quotes/{q['quote_id']}/verify", json=body).status_code)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200] + [409] * 5
