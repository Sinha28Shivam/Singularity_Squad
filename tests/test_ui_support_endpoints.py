"""Backend endpoints supporting the React UI: ticket search/paging, upload limits, rule facts,
candidate allowed decisions/transitions and single-comparison read."""
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.policy_rag import ingestion
from app.policy_rules.facts import FACT_CURRENCIES, FACT_TYPES
from app.policy_rules.schemas import Operator
from app.vendor_sourcing import metrics
from app.vendor_sourcing.repository import ALLOWED_TRANSITIONS

from tests.test_vendor_rfq_lifecycle import _compare, _quote, _rfq, _seed_ticket, _verify


@pytest.fixture(autouse=True)
def _quote_store(monkeypatch, tmp_path):
    monkeypatch.setenv("VENDOR_QUOTE_STORE_DIR", str(tmp_path / "quote_store"))
    metrics.reset()
    yield
    metrics.reset()


SUB =  {"title": "Quarterly team lunch", "event_category": "team_lunch", "service": "lunch",
       "event_date": "2026-12-15", "attendees": 20, "budget_inr": 20000, "location": "Delhi NCR office"}


@pytest.fixture
def c():
    return TestClient(app)


def _submit(c, headers, title):
    r = c.post("/api/my/tickets", json={**SUB, "title": title}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["ticket_id"]


# ------------------------------------------------------------------------------------- ticket search/paging

def test_my_tickets_search_paging_and_isolation(c, user_headers_factory):
    alice, bob = user_headers_factory("alice"), user_headers_factory("bob")
    ids = [_submit(c, alice, f"Team lunch {i}") for i in range(3)]
    _submit(c, alice, "Offsite (dinner) [special]")
    bob_id = _submit(c, bob, "Bob team lunch")

    r = c.get("/api/my/tickets", headers=alice)
    body = r.json()
    assert r.status_code == 200 and body["total"] == 4 and body["next_offset"] is None
    assert len(body["tickets"]) == 4 and bob_id not in {t["ticket_id"] for t in body["tickets"]}

    page1 = c.get("/api/my/tickets?q=TEAM LUNCH&limit=2", headers=alice).json()
    assert page1["total"] == 3 and len(page1["tickets"]) == 2 and page1["next_offset"] == 2
    page2 = c.get("/api/my/tickets?q=team lunch&limit=2&offset=2", headers=alice).json()
    assert len(page2["tickets"]) == 1 and page2["next_offset"] is None
    got = {t["ticket_id"] for t in page1["tickets"] + page2["tickets"]}
    assert got == set(ids)

    # regex metacharacters are literal
    special = c.get("/api/my/tickets", params={"q": "(dinner) ["}, headers=alice).json()
    assert special["total"] == 1
    assert c.get("/api/my/tickets", params={"q": ".*"}, headers=alice).json()["total"] == 0

    # ticket id contains / prefix
    by_id = c.get("/api/my/tickets", params={"q": ids[0].lower()}, headers=alice).json()
    assert [t["ticket_id"] for t in by_id["tickets"]] == [ids[0]]
    assert c.get("/api/my/tickets", params={"q": bob_id}, headers=alice).json()["total"] == 0
    # owner fields are not searchable by requesters
    assert c.get("/api/my/tickets", params={"q": "alice"}, headers=alice).json()["total"] == 0

    assert c.get("/api/my/tickets?status=submitted", headers=alice).json()["total"] == 4
    assert c.get("/api/my/tickets?status=reviewed", headers=alice).json()["total"] == 0
    assert c.get("/api/my/tickets?status=bogus", headers=alice).status_code == 422
    assert c.get("/api/my/tickets", params={"q": "x" * 121}, headers=alice).status_code == 422
    assert c.get("/api/my/tickets?offset=-1", headers=alice).status_code == 422
    assert c.get("/api/my/tickets").status_code == 401


def test_admin_tickets_search_by_owner_and_counts(c, admin_headers, user_headers_factory):
    alice, bob = user_headers_factory("alice"), user_headers_factory("bob")
    for i in range(2):
        _submit(c, alice, f"Alpha lunch {i}")
    _submit(c, bob, "Beta lunch")

    body = c.get("/api/admin/tickets", headers=admin_headers).json()
    assert body["total"] == 3 and body["next_offset"] is None and body["counts"] == {"submitted": 3}

    by_owner = c.get("/api/admin/tickets", params={"q": "ALI"}, headers=admin_headers).json()
    assert by_owner["total"] == 2 and {t["owner_username"] for t in by_owner["tickets"]} == {"alice"}
    assert by_owner["counts"] == {"submitted": 3}  # global, not filtered
    by_name = c.get("/api/admin/tickets", params={"q": "Bob"}, headers=admin_headers).json()
    assert by_name["total"] == 1

    paged = c.get("/api/admin/tickets?q=lunch&limit=1&offset=1", headers=admin_headers).json()
    assert paged["total"] == 3 and len(paged["tickets"]) == 1 and paged["next_offset"] == 2
    assert c.get("/api/admin/tickets?status=reviewed", headers=admin_headers).json()["total"] == 0
    assert c.get("/api/admin/tickets", headers=alice).status_code == 403


# ------------------------------------------------------------------------------------- upload limits / facts

def test_policy_document_limits(c, admin_headers, approver_headers_factory, user_headers_factory):
    for headers in (admin_headers, approver_headers_factory("carol")):
        r = c.get("/api/policy-documents/limits", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"upload_max_mb", "upload_batch_max_mb", "max_files_per_batch", "max_pdf_pages",
                             "allowed_content_types"}
        assert body["upload_max_mb"] * 1024 * 1024 == pytest.approx(ingestion.upload_max_bytes())
        assert body["upload_batch_max_mb"] * 1024 * 1024 == pytest.approx(ingestion.batch_max_bytes())
        assert body["max_files_per_batch"] == ingestion.MAX_FILES_PER_BATCH
        assert body["max_pdf_pages"] == int(ingestion.config().get("max_pdf_pages", 500))
        assert body["allowed_content_types"] == ["application/pdf"]
    assert c.get("/api/policy-documents/limits", headers=user_headers_factory("dave")).status_code == 403
    assert c.get("/api/policy-documents/limits").status_code == 401


def test_policy_rule_facts(c, admin_headers, approver_headers_factory, user_headers_factory):
    for headers in (admin_headers, approver_headers_factory("carol")):
        r = c.get("/api/policy-rules/facts", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["operators"] == list(get_args(Operator))
        assert [f["name"] for f in body["facts"]] == list(FACT_TYPES)
        by_name = {f["name"]: f for f in body["facts"]}
        for name, ftype in FACT_TYPES.items():
            assert by_name[name]["type"] == ftype and isinstance(by_name[name]["description"], str)
            assert by_name[name].get("currency") == FACT_CURRENCIES.get(name)
        assert "currency" not in by_name["request.attendees"]
    assert c.get("/api/policy-rules/facts", headers=user_headers_factory("dave")).status_code == 403


# ------------------------------------------------------------------------------------- candidates

def test_candidate_allowed_decisions_everywhere(c, admin_headers, user_headers_factory):
    api = TestClient(app, headers=admin_headers)
    created = api.post("/api/vendor-sourcing/candidates", json={
        "name": "Allowed Decisions Catering", "services": ["lunch"], "regions": ["Delhi NCR"], "source": "manual"})
    assert created.status_code == 201, created.text
    cand = created.json()
    assert cand["allowed_decisions"] == sorted(ALLOWED_TRANSITIONS["pending_review"])
    cid = cand["candidate_id"]

    detail = api.get(f"/api/vendor-sourcing/candidates/{cid}").json()
    assert detail["allowed_decisions"] == ["approve", "reject", "suspend"]
    listed = api.get("/api/vendor-sourcing/candidates").json()
    assert listed["items"][0]["allowed_decisions"] == ["approve", "reject", "suspend"]

    decided = api.post(f"/api/vendor-sourcing/candidates/{cid}/decision",
                       json={"decision": "approve", "reason": "Documents verified"})
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved" and decided.json()["allowed_decisions"] == ["suspend"]
    assert api.get(f"/api/vendor-sourcing/candidates/{cid}").json()["allowed_decisions"] == ["suspend"]

    t = api.get("/api/vendor-sourcing/candidates/transitions")
    assert t.status_code == 200
    assert t.json() == {k: sorted(v) for k, v in ALLOWED_TRANSITIONS.items()}
    assert c.get("/api/vendor-sourcing/candidates/transitions",
                 headers=user_headers_factory("dave")).status_code == 403


# ------------------------------------------------------------------------------------- single comparison

def test_get_single_comparison_keeps_score_breakdown(admin_headers):
    _seed_ticket()
    api = TestClient(app, headers=admin_headers)
    rid = _rfq(api)["rfq_id"]
    q1 = _quote(api, rid, vendor="VEN-001")
    q2 = _quote(api, rid, vendor="VEN-002")
    _verify(api, rid, q1["quote_id"], unit_price="600.00", delivery_date="2026-12-15", warranty_months=6)
    _verify(api, rid, q2["quote_id"], unit_price="700.00", delivery_date="2026-12-05", warranty_months=24)
    cmp_ = _compare(api, rid)

    r = api.get(f"/api/vendor-sourcing/rfqs/{rid}/comparisons/{cmp_['comparison_id']}")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["comparison_id"] == cmp_["comparison_id"] and got["rfq_id"] == rid
    assert got["recommended_quote_id"] == cmp_["recommended_quote_id"]
    assert "pending_events" not in got and "_id" not in got
    for row in got["quotes"]:
        assert set(row["score"]) >= {"components", "weights", "raw"}
        assert set(row["score"]["components"]) == {"cost", "delivery", "warranty"}
    assert got["quotes"] == cmp_["quotes"]

    assert api.get(f"/api/vendor-sourcing/rfqs/{rid}/comparisons/RFQCMP-NOPE").status_code == 404
    other = _rfq(api)["rfq_id"]
    assert api.get(f"/api/vendor-sourcing/rfqs/{other}/comparisons/{cmp_['comparison_id']}").status_code == 404
    assert api.get(f"/api/vendor-sourcing/rfqs/RFQ-NOPE/comparisons/{cmp_['comparison_id']}").status_code == 404
    assert api.get(f"/api/vendor-sourcing/rfqs/{rid}/comparisons/bad id").status_code == 404
