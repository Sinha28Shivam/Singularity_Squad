"""Shared helpers for Policy RAG tests (Agent B suites): synthetic documents, ingestion, ticket runs."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from app.policy_rag import ingestion
from app.policy_rag.schemas import PolicyDocumentMetadataIn
from tests.pdf_factory import make_pdf, text_page

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "data" / "policies"
TODAY = "2026-10-17"
HAPPY = {"title": "Quarterly team lunch", "event_category": "team_lunch", "service": "lunch",
         "event_date": "2026-10-23", "attendees": 100, "budget_inr": 100000, "location": "Bengaluru",
         "approved_vendor_only": True}

COMPANY_PAGES = [
    text_page("1. Purpose and commitment",
              "This company procurement policy governs workplace event approval, budget planning and spending. "
              "Every workplace event needs a named sponsor and an approved budget before planning starts."),
    text_page("2. Quotations",
              "Purchases above INR 50,000 require two competitive quotations retained with the approval record."),
    text_page("3. Large events and safety",
              "For events over 150 attendees a documented risk assessment, emergency plan, first aid cover and "
              "supervision arrangements are mandatory before approval."),
]
VENDOR_POLICY_PAGES = [
    text_page("1. Scope", "This event vendor and venue policy applies to all external event vendors, venues and "
                          "activity providers engaged for company events of any size."),
    text_page("4. Vendor verification",
              "Before engagement confirm vendor licenses, public liability insurance and venue facilities. "
              "Vendor verification evidence must be kept on file."),
]
PRIVACY_PAGES = [
    text_page("5. Personal data",
              "Photo handling and any collection of personal data at events requires attendee consent and a "
              "privacy review by the data protection owner."),
]


def ingest(pages, filename: str, **meta):
    meta.setdefault("document_type", "company_policy")
    return ingestion.ingest_bytes(make_pdf(pages), filename, PolicyDocumentMetadataIn(**meta), "admin")


def ingest_real(filename: str, **meta):
    return ingestion.ingest_bytes((POLICY_DIR / filename).read_bytes(), filename,
                                  PolicyDocumentMetadataIn(**meta), "admin")


def seed_basic_library():
    a = ingest(COMPANY_PAGES, "company_procurement.pdf", logical_key="company-procurement", version_label="2026-v1")
    b = ingest(VENDOR_POLICY_PAGES, "vendor_policy.pdf", document_type="vendor_policy",
               logical_key="vendor-venue-policy", version_label="1.0")
    assert a.ok and a.status == "ready", a
    assert b.ok and b.status == "ready", b
    return a, b


def rate_card_pages(record: str = "VEN-001", vendor: str = "Vendor A Events", amount: str = "₹18,000"):
    return [[
        "CORPORATE EVENT MANAGEMENT",
        vendor,
        "Illustrative corporate event planning menu",
        f"Dataset: SYNTH   Record: {record}   Currency: INR   Price basis: estimate, before applicable taxes",
        "Sample planning packages",
        "Package Illustrative scope Sample price",
        f"Team Gathering Concept, supplier coordination, event lead for 6 hours. {amount} / event",
        "Meeting",
        "Essentials",
        "Run-of-show and one coordinator for up to 4 hours.",
        "₹1,45,000 / event",
        "Optional sample add-ons",
        "Site visit",
        "One local walkthrough and written logistics checklist — ₹4,500.",
        "Sample terms & service standards",
        "Taxes are not included in sample prices.",
    ]]


def today() -> dt.date:
    return dt.date(2026, 10, 17)
