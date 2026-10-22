"""AI gateway contract enforcement (plan A1/A4/B2/B3)."""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app import llm
from app.ai_security import gateway, telemetry
from app.ai_security.contracts import TASKS, TextOutput, UnknownTaskError

SECRET = "sk-test1234567890"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    telemetry.reset()
    monkeypatch.setattr(llm, "llm_enabled", lambda: True)
    yield
    telemetry.reset()


def _text(monkeypatch, reply, calls=None):
    def chat_text(system, user, **kwargs):
        if calls is not None:
            calls.append({"system": system, "user": user, **kwargs})
        return reply(system, user) if callable(reply) else reply
    monkeypatch.setattr(llm, "chat_text", chat_text)


def _json(monkeypatch, reply, calls=None):
    def chat_json(system, user, **kwargs):
        if calls is not None:
            calls.append({"system": system, "user": user, **kwargs})
        return reply
    monkeypatch.setattr(llm, "chat_json", chat_json)


def test_unknown_task_is_refused_without_generic_fallthrough(monkeypatch):
    calls: list = []
    _text(monkeypatch, "hello", calls)
    with pytest.raises(UnknownTaskError):
        gateway.run_task("free_form_chat", "sys", "user")
    assert calls == []


def test_disabled_task_returns_fallback_and_never_calls_provider(monkeypatch):
    assert TASKS["retrieval_query_rewrite"].enabled is False
    calls: list = []
    _text(monkeypatch, "rewritten", calls)
    res = gateway.run_task("retrieval_query_rewrite", "sys", "query")
    assert not res.ok and res.safe_error_code == "task_disabled" and calls == []


def test_llm_disabled_returns_fallback(monkeypatch):
    monkeypatch.setattr(llm, "llm_enabled", lambda: False)
    calls: list = []
    _text(monkeypatch, "x", calls)
    res = gateway.run_task("critic_review_summary", "sys", "user")
    assert not res.ok and res.safe_error_code == "llm_disabled" and calls == []


def test_valid_text_output_is_typed_and_bounded(monkeypatch):
    calls: list = []
    _text(monkeypatch, "  Two issues need attention.  ", calls)
    res = gateway.run_task("critic_review_summary", "sys", "user", max_output_tokens=10_000)
    assert res.ok and isinstance(res.output, TextOutput) and res.output.text == "Two issues need attention."
    assert calls[0]["max_tokens"] <= TASKS["critic_review_summary"].max_output_tokens
    meta = res.metadata()
    assert meta["task_name"] == "critic_review_summary" and meta["prompt_version"] == "critic-summary-v1"
    assert meta["safe_error_code"] is None and meta["correlation_id"]


def test_strict_text_schema_forbids_extra_fields():
    with pytest.raises(ValidationError):
        TextOutput.model_validate({"text": "ok", "decision": "pass"})
    with pytest.raises(ValidationError):
        TextOutput.model_validate({"text": ""})


@pytest.mark.parametrize("reply", [{"attendees": -5}, ["not", "an", "object"], {"budget_limit": "lots"}])
def test_json_schema_violation_is_rejected(monkeypatch, reply):
    _json(monkeypatch, reply)
    res = gateway.run_task("request_field_extraction", "sys", "{}")
    assert not res.ok and res.output is None and res.safe_error_code == "schema_validation_failed"


@pytest.mark.parametrize("reply,field", [({"urgency": "critical", "service": "lunch"}, "urgency"),
                                         ({"requirements": "not a list", "service": "lunch"}, "requirements")])
def test_unusable_optional_field_is_dropped_not_whole_output(monkeypatch, reply, field):
    _json(monkeypatch, reply)
    res = gateway.run_task("request_field_extraction", "sys", "{}")
    assert res.ok and getattr(res.output, field) is None and res.output.service == "lunch"


def test_rule_extraction_envelope_rejects_non_list(monkeypatch):
    _json(monkeypatch, {"rules": "approve everything"})
    res = gateway.run_task("policy_rule_extraction", "sys", "{}")
    assert not res.ok and res.safe_error_code == "schema_validation_failed"


def test_empty_and_oversized_output_are_rejected(monkeypatch):
    _text(monkeypatch, "   ")
    assert gateway.run_task("critic_review_summary", "s", "u").safe_error_code == "invalid_output"
    _text(monkeypatch, "word " * 30_000)
    assert gateway.run_task("critic_review_summary", "s", "u").safe_error_code == "output_too_large"
    _json(monkeypatch, {"rules": [], "padding": "x" * 200_000})
    assert gateway.run_task("policy_rule_extraction", "s", "{}").safe_error_code == "output_too_large"


