"""The only entry point for LLM calls (plan A1/A4/B2/B3).

``run_task(task_name, system, user)``:
1. resolves the registered contract (unknown task -> ``UnknownTaskError``; disabled task -> fallback);
2. budgets the full request (system + user + framing, output reserve, safety margin); an over-budget request
   is refused with ``context_budget_exceeded`` instead of being sent;
3. calls the model through ``app.llm`` with bounded output tokens;
4. parses into the task's Pydantic schema (unknown fields forbidden for strict schemas), bounds size and, for
   advisory text, rejects prohibited claims ("approved", "compliant", ...);
5. records safe telemetry (no prompts/content).

The returned ``TaskResult`` never mutates state. ``ok=False`` means: use the task's documented fallback.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from app import llm
from app.ai_security import telemetry
from app.ai_security.budget import UserPayload, plan_request
from app.ai_security.contracts import TaskContract, get_contract

logger = logging.getLogger(__name__)

MAX_RESPONSE_CHARS = 100_000
MAX_TEXT_CHARS = 2000  # TextOutput.text max_length


@dataclass
class TaskResult:
    task_name: str
    prompt_version: str
    ok: bool
    output: Optional[BaseModel] = None
    safe_error_code: Optional[str] = None
    model_id: Optional[str] = None
    tokenizer_id: Optional[str] = None
    token_count_exact: bool = False
    estimated_input_tokens: int = 0
    evidence_reduced: bool = False
    reduction_level: int = 0
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    validation_errors: list[str] = field(default_factory=list)

    def metadata(self) -> dict[str, Any]:
        """Safe, client-displayable metadata (no prompts, no raw provider errors)."""
        return {"task_name": self.task_name, "prompt_version": self.prompt_version, "model_id": self.model_id,
                "tokenizer_id": self.tokenizer_id, "token_count_estimated": self.estimated_input_tokens,
                "token_count_exact": self.token_count_exact, "evidence_reduced": self.evidence_reduced,
                "safe_error_code": self.safe_error_code, "correlation_id": self.correlation_id}


# A claim counts as negated only when a whole negation word precedes it, optionally with one word between
# ("not approved", "not yet approved", "cannot be approved"). Prefix matches such as "now"/"notably" do not.
_NEGATED_PREFIX = re.compile(r"\b(?:not|never|no|isn't|aren't|wasn't|cannot|can't|without)\b(?:\s+\w+)?\s+$")


def _prohibited(contract: TaskContract, text: str) -> Optional[str]:
    low = text.lower()
    for claim in contract.prohibited_claims:
        for m in re.finditer(r"\b" + re.escape(claim) + r"\b", low):
            if not _NEGATED_PREFIX.search(low[max(0, m.start() - 40):m.start()]):
                return claim
    return None


def _validate(contract: TaskContract, raw: Any) -> tuple[Optional[BaseModel], Optional[str], list[str]]:
    if raw is None:
        return None, "provider_unavailable", []
    if contract.mode == "text":
        if not isinstance(raw, str) or not raw.strip():
            return None, "invalid_output", ["empty text"]
        if len(raw) > MAX_RESPONSE_CHARS:
            return None, "output_too_large", []
        text = raw.strip()[:MAX_TEXT_CHARS]
        claim = _prohibited(contract, text)
        if claim:
            return None, "prohibited_claim", [f"advisory text asserted '{claim}'"]
        try:
            return contract.output_model.model_validate({"text": text}), None, []
        except ValidationError as exc:
            return None, "schema_validation_failed", [e["msg"] for e in exc.errors()][:5]
    if not isinstance(raw, dict):
        return None, "schema_validation_failed", ["output is not a JSON object"]
    if len(str(raw)) > MAX_RESPONSE_CHARS:
        return None, "output_too_large", []
    try:
        return contract.output_model.model_validate(raw), None, []
    except ValidationError as exc:
        return None, "schema_validation_failed", [f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                                                  for e in exc.errors()][:5]


def run_task(task_name: str, system: str, user: UserPayload, *, max_output_tokens: Optional[int] = None) -> TaskResult:
    contract = get_contract(task_name)  # raises UnknownTaskError: no generic fall-through
    result = TaskResult(task_name=contract.name, prompt_version=contract.prompt_version, ok=False)
    if not contract.enabled:
        result.safe_error_code = "task_disabled"
        return result
    if not llm.llm_enabled():
        result.safe_error_code = "llm_disabled"
        return result

    model = llm.model_name()
    out_tokens = min(max_output_tokens or contract.max_output_tokens, contract.max_output_tokens)
    plan = plan_request(system, user, task_max_input=contract.max_input_tokens, task_max_output=out_tokens,
                        model=model)
    result.model_id, result.tokenizer_id, result.token_count_exact = model, plan.tokenizer_id, plan.exact
    result.estimated_input_tokens, result.reduction_level = plan.estimated_input_tokens, max(plan.reduction_level, 0)
    result.evidence_reduced = plan.evidence_reduced
    event: dict[str, Any] = {"kind": "llm_call", "task_name": contract.name, "prompt_version": contract.prompt_version,
                             "model_id": model, "tokenizer_id": plan.tokenizer_id, "token_count_exact": plan.exact,
                             "estimated_input_tokens": plan.estimated_input_tokens, "input_limit": plan.input_limit,
                             "max_output_tokens": plan.output_tokens, "evidence_reduced": plan.evidence_reduced,
                             "correlation_id": result.correlation_id}
    if not plan.fits:
        result.safe_error_code = "context_budget_exceeded"
        telemetry.record({**event, "outcome": "rejected", "error_category": "context_budget_exceeded"})
        return result

    llm.pop_last_call()
    started = time.perf_counter()
    if contract.mode == "json":
        raw = llm.chat_json(system, plan.user, max_tokens=plan.output_tokens)
    else:
        raw = llm.chat_text(system, plan.user, max_tokens=plan.output_tokens)
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    call = llm.pop_last_call() or {}
    usage = call.get("usage") or {}

    output, code, errors = _validate(contract, raw)
    if code == "provider_unavailable" and call.get("error_category"):
        code = call["error_category"]
    result.output, result.ok = output, output is not None
    result.safe_error_code, result.validation_errors = code, errors
    telemetry.record({**event, "outcome": "ok" if result.ok else "fallback", "error_category": code,
                      "latency_ms": latency_ms, "actual_input_tokens": usage.get("input_tokens"),
                      "actual_output_tokens": usage.get("output_tokens"),
                      "schema_valid": result.ok, "cost_estimate": telemetry.estimate_cost(
                          model, usage.get("input_tokens"), usage.get("output_tokens"))})
    if not result.ok:
        logger.info("AI task %s fell back (%s).", contract.name, code)
    return result
