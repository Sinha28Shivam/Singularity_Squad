"""Phase 3 restricted rule language + deterministic evaluator tests (pure unit; no DB)."""
from __future__ import annotations

import datetime as dt
import json

import pytest
from pydantic import ValidationError

from app.policy_rules.evaluator import (aggregate, canonical_hash, canonical_rule_hash, describe_expression,
                                        evaluate_expression, evaluate_rule, load_seed_bundle, validate_expression,
                                        validate_rule)
from app.policy_rules.facts import build_facts, snapshot_from_values
from app.policy_rules.schemas import (MAX_EXPRESSION_DEPTH, Condition, CoverageItem, PolicyRule, RuleResult)

NOW = dt.datetime(2026, 10, 17, 9, 0, tzinfo=dt.timezone.utc)

FACTS = snapshot_from_values({
    "request.attendees": 150, "request.budget_limit": "50000.00", "request.service": "lunch",
    "request.event_date": "2026-11-02", "request.currency": "INR",
    "request.free_text_terms": ["offsite", "photos", "team"], "request.approved_vendor_only": True,
    "vendor.selected": True, "vendor.vendor_id": "VEN-002", "po.total": "52500.00", "po.line_count": 2,
    "rate.applicable": True, "rate.any_fail": False, "rate.validation_statuses": ["pass"],
    "policy.retrieval_mode": "hybrid",
})


def C(field, op, value=None, vt="integer", **kw):
    d = {"field": field, "op": op, "value_type": vt, **kw}
    if value is not None:
        d["value"] = value
    return d


def ev(expr, facts=FACTS):
    return evaluate_expression(expr, facts)[0]


def rule(condition, *, decision="review_required", severity="review", mandatory=False, applies_when=None,
         rule_id="RULE-TEST-ONE", version=1, **kw) -> PolicyRule:
    data = {"rule_id": rule_id, "rule_version": version, "name": "Test rule", "severity": severity,
            "condition": condition, "applies_when": applies_when, "mandatory": mandatory,
            "outcome": {"decision": decision, "reason_code": "TEST_CODE", "message": "Test message."},
            "created_by": "tester", "created_at": "2026-10-01T00:00:00Z", "source": "manual"}
    data.update(kw)
    return PolicyRule.model_validate(data)


# ----------------------------------------------------------------------------------------- operators
@pytest.mark.parametrize("op,value,expected", [
    ("eq", 150, True), ("eq", 149, False), ("ne", 150, False), ("ne", 1, True),
    ("gt", 149, True), ("gt", 150, False), ("gte", 150, True), ("gte", 151, False),
    ("lt", 151, True), ("lt", 150, False), ("lte", 150, True), ("lte", 149, False),
    ("in", [1, 150], True), ("in", [1, 2], False), ("not_in", [1, 2], True), ("not_in", [150], False),
    ("between", [150, 200], True), ("between", [100, 150], True), ("between", [151, 200], False),
])
def test_integer_operators_with_boundaries(op, value, expected):
    assert validate_expression(C("request.attendees", op, value)) == []
    assert ev(C("request.attendees", op, value)) is expected


@pytest.mark.parametrize("op,value,expected", [
    ("gt", "52499.99", True), ("gt", "52500.00", False), ("gte", "52500", True), ("lt", "52500.01", True),
    ("lte", "52500.00", True), ("eq", "52500", True), ("ne", "52500.00", False),
    ("between", ["52500.00", "60000"], True), ("between", ["52500.01", "60000"], False),
])
def test_money_operators_decimal(op, value, expected):
    c = C("po.total", op, value, "money", currency="INR")
    assert validate_expression(c) == []
    assert ev(c) is expected


def test_string_boolean_list_ops():
    assert ev(C("request.service", "eq", "lunch", "string")) is True
    assert ev(C("request.service", "ne", "lunch", "string")) is False
    assert ev(C("request.service", "in", ["dinner", "lunch"], "string")) is True
    assert ev(C("request.service", "not_in", ["dinner"], "string")) is True
    assert ev(C("vendor.selected", "eq", True, "boolean")) is True
    assert ev(C("vendor.selected", "ne", True, "boolean")) is False
    assert ev(C("request.free_text_terms", "contains_any", ["photos", "video"], "string_list")) is True
    assert ev(C("request.free_text_terms", "contains_any", ["video"], "string_list")) is False