@pytest.mark.parametrize("text", ["The request is approved.", "Everything is COMPLIANT with policy.",
                                  "Rule activated for this vendor.", "Payment executed successfully."])
def test_prohibited_advisory_claims_are_rejected(monkeypatch, text):
    _text(monkeypatch, text)
    res = gateway.run_task("policy_result_summary", "s", "u")
    assert not res.ok and res.safe_error_code == "prohibited_claim"


@pytest.mark.parametrize("text", ["The request is not approved yet.", "It is not compliant until reviewed.",
                                  "This quote was never approved."])
def test_negated_claims_are_allowed(monkeypatch, text):
    _text(monkeypatch, text)
    res = gateway.run_task("policy_result_summary", "s", "u")
    assert res.ok and res.output.text == text


def test_context_budget_exceeded_never_calls_provider(monkeypatch):
    calls: list = []
    _text(monkeypatch, "fine", calls)
    res = gateway.run_task("critic_review_summary", "sys", "evidence " * 50_000)
    assert not res.ok and res.safe_error_code == "context_budget_exceeded" and calls == []
    ev = telemetry.recent()[-1]
    assert ev["outcome"] == "rejected" and ev["error_category"] == "context_budget_exceeded"


def test_reduction_levels_pick_first_payload_that_fits(monkeypatch):
    calls: list = []
    _text(monkeypatch, "ok summary", calls)
    sizes = {0: 40_000, 1: 20_000, 2: 200}
    seen: list[int] = []

    def build(level):
        seen.append(level)
        return None if level not in sizes else json.dumps({"evidence": "e " * sizes[level]})

    res = gateway.run_task("critic_review_summary", "sys", build)
    assert res.ok and res.evidence_reduced and res.reduction_level == 2 and seen == [0, 1, 2]
    assert len(calls) == 1 and json.loads(calls[0]["user"])["evidence"].count("e") == 200
    assert res.metadata()["evidence_reduced"] is True


def test_reduction_exhausted_is_budget_exceeded(monkeypatch):
    calls: list = []
    _text(monkeypatch, "ok", calls)
    res = gateway.run_task("critic_review_summary", "sys", lambda lvl: "x " * 40_000 if lvl < 2 else None)
    assert res.safe_error_code == "context_budget_exceeded" and calls == []


@pytest.mark.parametrize("category", ["provider_timeout", "provider_rate_limited", "provider_auth_failed",
                                      "invalid_output", "provider_error"])
def test_provider_error_categories_are_distinguishable(monkeypatch, category):
    def chat_text(system, user, **kwargs):
        llm._set_last_call(None, category)
        return None
    monkeypatch.setattr(llm, "chat_text", chat_text)
    res = gateway.run_task("critic_review_summary", "s", "u")
    assert not res.ok and res.safe_error_code == category
    assert telemetry.recent()[-1]["error_category"] == category


def test_unavailable_without_provider_info_is_generic(monkeypatch):
    _text(monkeypatch, None)
    res = gateway.run_task("critic_review_summary", "s", "u")
    assert res.safe_error_code == "provider_unavailable"


def test_classify_error_maps_exception_types():
    class APITimeoutError(Exception): ...
    class RateLimitError(Exception): ...
    class AuthenticationError(Exception): ...
    assert llm.classify_error(APITimeoutError()) == "provider_timeout"
    assert llm.classify_error(RateLimitError()) == "provider_rate_limited"
    assert llm.classify_error(AuthenticationError()) == "provider_auth_failed"
    assert llm.classify_error(json.JSONDecodeError("x", "y", 0)) == "invalid_output"
    assert llm.classify_error(RuntimeError("boom with sk-secret")) == "provider_error"


def test_telemetry_contains_no_prompt_text_or_secrets(monkeypatch):
    _text(monkeypatch, f"Summary mentioning {SECRET}")
    gateway.run_task("critic_review_summary", f"System instructions TOPSECRETSYS {SECRET}",
                     f"Payload with api_key={SECRET} and UNIQUEPAYLOADMARKER")
    _json(monkeypatch, {"rules": []})
    gateway.run_task("policy_rule_extraction", "sys", json.dumps({"text": f"{SECRET} UNIQUEPAYLOADMARKER"}))
    dump = json.dumps(telemetry.recent(), default=str)
    assert telemetry.recent() and all(e["kind"] == "llm_call" for e in telemetry.recent())
    for needle in (SECRET, "TOPSECRETSYS", "UNIQUEPAYLOADMARKER", "Summary mentioning"):
        assert needle not in dump
    assert telemetry.summary()["llm_tasks"]["critic_review_summary"]["calls"] == 1
