"""Task-specific, versioned AI contracts (plan A1).

Every LLM call names a registered task. Unknown tasks raise ``UnknownTaskError`` - there is no generic,
unrestricted prompt to fall through to. Each contract declares its prompt version, output schema, budgets,
whether it may see sensitive content and its safe fallback. Output models forbid extra fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UnknownTaskError(ValueError):
    pass


# ------------------------------------------------------------------------------- output schemas
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TextOutput(_Strict):
    text: str = Field(min_length=1, max_length=2000)


class IntakeFieldsOutput(BaseModel):
    """Extracted request fields. Extra keys are ignored (never trusted); numeric fields are re-checked by the
    deterministic parser, which wins on conflicts."""
    model_config = ConfigDict(extra="ignore")
    service: Optional[str] = Field(default=None, max_length=40)
    date: Optional[str] = Field(default=None, max_length=40)
    attendees: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    budget_limit: Optional[float] = Field(default=None, ge=0, le=1e12)
    currency: Optional[str] = Field(default=None, max_length=8)
    location: Optional[str] = Field(default=None, max_length=200)
    urgency: Optional[Literal["normal", "high"]] = None
    requirements: Optional[list[str]] = Field(default=None, max_length=20)

    @field_validator("urgency", mode="before")
    @classmethod
    def _urgency(cls, v: Any) -> Optional[str]:
        """Unknown urgency values are dropped (the deterministic parser decides), not a whole-output failure."""
        v = v.strip().lower() if isinstance(v, str) else v
        return v if v in ("normal", "high") else None

    @field_validator("requirements", mode="before")
    @classmethod
    def _requirements(cls, v: Any) -> Optional[list]:
        return [str(r)[:100] for r in v][:20] if isinstance(v, list) else None


class RuleExtractionOutput(BaseModel):
    """Envelope only: each rule item is validated individually by the extractor (typed rule language +
    evidence checks, first MAX_CANDIDATES items), so one malformed item does not discard valid siblings."""
    model_config = ConfigDict(extra="ignore")
    rules: list[Any] = Field(default_factory=list)


# ------------------------------------------------------------------------------------- contracts
@dataclass(frozen=True)
class TaskContract:
    name: str
    version: str
    prompt_version: str
    mode: Literal["json", "text"]
    output_model: Type[BaseModel]
    max_output_tokens: int
    max_input_tokens: int
    sensitive_content_allowed: bool
    fallback: str                         # documented safe fallback behaviour
    prohibited_claims: tuple[str, ...] = field(default_factory=tuple)
    enabled: bool = True


_ADVISORY_CLAIMS = ("approved", "compliant", "authorized to pay", "payment executed", "rule activated")

TASKS: dict[str, TaskContract] = {c.name: c for c in (
    TaskContract("request_field_extraction", "1", "intake-v1", "json", IntakeFieldsOutput,
                 max_output_tokens=400, max_input_tokens=4000, sensitive_content_allowed=True,
                 fallback="deterministic regex parser (always runs and wins on numeric fields)"),
    TaskContract("policy_rule_extraction", "1", "rule-extract-v1", "json", RuleExtractionOutput,
                 max_output_tokens=4000, max_input_tokens=24000, sensitive_content_allowed=False,
                 fallback="deterministic pattern extractor (pattern-v1); candidates always need human review"),
    TaskContract("policy_result_summary", "1", "policy-summary-v1", "text", TextOutput,
                 max_output_tokens=220, max_input_tokens=6000, sensitive_content_allowed=False,
                 fallback="no advisory summary; deterministic findings are shown", prohibited_claims=_ADVISORY_CLAIMS),
    TaskContract("policy_check_explanation", "1", "policy-explain-v1", "text", TextOutput,
                 max_output_tokens=300, max_input_tokens=3000, sensitive_content_allowed=False,
                 fallback="deterministic templated summary"),
    TaskContract("critic_review_summary", "1", "critic-summary-v1", "text", TextOutput,
                 max_output_tokens=300, max_input_tokens=4000, sensitive_content_allowed=False,
                 fallback="deterministic templated summary"),
    TaskContract("retrieval_query_rewrite", "0", "query-rewrite-v0", "text", TextOutput,
                 max_output_tokens=80, max_input_tokens=500, sensitive_content_allowed=False,
                 fallback="original query", enabled=False),
)}


def get_contract(name: str) -> TaskContract:
    try:
        return TASKS[name]
    except KeyError:
        raise UnknownTaskError(f"Unknown AI task '{name}'.") from None