def test_exists_and_is_missing():
    assert ev(C("request.attendees", "exists")) is True
    assert ev(C("request.location", "exists", vt="string")) is False
    assert ev(C("request.location", "is_missing", vt="string")) is True
    assert evaluate_expression(C("request.location", "is_missing", vt="string"), FACTS)[1] == []


@pytest.mark.parametrize("op,value,expected", [
    ("date_before", "2026-11-03", True), ("date_before", "2026-11-02", False),
    ("date_after", "2026-11-01", True), ("date_after", "2026-11-02", False),
    ("date_between", ["2026-11-02", "2026-11-02"], True), ("date_between", ["2026-11-03", "2026-12-01"], False),
    ("eq", "2026-11-02", True),
])
def test_date_ops_boundaries(op, value, expected):
    c = C("request.event_date", op, value, "date")
    assert validate_expression(c) == []
    assert ev(c) is expected


# --------------------------------------------------------------------------------------- composition
def test_all_any_not_composition():
    t = C("request.attendees", "gte", 100)
    f = C("request.attendees", "lt", 100)
    assert ev({"all": [t, t]}) is True and ev({"all": [t, f]}) is False
    assert ev({"any": [f, t]}) is True and ev({"any": [f, f]}) is False
    assert ev({"not": f}) is True and ev({"not": t}) is False
    assert ev({"all": [t, {"not": {"any": [f, f]}}]}) is True


def test_three_valued_missing_facts():
    m = C("request.location", "eq", "HQ", "string")
    t = C("request.attendees", "gte", 1)
    f = C("request.attendees", "lt", 1)
    assert evaluate_expression(m, FACTS) == (None, ["request.location"], ["request.location"])
    assert ev({"all": [m, f]}) is False
    assert ev({"all": [m, t]}) is None
    assert ev({"any": [m, t]}) is True
    assert ev({"any": [m, f]}) is None
    assert ev({"not": m}) is None
    res, missing, refs = evaluate_expression({"any": [m, t]}, FACTS)
    assert res is True and missing == [] and refs == ["request.attendees", "request.location"]


# ---------------------------------------------------------------------------------------- validation
def test_unknown_operator_and_type_rejected_by_schema():
    with pytest.raises(ValidationError):
        Condition.model_validate(C("request.attendees", "regex", "x"))
    with pytest.raises(ValidationError):
        Condition.model_validate(C("request.attendees", "eq", 1, "float"))
    assert validate_expression(C("request.attendees", "matches", ".*"))
    assert validate_expression({"xor": [C("request.attendees", "eq", 1)]})


def test_unknown_field_rejected():
    errs = validate_expression(C("env.secret_key", "eq", "x", "string"))
    assert errs and "unknown field" in errs[0]
    errs = validate_rule(rule(C("request.password", "eq", "x", "string")))
    assert any("unknown field" in e for e in errs)


@pytest.mark.parametrize("cond", [
    C("request.attendees", "eq", "150", "integer"),                   # wrong value type
    C("request.attendees", "eq", 150, "string"),                      # value_type != fact type
    C("request.attendees", "date_before", "2026-01-01", "integer"),   # date op on integer
    C("request.service", "gt", "a", "string"),                        # numeric op on string
    C("request.service", "in", "lunch", "string"),                    # in requires list
    C("request.service", "in", [], "string"),
    C("request.free_text_terms", "contains_any", "photos", "string_list"),
    C("request.free_text_terms", "contains_any", ["two words"], "string_list"),
    C("request.free_text_terms", "eq", "photos", "string_list"),
    C("request.service", "contains_any", ["x"], "string"),
    C("request.event_date", "date_before", "02/11/2026", "date"),
    C("request.event_date", "date_between", ["2026-12-01", "2026-11-01"], "date"),
    C("request.event_date", "gt", "2026-11-01", "date"),
    C("request.attendees", "between", [200, 100]),
    C("request.attendees", "between", [100]),
    C("request.attendees", "exists", 3),
    C("vendor.selected", "eq", "true", "boolean"),
    C("request.attendees", "gte", 10, unit="kg"),
    C("request.service", "eq", "lunch", "string", currency="INR"),
])
def test_type_and_shape_errors_rejected(cond):
    assert validate_expression(cond), cond


