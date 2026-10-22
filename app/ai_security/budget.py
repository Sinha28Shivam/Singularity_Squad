"""Full-request context budgeting (plan B2).

The budget covers the whole request - system instructions, user content, message framing - plus a reserved
output budget and a safety margin below the model's context window. Callers that carry evidence supply a
deterministic sequence of progressively smaller user payloads; the first that fits is used. If none fits the
call is refused with ``context_budget_exceeded`` (never silently truncated).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Union

from app.ai_security.telemetry import load_guardrails_config
from app.policy_rag.tokenization import count_chat_tokens, get_token_counter

UserPayload = Union[str, Callable[[int], Optional[str]]]
MAX_REDUCTION_LEVELS = 16


class BudgetConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BudgetLimits:
    model_context_tokens: int
    safety_margin_tokens: int
    max_input_tokens: int
    max_output_tokens: int
    max_evidence_items: int
    max_evidence_excerpt_chars: int


@dataclass
class BudgetDecision:
    fits: bool
    user: Optional[str]
    estimated_input_tokens: int
    input_limit: int
    output_tokens: int
    reduction_level: int
    tokenizer_id: str
    exact: bool

    @property
    def evidence_reduced(self) -> bool:
        return self.reduction_level > 0


def load_limits() -> BudgetLimits:
    cfg = load_guardrails_config()
    tb = cfg.get("token_budget") or {}
    try:
        limits = BudgetLimits(
            model_context_tokens=int(cfg["model_context_tokens"]),
            safety_margin_tokens=int(tb["safety_margin_tokens"]),
            max_input_tokens=int(tb["max_input_tokens"]),
            max_output_tokens=int(tb["max_output_tokens"]),
            max_evidence_items=int(tb["max_evidence_items"]),
            max_evidence_excerpt_chars=int(tb["max_evidence_excerpt_chars"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise BudgetConfigError("ai_guardrails_config.json token_budget is incomplete or invalid.") from exc
    if min(limits.model_context_tokens, limits.max_input_tokens, limits.max_output_tokens,
           limits.max_evidence_items, limits.max_evidence_excerpt_chars) <= 0 or limits.safety_margin_tokens < 0:
        raise BudgetConfigError("ai_guardrails_config.json token_budget values must be positive.")
    if limits.max_output_tokens + limits.safety_margin_tokens >= limits.model_context_tokens:
        raise BudgetConfigError("Output reserve plus safety margin must be below the model context window.")
    return limits


def input_limit(task_max_input: int, output_tokens: int, limits: Optional[BudgetLimits] = None) -> int:
    """Input tokens allowed: the task limit (a task may exceed the global default, e.g. bulk extraction),
    capped by the context window minus the output reserve and safety margin."""
    limits = limits or load_limits()
    window = limits.model_context_tokens - limits.safety_margin_tokens - output_tokens
    task_limit = task_max_input if task_max_input > 0 else limits.max_input_tokens
    return max(0, min(window, task_limit))


def plan_request(system: str, user: UserPayload, *, task_max_input: int, task_max_output: int,
                 model: Optional[str] = None) -> BudgetDecision:
    limits = load_limits()
    # A task's declared output reserve overrides the global default (like input); the window still caps both.
    output_tokens = task_max_output if task_max_output > 0 else limits.max_output_tokens
    output_tokens = min(output_tokens, limits.model_context_tokens - limits.safety_margin_tokens - 1)
    limit = input_limit(task_max_input, output_tokens, limits)
    counter = get_token_counter(model)
    builder = user if callable(user) else (lambda level, _u=user: _u if level == 0 else None)
    estimated = 0
    for level in range(MAX_REDUCTION_LEVELS):
        payload = builder(level)
        if payload is None:
            break
        estimated = count_chat_tokens([{"role": "system", "content": system}, {"role": "user", "content": payload}],
                                      model=model)
        if estimated <= limit:
            return BudgetDecision(True, payload, estimated, limit, output_tokens, level, counter.tokenizer_id,
                                  counter.exact)
    return BudgetDecision(False, None, estimated, limit, output_tokens, -1, counter.tokenizer_id, counter.exact)
