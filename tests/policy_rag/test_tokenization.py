"""Plan B1: model-aware token counting with an explicitly-labelled conservative fallback."""
from __future__ import annotations

import json

import pytest

from app.policy_rag import tokenization as tok

SAMPLES = {
    "punctuation": "Wait... what?! (Yes) -- no; maybe: 'quoted' \"double\" [x] {y} <z> #1 @2 $3 %4 ^5 &6 *7",
    "url": "https://example.com/policies/v2/procurement?section=4.1&lang=en#approval-thresholds-and-limits",
    "json": json.dumps({"vendor": "VEN-001", "rates": [{"item": "team_gathering", "inr": 18000}] * 5}),
    "table": "Package | Scope | Price\n---|---|---\nTeam Gathering | 6 hours | INR 18,000 / event\n"
             "Meeting Essentials | 4 hours | INR 1,45,000 / event",
    "unicode": "Caf\u00e9 \u2014 na\u00efve r\u00e9sum\u00e9 \u20b918,000 \u2603 \U0001F600 \u4e2d\u6587",
    "hindi_mixed": "Vendor \u0928\u0940\u0924\u093f \u0915\u0947 \u0905\u0928\u0941\u0938\u093e\u0930 approval "
                   "\u0906\u0935\u0936\u094d\u092f\u0915 \u0939\u0948 for events over 150 attendees.",
    "ocr": "S I N G U L A R I T Y  Ev3nt  P0licy || l1st 0f r@tes ~~ \ufffd\ufffd  r n m",
    "long_string": "x" * 5000,
}


@pytest.fixture
def exact(monkeypatch):
    monkeypatch.setenv("WORKPLACEOPS_TOKENIZER", "tiktoken")
    c = tok.get_token_counter()
    if not c.exact:
        pytest.skip("tiktoken not available")
    return c


def test_exact_counter_is_labelled_exact(exact):
    assert exact.exact is True and exact.tokenizer_id.startswith("tiktoken:")


def test_estimate_counter_is_never_labelled_exact(monkeypatch):
    monkeypatch.setenv("WORKPLACEOPS_TOKENIZER", "estimate")
    c = tok.get_token_counter()
    assert c.exact is False and c.tokenizer_id == tok.ESTIMATE_TOKENIZER_ID
    assert "tiktoken" not in c.tokenizer_id


@pytest.mark.parametrize("value", ["bogus", "gpt2", "../../etc", "TIKTOKENX"])
def test_invalid_env_falls_back_to_estimate(monkeypatch, value):
    monkeypatch.setenv("WORKPLACEOPS_TOKENIZER", value)
    c = tok.get_token_counter()
    assert c.exact is False and c.tokenizer_id == tok.ESTIMATE_TOKENIZER_ID


def test_auto_mode_labels_consistently(monkeypatch):
    monkeypatch.setenv("WORKPLACEOPS_TOKENIZER", "auto")
    c = tok.get_token_counter()
    assert c.exact is (c.tokenizer_id != tok.ESTIMATE_TOKENIZER_ID)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_counts_positive_and_estimate_is_conservative(exact, name):
    text = SAMPLES[name]
    n_exact = exact.count(text)
    n_est = tok.EstimateCounter().count(text)
    assert n_exact > 0 and n_est > 0
    # The fallback errs on the safe side: never fewer tokens than the real tokenizer for these inputs.
    assert n_est >= n_exact, (name, n_est, n_exact)


def test_empty_text_counts_zero(exact):
    assert exact.count("") == 0 and tok.EstimateCounter().count("") == 0


@pytest.mark.parametrize("mode", ["tiktoken", "estimate"])
@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_truncate_to_tokens_fits(monkeypatch, mode, name):
    monkeypatch.setenv("WORKPLACEOPS_TOKENIZER", mode)
    out = tok.truncate_to_tokens(SAMPLES[name], 12)
    assert 0 < tok.count_tokens(out) <= 12
    assert SAMPLES[name].startswith(out)


def test_truncate_keeps_short_text_and_handles_zero():
    assert tok.truncate_to_tokens("short text", 100) == "short text"
    assert tok.truncate_to_tokens("short text", 0) == ""


@pytest.mark.parametrize("mode", ["tiktoken", "estimate"])
def test_chat_count_exceeds_sum_of_contents(monkeypatch, mode):
    monkeypatch.setenv("WORKPLACEOPS_TOKENIZER", mode)
    messages = [{"role": "system", "content": "You are a policy assistant."},
                {"role": "user", "content": SAMPLES["hindi_mixed"]},
                {"role": "assistant", "content": SAMPLES["json"]}]
    contents = sum(tok.count_tokens(m["content"]) for m in messages)
    assert tok.count_chat_tokens(messages) > contents