def test_money_currency_rules():
    assert validate_expression(C("po.total", "gt", "100", "money")) != []                    # no currency
    errs = validate_expression(C("po.total", "gt", "100", "money", currency="USD"))
    assert errs and "currency" in errs[0]
    assert validate_expression(C("po.total", "gt", "100", "money", currency="INR")) == []
    assert validate_expression(C("po.total", "gt", 100.5, "money", currency="INR"))           # schema: no floats


def test_unit_compatible():
    assert validate_expression(C("request.attendees", "gte", 150, unit="attendees")) == []


def _nest(depth):
    e = C("request.attendees", "gte", 1)
    for _ in range(depth):
        e = {"not": e}
    return e


def test_depth_limit():
    assert validate_expression(_nest(MAX_EXPRESSION_DEPTH)) == []
    errs = validate_expression(_nest(MAX_EXPRESSION_DEPTH + 1))
    assert errs and "depth" in errs[0]


def test_node_limit():
    leaf = C("request.attendees", "gte", 1)
    ok = {"all": [{"all": [leaf] * 15}] * 4}                     # 1 + 4 + 60 = 65 > 64
    assert any("nodes" in e for e in validate_expression(ok))
    small = {"all": [{"all": [leaf] * 15}] * 3}                  # 1 + 3 + 45 = 49
    assert validate_expression(small) == []


def test_validate_rule_dict_and_versions():
    r = rule(C("request.attendees", "gte", 1))
    assert validate_rule(r) == []
    assert validate_rule(r.model_dump(mode="json", by_alias=True)) == []
    bad = r.model_copy(update={"evaluator_compatibility_version": "rule-evaluator-v0"})
    assert any("evaluator_compatibility_version" in e for e in validate_rule(bad))
    bad = r.model_copy(update={"schema_version": "9"})
    assert any("schema_version" in e for e in validate_rule(bad))
    assert validate_rule({"rule_id": "nope"})
    aw = rule(C("request.attendees", "gte", 1), applies_when=C("x.unknown", "eq", 1))
    assert any(e.startswith("applies_when") for e in validate_rule(aw))


# ---------------------------------------------------------------------------------------- evaluate_rule
def test_evaluate_rule_outcome_mapping():
    cond = C("request.attendees", "gte", 150)
    for decision, status in (("blocked", "fail"), ("review_required", "review_required"),
                             ("needs_clarification", "review_required"), ("pass", "pass")):
        r = evaluate_rule(rule(cond, decision=decision), FACTS, 3, NOW)
        assert r.status == status and r.decision == decision and r.reason_code == "TEST_CODE"
        assert r.policy_set_version == 3 and r.fact_refs == ["request.attendees"]
        assert r.explanation == "Test message."
    r = evaluate_rule(rule(C("request.attendees", "gte", 151), decision="blocked"), FACTS, 3, NOW)
    assert r.status == "pass" and r.decision is None and r.reason_code is None


def test_applies_when_false_not_applicable():
    r = evaluate_rule(rule(C("request.attendees", "gte", 1), applies_when=C("vendor.selected", "eq", False,
                                                                              "boolean")), FACTS, 1, NOW)
    assert r.status == "not_applicable" and r.fact_refs == ["vendor.selected"]


