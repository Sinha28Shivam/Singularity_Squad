"""Phase 3 fact registry tests (pure unit; no DB)."""
from __future__ import annotations

import json

import pytest

from app.policy_rules.facts import (FACT_CURRENCIES, FACT_TYPES, FactSnapshot, build_facts, canonical_hash,
                                    snapshot_from_values, tokenize_terms)

EXPECTED_FACTS = {
    "request.attendees": "integer", "request.budget_limit": "money", "request.service": "string",
    "request.location": "string", "request.event_date": "date", "request.event_category": "string",
    "request.currency": "string", "request.free_text_terms": "string_list",
    "request.approved_vendor_only": "boolean", "vendor.selected": "boolean", "vendor.vendor_id": "string",
    "vendor.vendor_key": "string", "vendor.approved": "boolean", "po.total": "money", "po.subtotal": "money",
    "po.tax": "money", "po.currency": "string", "po.line_count": "integer", "workflow.final_status": "string",
    "workflow.reconciliation_status": "string", "rate.validation_statuses": "string_list",
    "rate.any_fail": "boolean", "rate.any_unverified": "boolean", "rate.applicable": "boolean",
    "policy.retrieval_mode": "string", "policy.insufficient_evidence": "boolean",
}


def _ticket(**sub):
    base = {"title": "Team Offsite Lunch", "event_category": "team_lunch", "service": "lunch",
            "event_date": "2026-11-02", "attendees": 40, "budget_inr": 50000.0, "location": "Bengaluru HQ",
            "approved_vendor_only": True, "notes": "Vegetarian options please."}
    base.update(sub)
    return {"ticket_id": "TKT-1", "submission": base, "owner": {"password_hash": "x", "session_token": "s"},
            "storage_path": "C:\\secret\\file.pdf", "api_key": "sk-123"}


def _wr(po=True, **req):
    r = {"request_id": "REQ-1", "service": "lunch", "date": "2026-11-02", "attendees": 40,
         "budget_limit": 50000.0, "currency": "INR", "location": "Bengaluru HQ", "category": "employee_event"}
    r.update(req)
    out = {"request": r, "final_status": "pass",
           "vendors": {"selected": {"vendor_id": "VEN-002", "name": "X", "approved": True}},
           "reconciliation": {"status": "match"}}
    if po:
        out["procurement"] = {"po": {"po_id": "PO-1", "vendor_id": "VEN-002", "vendor_name": "X",
                                     "subtotal": 50000.0, "tax": 2500.0, "total": 52500.0, "currency": "INR",
                                     "line_items": [{"description": "a"}, {"description": "b"}]}}
    return out


def test_registry_names_and_types_exact():
    assert FACT_TYPES == EXPECTED_FACTS
    assert set(FACT_CURRENCIES) == {k for k, v in FACT_TYPES.items() if v == "money"}


def test_build_facts_typed_values():
    f = build_facts(_ticket(), _wr(), [{"validation_status": "pass"}],
                    {"retrieval_mode": "hybrid", "insufficient_evidence": False})
    assert isinstance(f, FactSnapshot)
    v = f.values
    assert v["request.attendees"] == 40
    assert v["request.budget_limit"] == "50000.00"
    assert v["po.total"] == "52500.00" and v["po.tax"] == "2500.00" and v["po.subtotal"] == "50000.00"
    assert all(isinstance(v[k], str) for k in FACT_CURRENCIES)
    assert v["request.event_date"] == "2026-11-02"
    assert v["request.event_category"] == "team_lunch"
    assert v["request.currency"] == "INR" and v["po.currency"] == "INR"
    assert v["vendor.selected"] is True and v["vendor.vendor_id"] == "VEN-002"
    assert v["vendor.vendor_key"] == "cat-ven-002" and v["vendor.approved"] is True
    assert v["po.line_count"] == 2
    assert v["workflow.final_status"] == "pass" and v["workflow.reconciliation_status"] == "match"
    assert v["rate.applicable"] is True and v["rate.any_fail"] is False and v["rate.any_unverified"] is False
    assert v["rate.validation_statuses"] == ["pass"]
    assert v["policy.retrieval_mode"] == "hybrid" and v["policy.insufficient_evidence"] is False
    assert v["request.free_text_terms"] == sorted(set(v["request.free_text_terms"]))
    assert "vegetarian" in v["request.free_text_terms"] and "offsite" in v["request.free_text_terms"]
    assert f.types == EXPECTED_FACTS
    assert f.missing == frozenset()
    assert f.currencies["po.total"] == "INR"
    assert len(f.hash) == 64


