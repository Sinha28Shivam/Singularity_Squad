"""Phase 3 dynamic policy engine contracts.

Business rules are DATA: typed, versioned, reviewed rule documents evaluated by a small deterministic
interpreter. Rules can never execute code, read the environment or call external systems. Only rules
approved by an authorized reviewer enter the active policy set. Nothing here places orders or payments.
"""
from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

RULE_SCHEMA_VERSION = "1"
EVALUATOR_VERSION = "rule-evaluator-v1"
MAX_EXPRESSION_DEPTH = 6
MAX_EXPRESSION_NODES = 64
MAX_IN_LIST = 100
MAX_STRING_VALUE = 200

Operator = Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "exists", "is_missing",
                   "contains_any", "between", "date_before", "date_after", "date_between"]
ValueType = Literal["string", "integer", "decimal", "boolean", "date", "money", "string_list"]
RuleStatus = Literal["candidate", "needs_review", "validated", "approved", "rejected", "superseded", "expired"]
ACTIVE_RULE_STATUSES = {"approved"}
RuleSeverity = Literal["info", "review", "block"]
RuleDecision = Literal["pass", "review_required", "blocked", "needs_clarification"]
RuleResultStatus = Literal["pass", "fail", "review_required", "not_applicable", "evaluation_error"]
CoverageStatus = Literal["covered", "not_applicable", "missing_coverage", "evaluation_error"]
OverallStatus = Literal["pass", "review_required", "blocked", "needs_clarification"]