def test_missing_fact_mandatory_vs_not():
    cond = C("request.location", "eq", "HQ", "string")
    r = evaluate_rule(rule(cond, mandatory=True, decision="blocked"), FACTS, 1, NOW)
    assert r.status == "review_required" and r.missing_fields == ["request.location"]
    assert r.decision == "review_required" and r.reason_code == "MANDATORY_FACTS_MISSING"
    r = evaluate_rule(rule(cond, mandatory=False, decision="blocked"), FACTS, 1, NOW)
    assert r.status == "not_applicable" and r.missing_fields == ["request.location"] and r.warnings
    r = evaluate_rule(rule(C("request.attendees", "gte", 1), applies_when=cond, mandatory=True), FACTS, 1, NOW)
    assert r.status == "review_required" and r.missing_fields == ["request.location"]


def test_type_mismatch_at_runtime_is_evaluation_error():
    bad_facts = snapshot_from_values({"request.attendees": "150"})       # string instead of int
    r = evaluate_rule(rule(C("request.attendees", "gte", 1)), bad_facts, 1, NOW)
    assert r.status == "evaluation_error" and r.errors and r.decision == "review_required"


def test_money_currency_mismatch_runtime_is_evaluation_error():
    usd = snapshot_from_values({"po.total": "100.00"}, currencies={"po.total": "USD"})
    r = evaluate_rule(rule(C("po.total", "gt", "10", "money", currency="INR")), usd, 1, NOW)
    assert r.status == "evaluation_error" and "currency" in r.errors[0]


def test_invalid_rule_never_passes():
    r = rule(C("request.attendees", "gte", 1))
    r = r.model_copy(update={"condition": Condition.model_construct(field="nope.field", op="eq", value=1,
                                                                    value_type="integer")})
    res = evaluate_rule(r, FACTS, 1, NOW)
    assert res.status == "evaluation_error" and res.errors


def test_effective_window_boundaries():
    cond = C("request.attendees", "gte", 1)
    d = dt.date(2026, 11, 2)
    r1 = evaluate_rule(rule(cond, effective_from="2026-11-02"), FACTS, 1, NOW)      # as_of = event date
    assert r1.status == "review_required"
    r2 = evaluate_rule(rule(cond, effective_from="2026-11-03"), FACTS, 1, NOW)
    assert r2.status == "not_applicable" and r2.warnings
    r3 = evaluate_rule(rule(cond, effective_until="2026-11-02"), FACTS, 1, NOW)
    assert r3.status == "review_required"
    r4 = evaluate_rule(rule(cond, effective_until="2026-11-01"), FACTS, 1, NOW)
    assert r4.status == "not_applicable" and r4.warnings
    r5 = evaluate_rule(rule(cond, effective_until="2026-11-01"), FACTS, 1, NOW, as_of=d - dt.timedelta(days=1))
    assert r5.status == "review_required"
    # no event date -> UTC date of now (late evening IST is still the previous UTC day)
    nf = snapshot_from_values({"request.attendees": 5})
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now_ist = dt.datetime(2026, 10, 18, 2, 0, tzinfo=ist)                            # = 2026-10-17 20:30 UTC
    r6 = evaluate_rule(rule(cond, effective_from="2026-10-18"), nf, 1, now_ist)
    assert r6.status == "not_applicable"


def test_determinism():
    r = rule({"all": [C("request.attendees", "gte", 100), C("po.total", "gt", "1", "money", currency="INR")]},
             decision="blocked")
    a = evaluate_rule(r, FACTS, 1, NOW).model_dump(mode="json")
    b = evaluate_rule(r, FACTS, 1, NOW + dt.timedelta(hours=1)).model_dump(mode="json")
    a.pop("evaluated_at"), b.pop("evaluated_at")
    assert a == b


def test_canonical_rule_hash_stable():
    r = rule(C("request.attendees", "gte", 150))
    d = r.model_dump(mode="json", by_alias=True)
    shuffled = json.loads(json.dumps({k: d[k] for k in reversed(list(d))}))
    assert canonical_rule_hash(r) == canonical_rule_hash(shuffled)
    approved = r.model_copy(update={"status": "approved", "approved_by": "rev", "approved_at": NOW,
                                    "reviewed_by": "rev", "validation_errors": []})
    assert canonical_rule_hash(approved) == canonical_rule_hash(r)
    changed = rule(C("request.attendees", "gte", 151))
    assert canonical_rule_hash(changed) != canonical_rule_hash(r)
    assert canonical_hash({"a": 1, "b": [2]}) == canonical_hash({"b": [2], "a": 1})


