from fastapi.testclient import TestClient

from app.main import app


def _rfq_payload():
    return {
        "request_id": "REQ-RFQ-TEST-100",
        "service": "lunch",
        "quantity": "100",
        "budget_limit": "100000.00",
        "currency": "INR",
        "delivery_date": "2026-12-15",
        "delivery_location": "Delhi NCR office",
        "warranty_months_required": 12,
        "compliance_requirements": ["GST", "FoodSafety"],
        "response_deadline": "2026-11-15T12:00:00Z",
        "candidate_ids": ["VEN-001", "VEN-002"]
    }


def test_rfq_structured_quote_verification_and_comparison(admin_headers):
    client = TestClient(app, headers=admin_headers)
    created = client.post("/api/vendor-sourcing/rfqs", json=_rfq_payload())
    assert created.status_code == 201, created.text
    rfq = created.json()
    assert rfq["status"] == "open"
    assert rfq["candidate_ids"] == ["VEN-001", "VEN-002"]
    assert rfq["requirements_snapshot_hash"]

    quote = client.post(f"/api/vendor-sourcing/rfqs/{rfq['rfq_id']}/quotes", json={
        "vendor_id": "VEN-002",
        "quote_reference": "Q-GL-100",
        "currency": "INR",
        "unit_price": "700.00",
        "tax_amount": "3500.00",
        "delivery_fee": "2500.00",
        "other_fees": "0.00",
        "delivery_date": "2026-12-10",
        "warranty_months": 12,
        "warranty_scope": "Catering service quality and replacement guarantee",
        "compliance": {"GST": True, "FoodSafety": True},
        "notes": "All fees included"
    })
    assert quote.status_code == 201, quote.text
    assert quote.json()["status"] == "pending_verification"

    before_review = client.post(f"/api/vendor-sourcing/rfqs/{rfq['rfq_id']}/compare")
    assert before_review.status_code == 200
    assert before_review.json()["recommended_quote_id"] is None

    verified = client.post(
        f"/api/vendor-sourcing/rfqs/{rfq['rfq_id']}/quotes/{quote.json()['quote_id']}/verify",
        json={
            "currency": "INR",
            "unit_price": "700.00",
            "tax_amount": "3500.00",
            "delivery_fee": "2500.00",
            "other_fees": "0.00",
            "delivery_date": "2026-12-10",
            "warranty_months": 12,
            "warranty_scope": "Catering service quality and replacement guarantee",
            "compliance": {"GST": True, "FoodSafety": True},
            "reviewer_note": "Checked quote values and compliance evidence"
        }
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "verified"

    comparison = client.post(f"/api/vendor-sourcing/rfqs/{rfq['rfq_id']}/compare")
    assert comparison.status_code == 200, comparison.text
    body = comparison.json()
    assert body["recommended_quote_id"] == quote.json()["quote_id"]
    assert body["quotes"][0]["total_cost"] == "76000.00"
    assert body["award_decision"] == "human_review_required"
    assert "no vendor is awarded" in body["notice"].lower()


def test_rfq_rejects_unapproved_vendor_and_naive_deadline(admin_headers):
    client = TestClient(app, headers=admin_headers)
    payload = _rfq_payload()
    payload["candidate_ids"] = ["VEN-003"]
    response = client.post("/api/vendor-sourcing/rfqs", json=payload)
    assert response.status_code == 422
    assert "approved" in response.json()["detail"].lower()

    payload = _rfq_payload()
    payload["response_deadline"] = "2026-11-15T12:00:00"
    response = client.post("/api/vendor-sourcing/rfqs", json=payload)
    assert response.status_code == 422


def test_quote_compliance_and_delivery_are_gates(admin_headers):
    client = TestClient(app, headers=admin_headers)
    rfq_response = client.post("/api/vendor-sourcing/rfqs", json=_rfq_payload())
    assert rfq_response.status_code == 201, rfq_response.text
    rfq_id = rfq_response.json()["rfq_id"]
    quote_response = client.post(f"/api/vendor-sourcing/rfqs/{rfq_id}/quotes", json={
        "vendor_id": "VEN-002",
        "quote_reference": "Q-FAIL-COMPLIANCE",
        "currency": "INR",
        "unit_price": "500.00",
        "tax_amount": "0",
        "delivery_fee": "0",
        "other_fees": "0",
        "delivery_date": "2026-12-20",
        "warranty_months": 6,
        "warranty_scope": "Limited",
        "compliance": {"GST": True, "FoodSafety": False}
    })
    assert quote_response.status_code == 201, quote_response.text
    quote_id = quote_response.json()["quote_id"]
    verified = client.post(f"/api/vendor-sourcing/rfqs/{rfq_id}/quotes/{quote_id}/verify", json={
        "currency": "INR",
        "unit_price": "500.00",
        "tax_amount": "0",
        "delivery_fee": "0",
        "other_fees": "0",
        "delivery_date": "2026-12-20",
        "warranty_months": 6,
        "warranty_scope": "Limited",
        "compliance": {"GST": True, "FoodSafety": False},
        "reviewer_note": "Verified supplied terms; mandatory compliance failed"
    })
    assert verified.status_code == 200
    comparison = client.post(f"/api/vendor-sourcing/rfqs/{rfq_id}/compare")
    row = comparison.json()["quotes"][0]
    assert row["eligible"] is False
    assert row["delivery_compliant"] is False
    assert row["warranty_compliant"] is False
    assert row["missing_compliance"] == ["FoodSafety"]


def test_pdf_upload_rejects_non_pdf_and_wrong_content_type(admin_headers):
    client = TestClient(app, headers=admin_headers)
    rfq_response = client.post("/api/vendor-sourcing/rfqs", json=_rfq_payload())
    assert rfq_response.status_code == 201, rfq_response.text
    rfq_id = rfq_response.json()["rfq_id"]
    wrong_type = client.post(
        f"/api/vendor-sourcing/rfqs/{rfq_id}/quotes/pdf?vendor_id=VEN-002&quote_reference=Q-PDF",
        content=b"not a pdf",
        headers={"Content-Type": "text/plain"}
    )
    assert wrong_type.status_code == 415
    invalid_pdf = client.post(
        f"/api/vendor-sourcing/rfqs/{rfq_id}/quotes/pdf?vendor_id=VEN-002&quote_reference=Q-PDF",
        content=b"not a pdf",
        headers={"Content-Type": "application/pdf"}
    )
    assert invalid_pdf.status_code == 422