class Condition(BaseModel):
    """Leaf comparison: ``fact(field) <op> value``. Field paths are resolved only against the fact registry."""
    model_config = {"extra": "forbid"}
    field: str = Field(min_length=3, max_length=80, pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$")
    op: Operator
    value: Optional[Union[str, int, bool, list[str], list[int]]] = None
    value_type: ValueType
    currency: Optional[str] = Field(default=None, pattern=r"^[A-Z]{3}$")   # required for money
    unit: Optional[str] = Field(default=None, max_length=24)

    @field_validator("value")
    @classmethod
    def _bounded(cls, v):
        if isinstance(v, str) and len(v) > MAX_STRING_VALUE:
            raise ValueError("string value too long")
        if isinstance(v, list) and len(v) > MAX_IN_LIST:
            raise ValueError("list value too long")
        return v


class AllOf(BaseModel):
    model_config = {"extra": "forbid"}
    all: list["Expression"] = Field(min_length=1, max_length=16)


class AnyOf(BaseModel):
    model_config = {"extra": "forbid"}
    any: list["Expression"] = Field(min_length=1, max_length=16)


class NotOf(BaseModel):
    model_config = {"extra": "forbid", "populate_by_name": True}
    not_: "Expression" = Field(alias="not")


Expression = Annotated[Union[Condition, AllOf, AnyOf, NotOf], Field(union_mode="left_to_right")]
AllOf.model_rebuild()
AnyOf.model_rebuild()
NotOf.model_rebuild()


class RuleOutcome(BaseModel):
    """What happens when the condition MATCHES. Typed; never free-form decisions."""
    model_config = {"extra": "forbid"}
    decision: RuleDecision
    reason_code: str = Field(min_length=3, max_length=64, pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str = Field(min_length=3, max_length=400)


class EvidenceRef(BaseModel):
    model_config = {"extra": "forbid"}
    document_id: str
    document_version: int = Field(ge=1)
    content_hash: str
    chunk_id: Optional[str] = None
    page_number: Optional[int] = Field(default=None, ge=1)    # None = unavailable (never invented)
    section: Optional[str] = None
    excerpt: Optional[str] = Field(default=None, max_length=600)


class PolicyRule(BaseModel):
    """One immutable rule version (rule_id + rule_version)."""
    rule_id: str = Field(pattern=r"^RULE-[A-Z0-9-]{3,48}$")
    rule_version: int = Field(ge=1)
    name: str = Field(min_length=3, max_length=120)
    description: Optional[str] = Field(default=None, max_length=1000)
    policy_domain: Optional[str] = Field(default=None, max_length=64)    # document-derived metadata only
    status: RuleStatus = "candidate"
    severity: RuleSeverity
    applies_when: Optional[Expression] = None    # scope; if false -> not_applicable
    condition: Expression                        # violation/trigger condition
    outcome: RuleOutcome
    mandatory: bool = False                      # missing facts on a mandatory rule -> review_required
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    extraction_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    extraction_model: Optional[str] = None
    extraction_prompt_version: Optional[str] = None
    source: Literal["extracted", "manual", "legacy_config"] = "extracted"
    validation_errors: list[str] = Field(default_factory=list)
    conflict_refs: list[str] = Field(default_factory=list)
    supersedes: Optional[str] = None             # "RULE-X@3"
    effective_from: Optional[dt.date] = None
    effective_until: Optional[dt.date] = None
    created_by: str
    created_at: dt.datetime
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[dt.datetime] = None
    review_comment: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[dt.datetime] = None
    schema_version: str = RULE_SCHEMA_VERSION
    evaluator_compatibility_version: str = EVALUATOR_VERSION

    @model_validator(mode="after")
    def _dates(self):
        if self.effective_from and self.effective_until and self.effective_until < self.effective_from:
            raise ValueError("effective_until must not be before effective_from")
        return self


class PolicyRuleIn(BaseModel):
    """Manual candidate creation by a policy reviewer (still requires review/approval)."""
    model_config = {"extra": "forbid"}
    rule_id: Optional[str] = Field(default=None, pattern=r"^RULE-[A-Z0-9-]{3,48}$")
    name: str = Field(min_length=3, max_length=120)
    description: Optional[str] = Field(default=None, max_length=1000)
    policy_domain: Optional[str] = Field(default=None, max_length=64)
    severity: RuleSeverity
    applies_when: Optional[Expression] = None
    condition: Expression
    outcome: RuleOutcome
    mandatory: bool = False
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    effective_from: Optional[dt.date] = None
    effective_until: Optional[dt.date] = None
    supersedes: Optional[str] = None


class RuleReviewIn(BaseModel):
    model_config = {"extra": "forbid"}
    rule_version: int = Field(ge=1)
    comment: Optional[str] = Field(default=None, max_length=1000)
    expected_policy_set_version: Optional[int] = Field(default=None, ge=0)   # compare-and-set guard


class RuleResult(BaseModel):
    rule_id: str
    rule_version: int
    policy_set_version: int
    status: RuleResultStatus
    severity: RuleSeverity
    decision: Optional[RuleDecision] = None
    reason_code: Optional[str] = None
    explanation: str
    fact_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    evaluated_at: dt.datetime
    evaluator_version: str = EVALUATOR_VERSION


class CoverageItem(BaseModel):
    area: str
    status: CoverageStatus
    rule_refs: list[str] = Field(default_factory=list)
    detail: Optional[str] = None


class PolicySetVersion(BaseModel):
    """Immutable active policy-set version; the registry pointer moves by compare-and-set."""
    policy_set_version: int = Field(ge=0)
    rule_refs: list[str]                      # ["RULE-X@2", ...]
    rule_hashes: dict[str, str]               # rule ref -> canonical sha256
    created_at: dt.datetime
    created_by: str
    reason: str
    previous_version: Optional[int] = None


class RuleEngineEvaluation(BaseModel):
    """Deterministic engine output for one ticket run; embedded in the evidence snapshot."""
    evaluation_id: str
    ticket_id: str
    run_count: int
    policy_set_version: int
    evaluator_version: str = EVALUATOR_VERSION
    evaluated_at: dt.datetime
    fact_snapshot_hash: str
    facts_used: dict[str, Any] = Field(default_factory=dict)   # only registry facts referenced by rules
    results: list[RuleResult] = Field(default_factory=list)
    coverage: list[CoverageItem] = Field(default_factory=list)
    overall: OverallStatus
    reason_codes: list[str] = Field(default_factory=list)
    blocking_rule_refs: list[str] = Field(default_factory=list)
    review_rule_refs: list[str] = Field(default_factory=list)
    clarification_rule_refs: list[str] = Field(default_factory=list)
    rate_validations: list[dict] = Field(default_factory=list)   # RateValidationResult JSON per line
    engine_hash: str = ""