def test_describe_expression():
    s = describe_expression({"all": [C("request.attendees", "gte", 150), {"not": C("vendor.selected", "eq", True,
                                                                                    "boolean")}]})
    assert s == "(request.attendees ≥ 150 AND NOT (vendor.selected = true))"


# ------------------------------------------------------------------------------------------- aggregation
def _res(status, decision=None, rid="RULE-A", code="X_CODE", severity="review"):
    return RuleResult(rule_id=rid, rule_version=1, policy_set_version=1, status=status, severity=severity,
                      decision=decision, reason_code=code if decision else None, explanation="e",
                      evaluated_at=NOW)


def test_aggregate_precedence_and_retention():
    assert aggregate([])["overall"] == "pass"
    assert aggregate([_res("pass"), _res("not_applicable")])["overall"] == "pass"
    assert aggregate([_res("review_required", "review_required")])["overall"] == "review_required"
    clar = _res("review_required", "needs_clarification", rid="RULE-C", code="CLAR")
    block = _res("fail", "blocked", rid="RULE-B", code="BLOCK")
    rev = _res("review_required", "review_required", rid="RULE-R", code="REV")
    out = aggregate([clar, rev])
    assert out["overall"] == "needs_clarification"
    out = aggregate([clar, block, rev])
    assert out["overall"] == "blocked"                       # clarification never hides a block
    assert out["blocking_rule_refs"] == ["RULE-B@1"]
    assert out["clarification_rule_refs"] == ["RULE-C@1"]
    assert out["review_rule_refs"] == ["RULE-R@1"]
    assert out["reason_codes"] == ["BLOCK", "CLAR", "REV"]
    assert aggregate([block, clar, rev]) == aggregate([rev, clar, block])   # order independent


def test_aggregate_evaluation_error_never_pass():
    err = _res("evaluation_error", "review_required", rid="RULE-E", code="RULE_EVALUATION_ERROR")
    out = aggregate([_res("pass"), err])
    assert out["overall"] == "review_required" and "RULE-E@1" in out["review_rule_refs"]
    err2 = RuleResult(rule_id="RULE-E2", rule_version=1, policy_set_version=1, status="evaluation_error",
                      severity="info", explanation="e", evaluated_at=NOW)
    out = aggregate([err2])
    assert out["overall"] == "review_required" and "RULE_EVALUATION_ERROR" in out["reason_codes"]


def test_aggregate_coverage_gaps():
    cov = [CoverageItem(area="safety", status="covered"),
           CoverageItem(area="privacy", status="missing_coverage")]
    out = aggregate([_res("pass")], cov)
    assert out["overall"] == "review_required" and out["coverage_gaps"] == ["privacy"]
    out = aggregate([], [CoverageItem(area="price", status="evaluation_error", rule_refs=["RULE-P@2"])])
    assert out["overall"] == "review_required" and out["review_rule_refs"] == ["RULE-P@2"]
    out = aggregate([_res("fail", "blocked")], cov)
    assert out["overall"] == "blocked"


# ------------------------------------------------------------------------------------------- seed bundle
def _seed():
    version, rules = load_seed_bundle()
    return version, {r.rule_id: r for r in rules}


def test_seed_bundle_validates_cleanly():
    raw = json.loads(open("data/policy_rules_seed.json", encoding="utf-8").read())
    assert raw["bundle_version"] == "legacy-config-v1" and "legacy_config" in raw["description"]
    version, rules = _seed()
    assert set(rules) == {"RULE-SAFETY-REVIEW", "RULE-VENDOR-VERIFICATION", "RULE-PRIVACY-REVIEW",
                          "RULE-RATE-CARD-MISMATCH", "RULE-RATE-CARD-UNVERIFIED", "RULE-RETRIEVAL-DEGRADED"}
    for r in rules.values():
        assert validate_rule(r) == [], r.rule_id
        assert r.status == "candidate" and r.source == "legacy_config" and r.created_by == "system:bootstrap"
    assert rules["RULE-RATE-CARD-MISMATCH"].mandatory is True


