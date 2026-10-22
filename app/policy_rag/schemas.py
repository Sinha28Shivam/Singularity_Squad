"""Phase 2 Policy RAG contracts: policy documents, chunks, evidence, findings, rate cards, evaluation.

RAG finds evidence. Structured extraction supplies candidate facts. Deterministic code validates amounts
and enforces rules. Humans approve. Nothing here executes an order or payment.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

DocumentType = Literal[
    "company_policy", "vendor_policy", "vendor_contract", "privacy_policy", "security_policy",
    "rate_card", "procurement_guideline", "other",
]
DocumentStatus = Literal[
    "uploaded", "validating", "extracting", "needs_ocr", "indexing", "ready", "needs_review",
    "failed", "superseded", "archived",
]
SEARCHABLE_STATUSES = {"ready"}  # needs_review docs are listed but never used as current evidence
FindingCategory = Literal[
    "price", "vendor_eligibility", "approval_threshold", "privacy", "security", "documentation",
    "contract_term", "other",
]
FindingSeverity = Literal["info", "warning", "review_required", "hard_block"]
FindingStatus = Literal["pass", "fail", "unknown", "conflict", "not_applicable"]
RateValidationStatus = Literal["pass", "fail", "unknown", "conflict", "needs_review"]
VerificationStatus = Literal["needs_review", "verified", "rejected"]
EvaluationOutcome = Literal["pass", "review_required", "hard_block", "unknown"]
# "hybrid" = keyword + real semantic embeddings; "hybrid_local_hash" = keyword + local hashed vectors (not
# semantic); "keyword_only_degraded" = no comparable vectors. Anything but "hybrid" is reported as degraded.
RetrievalMode = Literal["hybrid", "hybrid_local_hash", "keyword_only_degraded"]
EmbeddingProvider = Literal["openai", "local_hash", "none"]


class PolicyDocumentMetadataIn(BaseModel):
    """Uploader-supplied metadata (per file). Never contains uploaded_by/status/hash: those are server-set."""
    model_config = {"extra": "forbid"}

    document_type: DocumentType
    vendor_key: Optional[str] = Field(default=None, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    department: Optional[str] = Field(default=None, max_length=64)
    jurisdiction: Optional[str] = Field(default=None, max_length=16)
    effective_from: Optional[dt.date] = None
    effective_to: Optional[dt.date] = None
    version_label: Optional[str] = Field(default=None, max_length=64)
    logical_key: Optional[str] = Field(default=None, max_length=96, pattern=r"^[a-z0-9][a-z0-9_.-]*$")

    @model_validator(mode="after")
    def _dates(self):
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError("effective_to must not be before effective_from")
        return self


class PolicyDocument(BaseModel):
    document_id: str                     # server-generated "doc_<hex>"
    logical_key: str                     # stable key linking versions (default: slug of filename)
    filename: str                        # sanitized display name
    storage_name: str                    # server-side safe name; never exposed as a path
    content_hash: str                    # sha256 hex of the bytes
    document_type: DocumentType
    vendor_key: Optional[str] = None
    department: Optional[str] = None
    jurisdiction: Optional[str] = None
    effective_from: Optional[dt.date] = None
    effective_to: Optional[dt.date] = None
    version_label: Optional[str] = None
    version: int = 1                     # increments per logical_key
    status: DocumentStatus
    uploaded_by: str                     # from the authenticated session
    uploaded_at: dt.datetime
    extraction_status: Literal["pending", "complete", "partial", "failed"] = "pending"
    index_status: Literal["pending", "ready", "failed", "degraded"] = "pending"
    page_count: int = 0
    low_text_pages: list[int] = Field(default_factory=list)
    # Extraction tool/version + page-level quality (low_text_page_count/ratio, needs_review flag, warnings).
    extraction_quality: Optional[dict] = None
    # Index build metadata: embedding provider/model/dims, tokenizer_id, chunking_config_version, settings.
    index_metadata: Optional[dict] = None
    chunk_count: int = 0
    rate_entry_count: int = 0
    error_code: Optional[str] = None     # safe error category, never a stack trace
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    source_system: Optional[str] = None
    access_scope: Literal["internal"] = "internal"
    schema_version: int = 1


class DocumentUploadResult(BaseModel):
    filename: str
    ok: bool
    document_id: Optional[str] = None
    status: Optional[DocumentStatus] = None
    duplicate_of: Optional[str] = None
    error_code: Optional[str] = None     # e.g. not_pdf, too_large, too_many_pages, corrupt, empty_text
    message: Optional[str] = None


class PolicyChunk(BaseModel):
    chunk_id: str
    document_id: str
    content_hash: str
    page_number: int                     # 1-based
    section: Optional[str] = None
    chunk_index: int
    text: str
    token_estimate: int                  # = token_count (kept for backward compatibility)
    embedding_model: Optional[str] = None
    token_count: Optional[int] = None    # count under ``tokenizer_id``
    tokenizer_id: Optional[str] = None   # e.g. "tiktoken:o200k_base" or "estimate-v1"
    token_count_exact: Optional[bool] = None  # False for the conservative estimate; never labelled exact
    chunking_config_version: Optional[str] = None
    embedding_dims: Optional[int] = None
    embedding_provider: Optional[EmbeddingProvider] = None


class PolicyEvidence(BaseModel):
    evidence_id: str
    chunk_id: str
    document_id: str
    filename: str
    content_hash: str
    version_label: Optional[str] = None
    version: int
    page_number: int
    section: Optional[str] = None
    excerpt: str                         # compact, from the actual chunk text
    relevance_score: float               # real fused score
    document_type: DocumentType
    vendor_key: Optional[str] = None
    effective_from: Optional[dt.date] = None
    effective_to: Optional[dt.date] = None


class PolicySearchIn(BaseModel):
    model_config = {"extra": "forbid"}
    query: str = Field(min_length=2, max_length=500)
    vendor_key: Optional[str] = Field(default=None, max_length=64)
    document_types: Optional[list[DocumentType]] = None
    as_of: Optional[dt.date] = None
    top_k: int = Field(default=8, ge=1, le=20)
    include_historical: bool = False


class PolicySearchResult(BaseModel):
    query_id: str
    evidence: list[PolicyEvidence] = Field(default_factory=list)
    retrieval_mode: RetrievalMode
    insufficient_evidence: bool = False
    warnings: list[str] = Field(default_factory=list)
    embedding_provider: Optional[EmbeddingProvider] = None
    embedding_model: Optional[str] = None
    embedding_available: Optional[bool] = None       # True only when semantic embeddings were used
    degradation_reason: Optional[str] = None
    retrieval_config_version: Optional[str] = None
    tokenizer_id: Optional[str] = None
    chunking_config_version: Optional[str] = None
    mixed_index_warning: bool = False                # candidate chunks span several embedding models/dims


class RateCardEntry(BaseModel):
    rate_id: str
    document_id: str
    content_hash: str
    vendor_key: str                      # namespaced, e.g. "evt-ven-001"; never a fuzzy name
    vendor_name: Optional[str] = None
    item_or_service_code: str            # slug of package name, e.g. "team_gathering"
    description: str
    unit_rate: str                       # Decimal as string
    currency: str = "INR"
    unit: str                            # "event", "person", "hour", "item" ...
    minimum_quantity: Optional[int] = None
    maximum_quantity: Optional[int] = None
    tax_treatment: Literal["excluded", "included", "not_verified"] = "not_verified"
    effective_from: Optional[dt.date] = None
    effective_to: Optional[dt.date] = None
    source_page: int
    verification_status: VerificationStatus = "needs_review"
    verified_by: Optional[str] = None
    verified_at: Optional[dt.datetime] = None
    extraction_confidence: Literal["high", "low"] = "low"

    @field_validator("unit_rate")
    @classmethod
    def _decimal(cls, v: str) -> str:
        d = Decimal(v)
        if d < 0:
            raise ValueError("unit_rate must be non-negative")
        return format(d.quantize(Decimal("0.01")), "f")


class RateQuote(BaseModel):
    """A quoted line to validate. Amounts as Decimal strings."""
    model_config = {"extra": "forbid"}
    vendor_key: str
    item_or_service_code: str
    quoted_unit_rate: str
    quantity: int = Field(ge=1)
    currency: str = "INR"
    unit: Optional[str] = None
    as_of: dt.date


class RateValidationResult(BaseModel):
    quoted_unit_rate: str
    expected_unit_rate: Optional[str] = None
    currency: str
    quantity: int
    quoted_total: str
    expected_total: Optional[str] = None
    difference_amount: Optional[str] = None
    difference_percentage: Optional[str] = None
    rate_id: Optional[str] = None
    candidate_rate_ids: list[str] = Field(default_factory=list)
    source_document_id: Optional[str] = None
    source_page: Optional[int] = None
    rate_card_version: Optional[str] = None
    validation_status: RateValidationStatus
    reason: str
    # Set when the quote was built from a draft-PO line during a ticket run.
    line_index: Optional[int] = None
    line_description: Optional[str] = None
    vendor_key: Optional[str] = None
    item_or_service_code: Optional[str] = None
    unit: Optional[str] = None
    missing_fields: list[str] = Field(default_factory=list)   # required quote fields absent (never guessed)


class PolicyFinding(BaseModel):
    finding_id: str
    category: FindingCategory
    severity: FindingSeverity
    status: FindingStatus
    summary: str
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)   # must reference retrieved evidence only
    required_action: Optional[str] = None
    rule_id: Optional[str] = None                          # set for deterministic findings
    source: Literal["rule", "llm"] = "rule"                # llm findings are advisory, never decisive


class PolicyEvaluationResult(BaseModel):
    evaluation_id: str
    ticket_id: str
    run_count: int
    evaluated_at: dt.datetime
    rule_set_version: str
    retrieval_mode: RetrievalMode
    outcome: EvaluationOutcome
    findings: list[PolicyFinding] = Field(default_factory=list)
    evidence: list[PolicyEvidence] = Field(default_factory=list)
    rate_validation: Optional[RateValidationResult] = None
    missing_evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    approval_reason_codes: list[str] = Field(default_factory=list)  # added to the approval requirement
    blocking_findings: list[str] = Field(default_factory=list)      # finding_ids with severity hard_block
    documents_used: list[dict] = Field(default_factory=list)        # {document_id, content_hash, version, version_label}
    model_info: Optional[dict] = None
    summary: Optional[str] = None                                    # optional LLM explanation (advisory)
    evaluation_hash: str = ""                                        # sha256 of canonical evaluation fields
    schema_version: int = 1
    # Phase 3 additions (Optional; old snapshots stay valid).
    rate_validations: list[RateValidationResult] = Field(default_factory=list)  # one per validated PO line
    rate_applicable: Optional[bool] = None
    rate_not_applicable_reason: Optional[str] = None
    embedding_provider: Optional[EmbeddingProvider] = None
    embedding_model: Optional[str] = None
    embedding_available: Optional[bool] = None
    degradation_reason: Optional[str] = None
    retrieval_config_version: Optional[str] = None
    # RuleEngineEvaluation JSON (own engine_hash; excluded from evaluation_hash).
    rule_engine: Optional[dict] = None


class EvidenceSnapshot(BaseModel):
    """Append-only record stored per (ticket_id, run_count) evaluation."""
    snapshot_id: str
    ticket_id: str
    run_count: int
    workflow_snapshot_hash: Optional[str] = None
    evaluation: PolicyEvaluationResult
    created_at: dt.datetime
    schema_version: int = 1
