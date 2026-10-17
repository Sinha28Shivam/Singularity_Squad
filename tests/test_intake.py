import os

os.environ["WORKPLACEOPS_OFFLINE"] = "1"

from datetime import date

import pytest

from app.agents.intake import extract_request
from app.data_loader import load_json

TODAY = date(2026, 10, 17)
SAMPLES = load_json("sample_requests.json")


def test_happy_path():
    req, missing, questions, llm_used = extract_request(SAMPLES["happy_path"], TODAY)
    assert req.date == date(2026, 10, 23)
    assert req.attendees == 100
    assert req.budget_limit == 100000
    assert req.service == "lunch"
    assert "approved_vendor_only" in req.requirements
    assert missing == [] and questions == []
    assert llm_used is False
    assert req.raw_text == SAMPLES["happy_path"]
    assert req.category == "employee_event"


def test_over_budget():
    req, missing, _, _ = extract_request(SAMPLES["over_budget"], TODAY)
    assert req.attendees == 120
    assert req.budget_limit == 60000
    assert req.location == "Bengaluru"
    assert req.date == date(2026, 10, 23)
    assert "approved_vendor_only" in req.requirements
    assert missing == []


def test_missing_info():
    req, missing, questions, _ = extract_request(SAMPLES["missing_info"], TODAY)
    assert set(missing) == {"date", "attendees", "budget_limit"}
    assert len(questions) == 3
    assert req.date is None and req.attendees is None and req.budget_limit is None


def test_too_soon():
    req, missing, _, _ = extract_request(SAMPLES["too_soon"], TODAY)
    assert req.date == date(2026, 10, 19)
    assert req.attendees == 40
    assert req.budget_limit == 40000
    assert missing == []


@pytest.mark.parametrize(
    "text,expected",
    [
        ("dinner for 30 people on 2026-11-05, budget ₹1,00,000", (date(2026, 11, 5), 30, 100000.0, "dinner")),
        ("breakfast for 20 people on 23 Oct 2026 under Rs 60,000", (date(2026, 10, 23), 20, 60000.0, "breakfast")),
        ("snacks for 10 people tomorrow, Budget 5000 INR", (date(2026, 10, 18), 10, 5000.0, "snacks")),
        ("lunch for 15 people this Friday, Rs 20,000", (date(2026, 10, 23), 15, 20000.0, "lunch")),
    ],
)
def test_parser_variants(text, expected):
    req, _, _, _ = extract_request(text, TODAY)
    assert (req.date, req.attendees, req.budget_limit, req.service) == expected


def test_no_service_is_missing():
    req, missing, _, _ = extract_request("Team offsite for 10 people on Monday, budget Rs 5,000", TODAY)
    assert "service" in missing