def test_money_never_float_and_precise():
    f = build_facts(_ticket(), _wr(budget_limit=0.1 + 0.2))
    assert f.values["request.budget_limit"] == "0.30"
    wr = _wr()
    wr["procurement"]["po"]["total"] = "52500.555"
    assert build_facts(_ticket(), wr).values["po.total"] == "52500.56"


def test_missing_facts_listed_not_guessed():
    f = build_facts({"ticket_id": "T"}, {})
    assert "request.attendees" in f.missing
    assert "vendor.vendor_id" in f.missing and "po.total" in f.missing
    assert "rate.applicable" in f.missing and "policy.retrieval_mode" in f.missing
    assert "request.free_text_terms" in f.missing
    assert f.values["vendor.selected"] is False      # no vendor is a definite fact
    assert set(f.values) | set(f.missing) == set(FACT_TYPES)
    assert not (set(f.values) & set(f.missing))


def test_attendees_fallback_to_submission_and_bad_values():
    wr = _wr()
    wr["request"]["attendees"] = None
    assert build_facts(_ticket(attendees=151), wr).values["request.attendees"] == 151
    wr["request"]["attendees"] = "abc"
    assert build_facts({"ticket_id": "T"}, wr).values.get("request.attendees") is None


def test_rate_facts():
    f = build_facts(_ticket(), _wr(), [])
    assert f.values["rate.applicable"] is False and f.values["rate.validation_statuses"] == []
    f = build_facts(_ticket(), _wr(), [{"validation_status": "not_applicable"}])
    assert f.values["rate.applicable"] is False
    f = build_facts(_ticket(), _wr(), [{"validation_status": "pass"}, {"validation_status": "fail"}])
    assert f.values["rate.any_fail"] is True and f.values["rate.validation_statuses"] == ["fail", "pass"]
    for s in ("needs_review", "unknown", "conflict"):
        f = build_facts(_ticket(), _wr(), [{"validation_status": s}])
        assert f.values["rate.any_unverified"] is True and f.values["rate.any_fail"] is False


def test_free_text_terms_tokenization():
    assert tokenize_terms("Photos & VIDEO, photos!! PII-data") == ["data", "photos", "pii", "video"]
    f = build_facts(_ticket(title="Annual Day", notes=None), _wr())
    assert f.values["request.free_text_terms"] == ["annual", "day"]


FORBIDDEN = ("password", "token", "session", "secret", "api_key", "storage", "path", "hash", "owner", "raw")


def test_facts_never_contain_forbidden_keys_or_values():
    f = build_facts(_ticket(notes="nothing"), _wr(), [{"validation_status": "pass", "source_document_id": "D",
                                                       "storage_path": "C:\\x"}],
                    {"retrieval_mode": "hybrid", "api_key": "sk-123", "token": "t"})
    assert set(f.values) <= set(FACT_TYPES)
    for k in f.values:
        assert not any(bad in k for bad in FORBIDDEN), k
    blob = json.dumps(f.values)
    for leak in ("sk-123", "C:\\\\secret", "password_hash", "session_token", "storage_path"):
        assert leak not in blob


def test_hash_deterministic_and_key_order_independent():
    t1, w1 = _ticket(), _wr()
    t2 = json.loads(json.dumps(t1))
    w2 = {k: w1[k] for k in reversed(list(w1))}
    a = build_facts(t1, w1, [{"validation_status": "pass"}], {"retrieval_mode": "hybrid"})
    b = build_facts(t2, w2, [{"validation_status": "pass"}], {"retrieval_mode": "hybrid"})
    assert a.hash == b.hash and a.values == b.values
    c = build_facts(_ticket(attendees=41), _wr(attendees=41))
    assert c.hash != a.hash
    assert canonical_hash({"b": 1, "a": [1, 2]}) == canonical_hash({"a": [1, 2], "b": 1})


def test_snapshot_from_values_rejects_unknown():
    s = snapshot_from_values({"request.attendees": 3})
    assert s.values == {"request.attendees": 3} and "po.total" in s.missing
    with pytest.raises(ValueError):
        snapshot_from_values({"env.secret": "x"})


def test_accepts_rate_validation_models():
    from app.policy_rag.schemas import RateValidationResult
    rv = RateValidationResult(quoted_unit_rate="100.00", currency="INR", quantity=2, quoted_total="200.00",
                              validation_status="fail", reason="differs")
    f = build_facts(_ticket(), _wr(), [rv])
    assert f.values["rate.any_fail"] is True and f.values["rate.applicable"] is True
