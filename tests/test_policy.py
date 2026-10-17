import os

os.environ["WORKPLACEOPS_OFFLINE"] = "1"

from datetime import date

from app.agents import policy
from app.agents.intake import extract_request
from app.data_loader import load_json
from app.schemas import EventRequest

TODAY = date(2026, 10, 17)
SAMPLES = load_json("sample_requests.json")


def _check(report, name):
    return next(c for c in report.checks if c.name == name)


def test_load_rules_and_clauses():
    rules = policy.load_rules()
    assert rules.per_head_catering_cap == 900
    ids = {c.clause_id for c in policy.load_clauses()}
    assert {"EVT-2.2", "EVT-3.1", "PROC-2.1", "FIN-5.4"} <= ids
    assert policy.get_clause("EVT-3.3").title == "Budget ceiling"
    assert policy.get_clause("NOPE-1") is None


def test_bm25_retrieval():
    top = policy.retrieve("duplicate invoice", k=4)
    assert top[0].clause_id == "FIN-5.4" and top[0].score > 0
    top = policy.retrieve("per person catering cap", k=4)
    assert top[0].clause_id == "EVT-3.1"
    assert policy.retrieve("duplicate invoice") == policy.retrieve("duplicate invoice")


def test_business_days():
    assert policy.business_days_between(date(2026, 10, 17), date(2026, 10, 23)) == 5
    assert policy.business_days_between(date(2026, 10, 17), date(2026, 10, 19)) == 1
    assert policy.business_days_between(date(2026, 10, 19), date(2026, 10, 19)) == 0


def test_happy_path_policy():
    req, *_ = extract_request(SAMPLES["happy_path"], TODAY)
    report = policy.evaluate(req, TODAY)
    assert all(c.status == "pass" for c in report.checks)
    assert {c.name for c in report.checks} == {
        "lead_time", "attendee_limit", "approved_vendor_requirement", "per_head_cap", "budget_rule"}
    ids = {c.clause_id for c in report.clauses}
    assert {"EVT-2.2", "EVT-2.4", "PROC-2.1", "EVT-3.1", "EVT-3.3"} <= ids
    for c in report.checks:
        assert c.evidence and c.evidence[0].source.startswith("policy:")
    assert report.summary


def test_too_soon_fails_lead_time():
    req, *_ = extract_request(SAMPLES["too_soon"], TODAY)
    report = policy.evaluate(req, TODAY)
    lt = _check(report, "lead_time")
    assert lt.status == "fail"
    assert lt.evidence[0].source == "policy:EVT-2.2"
    assert "EVT-2.2" in report.summary


def test_missing_fields_uncertain():
    req, *_ = extract_request(SAMPLES["missing_info"], TODAY)
    report = policy.evaluate(req, TODAY)
    assert _check(report, "lead_time").status == "uncertain"
    assert _check(report, "budget_rule").status == "uncertain"


def test_large_event_warns():
    report = policy.evaluate(EventRequest(date=date(2026, 11, 30), attendees=200, budget_limit=1e6), TODAY)
    assert _check(report, "attendee_limit").status == "warn"


def test_missing_clause_is_uncertain(monkeypatch):
    rules = policy.load_rules()
    rules.clause_refs["min_lead_time_business_days"] = "EVT-9.9"
    monkeypatch.setattr(policy, "load_rules", lambda: rules)
    report = policy.evaluate(EventRequest(date=date(2026, 11, 30), attendees=10, budget_limit=1e5), TODAY)
    assert _check(report, "lead_time").status == "uncertain"
