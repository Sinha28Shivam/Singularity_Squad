"""Regressions for issues found in code review of the AI guardrails work."""
import importlib.util
from pathlib import Path

import pytest

from app import llm
from app.ai_security import gateway
from app.ai_security.budget import plan_request
from app.ai_security.contracts import IntakeFieldsOutput, RuleExtractionOutput, get_contract

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("text", [
    "The request is now approved.",
    "The vendor is unconditionally approved.",
    "This is notably compliant.",
    "Normally approved requests go ahead.",
])
def test_prefix_words_do_not_count_as_negation(text):
    assert gateway._prohibited(get_contract("policy_result_summary"), text) is not None


@pytest.mark.parametrize("text", [
    "The request is not approved.",
    "It is not yet approved; a reviewer must decide.",
    "The quote cannot be approved without a verified rate card.",
    "The quote is never compliant until the rate card is verified.",
])
def test_whole_word_negation_is_allowed(text):
    assert gateway._prohibited(get_contract("policy_result_summary"), text) is None


def test_task_output_reserve_overrides_global_default():
    plan = plan_request("s", "u", task_max_input=24000, task_max_output=4000)
    assert plan.output_tokens == 4000 and plan.fits


def test_rule_envelope_keeps_more_than_fifty_and_non_object_items():
    out = RuleExtractionOutput.model_validate({"rules": [{"n": i} for i in range(60)] + ["junk"]})
    assert len(out.rules) == 61


def test_intake_schema_accepts_nulls_and_drops_unknown_urgency():
    out = IntakeFieldsOutput.model_validate({"service": "lunch", "currency": "INR", "requirements": None,
                                             "urgency": "Urgent"})
    assert out.requirements is None and out.urgency is None and out.service == "lunch"
    assert IntakeFieldsOutput.model_validate({"urgency": " High "}).urgency == "high"


def test_rule_extraction_reports_invalid_output_fallback(monkeypatch):
    from app.policy_rules import extraction
    monkeypatch.setattr(llm, "llm_enabled", lambda: True)
    monkeypatch.setattr(llm, "chat_json", lambda *a, **k: "not a dict")
    warnings: list[str] = []
    chunk = {"chunk_id": "chk_x", "text": "Events over 150 attendees require a safety review.", "page_number": 1}
    assert extraction.llm_extract({"document_id": "d", "filename": "f.pdf"}, [chunk], warnings) is None
    assert any("schema_validation_failed" in w for w in warnings)


def _gate():
    spec = importlib.util.spec_from_file_location("ai_release_gate", ROOT / "scripts" / "ai_release_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_secret_scan_is_not_hidden_by_hint_words_elsewhere_on_the_line():
    gate = _gate()
    key = "sk-" + "Ab3dE5gH7jK9mN1pQ3sT5vW7"
    lines = [f'<div data-k="{key}">', f'KEY = "{key}"  # see example.md', f'redact_text("{key}")']
    for line in lines:
        assert gate.scan_text(line), line