def test_seed_threshold_matches_config():
    cfg = json.loads(open("data/policy_rag_config.json", encoding="utf-8").read())
    _, rules = _seed()
    cond = rules["RULE-SAFETY-REVIEW"].condition
    assert cond.op == "gte" and cond.value == cfg["review_topics"]["safety_review"]["min_attendees"]


_UNSET = object()


def _facts(attendees=100, notes="Vegetarian food", po=False, rates=_UNSET, mode="hybrid", title="Team lunch"):
    rates = [] if rates is _UNSET else rates
    ticket = {"ticket_id": "T", "submission": {"title": title, "notes": notes, "attendees": attendees,
                                               "event_date": "2026-11-02", "budget_inr": 50000}}
    wr = {"request": {"attendees": attendees, "date": "2026-11-02", "currency": "INR"}, "final_status": "pass"}
    if po:
        wr["procurement"] = {"po": {"vendor_id": "VEN-002", "total": 100.0, "subtotal": 95.0, "tax": 5.0,
                                    "line_items": [{}]}}
    return build_facts(ticket, wr, rates, {"retrieval_mode": mode})


def _triggered(facts):
    _, rules = _seed()
    res = [evaluate_rule(r, facts, 1, NOW) for r in rules.values()]
    assert not [r for r in res if r.status == "evaluation_error"]
    return {r.reason_code for r in res if r.status == "review_required"}


def test_seed_reproduces_phase2_triggers():
    assert _triggered(_facts(100)) == set()
    assert _triggered(_facts(149)) == set()
    assert _triggered(_facts(150)) == {"SAFETY_REVIEW_REQUIRED"}
    assert _triggered(_facts(100, po=True, rates=[])) == {"VENDOR_VERIFICATION_REQUIRED"}
    assert _triggered(_facts(100, notes="We will take photos")) == {"PRIVACY_REVIEW_REQUIRED"}
    assert _triggered(_facts(100, notes="Collect personal data at the door")) == {"PRIVACY_REVIEW_REQUIRED"}
    assert _triggered(_facts(100, notes="share the guest list")) == {"PRIVACY_REVIEW_REQUIRED"}
    assert _triggered(_facts(100, notes=None, title="Recorded town hall")) == {"PRIVACY_REVIEW_REQUIRED"}
    assert _triggered(_facts(100, notes="personal preference")) == set()
    assert _triggered(_facts(100, mode="keyword_only_degraded")) == {"POLICY_RETRIEVAL_DEGRADED"}
    assert _triggered(_facts(100, mode="hybrid_local_hash")) == {"POLICY_RETRIEVAL_DEGRADED"}


def test_seed_rate_rules():
    fail = _triggered(_facts(po=True, rates=[{"validation_status": "fail"}]))
    assert fail == {"VENDOR_VERIFICATION_REQUIRED", "RATE_CARD_MISMATCH"}
    unv = _triggered(_facts(po=True, rates=[{"validation_status": "needs_review"}]))
    assert unv == {"VENDOR_VERIFICATION_REQUIRED", "RATE_CARD_UNVERIFIED"}
    assert _triggered(_facts(po=True, rates=[{"validation_status": "pass"}])) == {"VENDOR_VERIFICATION_REQUIRED"}
    # catering vendors: no applicable rate card -> not_applicable, not a review
    assert _triggered(_facts(po=True, rates=[{"validation_status": "not_applicable"}])) == \
        {"VENDOR_VERIFICATION_REQUIRED"}
    # rate validation not computed -> mandatory mismatch rule must not silently pass
    _, rules = _seed()
    r = evaluate_rule(rules["RULE-RATE-CARD-MISMATCH"], _facts(rates=None), 1, NOW)
    assert r.status == "review_required" and "rate.applicable" in r.missing_fields
