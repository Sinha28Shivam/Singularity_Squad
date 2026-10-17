# WorkplaceOps AI — Phase 2 Implementation Specification
## Policy RAG, Multi-PDF Knowledge Base, Rate-Card Validation, and Approval Safety

**Project:** WorkplaceOps AI — Request-to-Payment Copilot  
**Phase:** Phase 2  
**Status:** Implementation specification  
**Primary stack:** Python, FastAPI, Pydantic, Streamlit, MongoDB, Azure AI Foundry  
**Document processing:** PyMuPDF (or pypdf), with OCR fallback for scanned PDFs  
**Retrieval:** Hybrid keyword + vector retrieval, with metadata filtering  
**Non-negotiable boundary:** This system prepares and reviews drafts. It must not place orders, send purchase orders to vendors, initiate payments, or perform other external purchasing actions.

---

## 1. Purpose

Extend the existing WorkplaceOps AI application so users can upload multiple company and vendor policy PDFs, retrieve relevant evidence for a purchase/request workflow, validate rate-card information, explain policy findings with citations, and route requests to authorized human approvers.

The system must combine:

1. **RAG for policy evidence:** retrieve relevant clauses and explain their relevance.
2. **Structured extraction for numeric facts:** extract and validate rate-card entries, currencies, dates, and thresholds.
3. **Deterministic policy enforcement:** apply explicit rules in Python rather than trusting an LLM to make final compliance decisions.
4. **Approval Safety:** persist approval requests, enforce trusted authorization and requester/approver separation, and audit decisions.
5. **Evidence traceability:** preserve source document identity, version, page, section, and relevant excerpt for important findings.

This phase extends the current FastAPI + Pydantic + Streamlit application and its existing workflow, policy retrieval, vendor matching, deterministic billing, Critic, orchestration, JSON fixtures, and MongoDB persistence work. Reuse existing components where practical instead of creating a parallel application.

## 2. Goals and success criteria

By the end of Phase 2, a user should be able to:

- Upload multiple PDF documents in one batch.
- Classify documents as company policy, vendor policy/contract, privacy/security policy, rate card, or other approved document type.
- See ingestion status and actionable errors for each file.
- Retrieve policy passages relevant to a submitted request, vendor, service, location, and request amount.
- See citations that identify the source file, version, page number, section when available, and supporting excerpt.
- Compare quoted prices against applicable, valid rate-card entries using deterministic calculations.
- Detect missing evidence, expired documents, conflicting clauses, ambiguous extracted values, and out-of-scope policies.
- Generate a reviewable request/PO draft and policy-evidence report.
- Create and persist an approval request when configured policy rules require human review.
- Enforce authorization on the backend, including preventing self-approval.
- Keep the decision, request version, policy evidence, and source document versions linked for audit.
- Verify that an approved request remains a draft and does not trigger any external purchase or payment action.

Acceptance requires automated tests for policy retrieval, numeric validation, access control, approval lifecycle, persistence, and failure cases. Do not claim tests pass unless they have actually been run.

## 3. Scope

### In scope

- Multiple PDF uploads and per-file ingestion status.
- File validation, text extraction, OCR fallback where needed, chunking, metadata, embeddings, and retrieval.
- Versioning and effective-date metadata.
- Vendor-aware and document-type-aware retrieval filters.
- Citation-bearing policy findings.
- Structured rate-card extraction and deterministic comparison.
- Policy conflict and missing-evidence handling.
- Approval request lifecycle and role enforcement from the Phase 2 Approval Safety specification.
- Streamlit UI for upload, document inventory, request findings, citations, and approval review.
- MongoDB persistence and audit events.
- Synthetic test documents and test fixtures.

### Out of scope

- Sending a purchase order to a supplier.
- Creating an order in an ERP or procurement platform.
- Initiating or approving a payment transaction.
- Automatic approval by an LLM or retrieval result.
- Treating a Critic score, RAG answer, `final_status`, or policy passage as a human approval.
- Treating client-supplied role headers or request-body actor fields as trusted identity.
- Production identity-provider integration unless separately scoped and configured.
- Multi-level/quorum approval unless separately specified.
- Unrestricted ingestion or execution of instructions embedded in PDFs.

---

## 4. High-level architecture

```text
Streamlit UI
  ├── Multi-PDF upload
  ├── Document inventory and ingestion status
  ├── Request intake and findings
  ├── Evidence/citation viewer
  └── Approval queue and decision UI
          |
          v
FastAPI
  ├── Document upload/ingestion API
  ├── Document inventory/version API
  ├── Policy retrieval API/service
  ├── Rate-card extraction and validation service
  ├── Request evaluation/orchestration
  ├── Approval service and authorization
  └── Audit/reporting APIs
          |
          +------------------------+
          |                        |
          v                        v
Document processing          Existing workflow
  ├── PDF validation          ├── Intake
  ├── Text extraction         ├── Policy RAG
  ├── OCR fallback            ├── Vendor matching
  ├── Page-aware chunking     ├── Procurement
  ├── Metadata tagging        ├── Deterministic billing
  └── Embeddings              ├── Critic
          |                   └── Orchestration
          v                        |
Hybrid retrieval index             v
  ├── Keyword/BM25             Evidence-backed findings
  └── Vector search                  |
          |                          v
          +------------------> Deterministic rule engine
                                     |
                      +--------------+--------------+
                      |                             |
                      v                             v
             MongoDB persistence            Approval request
             ├── Document metadata           ├── Pending/decision
             ├── Ingestion jobs               ├── Version/hash binding
             ├── Rate-card records            └── Audit trail
             ├── Ticket/request records
             └── Audit events
```

The vector index may be MongoDB Atlas Vector Search if available. If not, use the project's existing retrieval mechanism or a local vector index for the hackathon. Keep vector-index implementation behind a small interface so it can be changed without rewriting the workflow.

## 5. Document ingestion requirements

### 5.1 Supported document categories

Each document must be classified as one of:

- `company_policy`
- `vendor_policy`
- `vendor_contract`
- `privacy_policy`
- `security_policy`
- `rate_card`
- `procurement_guideline`
- `other`

The category may be selected by the uploader and optionally suggested by a model. A model suggestion must not silently override user-confirmed metadata. Documents marked `other` must not influence approval rules until their applicability is established.

### 5.2 Multi-file upload

The upload interface should allow multiple PDFs in a single batch and display an individual status for every file.

Suggested statuses:

- `uploaded`
- `validating`
- `extracting`
- `needs_ocr`
- `indexing`
- `ready`
- `needs_review`
- `failed`
- `superseded`
- `archived`

A failed file must not cause all other valid files in the batch to fail. Show a specific, safe error for each unsuccessful file.

### 5.3 File validation and safety

- Accept PDF files only for the initial implementation.
- Validate file extension and actual file signature; do not trust the filename alone.
- Enforce configurable upload size, page-count, and processing-time limits.
- Generate a server-side document ID and safe storage name.
- Calculate a cryptographic content hash for duplicate detection and version tracking.
- Do not execute embedded scripts, macros, links, or instructions from documents.
- Treat PDF text as untrusted data. Prompt-injection-like text inside a document is evidence to analyze, not an instruction that can override system or developer rules.
- Avoid logging full document contents or sensitive personal data.
- Store files in a controlled location or object store; do not expose arbitrary local filesystem paths through the API.
- Ensure that the uploader is authorized to upload and view the document category.
- If malware scanning is available in the environment, scan before processing; otherwise document this as a deployment limitation.

### 5.4 Text extraction and OCR

1. Extract text with PyMuPDF or pypdf.
2. Preserve page boundaries and page numbers.
3. Detect low-text or image-only pages.
4. Mark those documents/pages as `needs_ocr` or run a configured OCR fallback.
5. Do not treat a successful PDF parse as proof that all text was extracted correctly.
6. Store extraction status and basic quality indicators.
7. Route poor-quality or ambiguous extraction to human review before using extracted numeric values for enforcement.

### 5.5 Metadata schema

Every document should have metadata similar to:

```json
{
  "document_id": "doc_<server-generated-id>",
  "filename": "vendor_a_rate_card.pdf",
  "content_hash": "<sha256>",
  "document_type": "rate_card",
  "vendor_id": "vendor_a",
  "department": "IT",
  "jurisdiction": "IN",
  "effective_from": "2026-10-01",
  "effective_to": "2027-03-31",
  "version_label": "2026-Q4",
  "status": "ready",
  "uploaded_by": "user_id_from_trusted_auth",
  "uploaded_at": "<UTC timestamp>",
  "extraction_status": "complete",
  "index_status": "ready",
  "page_count": 8,
  "schema_version": 1
}
```

This is an illustrative schema. Dates, vendor IDs, jurisdiction, and version labels must come from verified input or extraction and should be marked unknown when not established. Never invent missing metadata.

Recommended metadata fields:

- `document_id`
- `filename`
- `content_hash`
- `document_type`
- `vendor_id` or `null`
- `department` or `null`
- `jurisdiction` or `null`
- `effective_from` and `effective_to` or `null`
- `version_label` or `null`
- `status`
- `uploaded_by`
- `uploaded_at`
- `extraction_status`
- `index_status`
- `page_count`
- `source_system` or `null`
- `access_scope`
- `schema_version`

## 6. Chunking, embeddings, and retrieval

### 6.1 Chunking

- Split extracted text into meaningful sections or paragraphs where possible.
- Keep chunks small enough for retrieval but large enough to retain the rule and its exceptions.
- Preserve document ID, content hash/version, page number, section heading, and chunk order on every chunk.
- Avoid chunks that combine unrelated policies or separate a rule from its exception.
- For tables, retain row/column relationships where possible; rate-card tables should also be parsed into structured records.

A starting point for experimentation is roughly 400–800 tokens per chunk with modest overlap, but tune this against the actual policy documents and evaluation set. These values are not hard requirements.

### 6.2 Retrieval strategy

Use hybrid retrieval where feasible:

1. Apply access-control and applicability filters first.
2. Filter by vendor, document type, jurisdiction, department, effective dates, and status when known.
3. Run keyword/BM25 retrieval for exact terms, codes, names, and clause identifiers.
4. Run vector retrieval for semantic matches.
5. Merge and rank candidates.
6. Remove duplicate or superseded versions unless the query explicitly asks for historical policy.
7. Optionally rerank the shortlist.
8. Return a limited set of relevant chunks with source metadata.

Do not filter out a relevant company-wide policy just because it has no `vendor_id`. Use applicability rules that allow global company policies alongside vendor-specific documents.

### 6.3 Retrieval output contract

The retrieval service should return structured evidence, not only a paragraph of generated text.

```json
{
  "query_id": "query_<id>",
  "evidence": [
    {
      "evidence_id": "ev_<id>",
      "document_id": "doc_<id>",
      "filename": "procurement_policy.pdf",
      "content_hash": "<sha256>",
      "version_label": "2026-v3",
      "page_number": 12,
      "section": "Competitive Quotations",
      "excerpt": "Illustrative supporting text from the source document.",
      "relevance_score": 0.87,
      "document_type": "company_policy",
      "effective_from": "2026-04-01",
      "effective_to": null
    }
  ],
  "warnings": []
}
```

Scores and fields shown are examples. The service must return real values from retrieval and metadata; do not fabricate scores, source pages, or excerpts.

### 6.4 Citation rules

- Every important policy finding must link to one or more retrieved evidence IDs.
- Show document title, version, page number, section when available, and a concise excerpt.
- If page/section metadata is missing, say it is unavailable instead of inventing it.
- A citation demonstrates where a claim came from; it does not automatically prove that the claim applies to the current request.
- If no sufficiently relevant evidence is retrieved, report `insufficient_evidence` and route to review when required.
- Do not create citations to documents or passages that were not actually retrieved.

## 7. Policy analysis and findings

The LLM may summarize and classify retrieved evidence, identify candidate requirements, and explain likely implications. It must return structured output validated by Pydantic.

Suggested finding fields:

- `finding_id`
- `category` (`price`, `vendor_eligibility`, `approval_threshold`, `privacy`, `security`, `documentation`, `contract_term`, `other`)
- `severity` (`info`, `warning`, `review_required`, `hard_block`)
- `status` (`pass`, `fail`, `unknown`, `conflict`, `not_applicable`)
- `summary`
- `reason`
- `evidence_ids`
- `required_action`
- `confidence` where supported and calibrated
- `rule_id` where a deterministic rule generated the finding

### 7.1 Required behavior

- Distinguish a confirmed violation from a missing fact.
- Distinguish policy text from the system's interpretation of it.
- Do not infer that a vendor is approved merely because its name appears in a document.
- Do not infer a price is compliant based only on a semantically similar passage.
- Do not let an LLM turn missing evidence into a passing result.
- Conflicting, expired, ambiguous, or out-of-scope documents must be surfaced.
- A human review requirement cannot be removed by a favorable LLM summary.
- Hard-block rules remain hard blocks unless an explicit, authorized exception process exists in the policy engine. A normal approval must not override a hard block.

## 8. Rate-card extraction and deterministic validation

### 8.1 Why rate cards need a structured path

RAG is useful for locating pricing rules and explaining them, but it is not sufficiently reliable as the sole mechanism for exact numeric comparison. Extract rate-card entries into validated structured records, and perform calculations in Python.

### 8.2 Rate-card schema

```json
{
  "rate_id": "rate_<id>",
  "document_id": "doc_<id>",
  "content_hash": "<sha256>",
  "vendor_id": "vendor_a",
  "item_or_service_code": "LAPTOP_STANDARD",
  "description": "Standard laptop",
  "unit_rate": "62000.00",
  "currency": "INR",
  "unit": "each",
  "minimum_quantity": 1,
  "maximum_quantity": null,
  "discount_rule": null,
  "tax_treatment": "not_verified",
  "effective_from": "2026-10-01",
  "effective_to": "2027-03-31",
  "source_page": 4,
  "verification_status": "needs_review",
  "verified_by": null,
  "verified_at": null
}
```

Use decimal strings or Python `Decimal` for money; do not use binary floating-point values for financial calculations. The sample values are illustrative, not real vendor pricing.

### 8.3 Validation steps

For each quote:

1. Match the vendor using a stable vendor ID, not only a fuzzy name match.
2. Match the item/service code or route ambiguous descriptions to review.
3. Select the rate-card version valid for the request date and applicable scope.
4. Check currency and unit compatibility.
5. Check minimum/maximum quantities and discount tiers.
6. Handle taxes, shipping, surcharges, and discounts only when the relevant rule and inputs are established.
7. Calculate expected totals with `Decimal`.
8. Compare quoted values with the expected values using an explicit tolerance and configured business rule.
9. Preserve the source rate ID and source document/page.
10. If multiple rate entries are equally plausible, conflict, or are unverified, do not choose silently; mark the finding for review.

Suggested result fields:

- `quoted_unit_rate`
- `expected_unit_rate`
- `currency`
- `quantity`
- `quoted_total`
- `expected_total`
- `difference_amount`
- `difference_percentage` when meaningful
- `rate_id`
- `source_document_id`
- `source_page`
- `rate_card_version`
- `validation_status` (`pass`, `fail`, `unknown`, `conflict`, `needs_review`)
- `reason`

No rate-card result should be treated as authoritative until the extracted data has passed the configured verification process.

## 9. Policy applicability, versioning, and conflicts

### 9.1 Applicability

For each document, evaluate:

- Is the document active for the request date?
- Does it apply to the relevant vendor, department, service, jurisdiction, or request type?
- Is it a company-wide policy or vendor-specific document?
- Is it superseded or archived?
- Is its version verified?
- Does the requester have permission to use and view it?

### 9.2 Version handling

- Never silently overwrite a prior document version.
- Use a content hash to detect identical uploads.
- Store revisions as separate versions or records linked by a stable logical document key.
- Mark superseded versions clearly.
- Preserve the exact source version and content hash used in each evaluation and approval snapshot.
- Permit historical lookup for audit, but do not use a superseded version as current policy unless the user explicitly requests historical analysis.

### 9.3 Conflict handling

Examples of conflicts include:

- Company policy and vendor contract appear to specify different terms.
- Two active rate cards have overlapping validity dates.
- A policy excerpt sets a threshold while another relevant document appears to set a different threshold.
- Effective dates or jurisdiction cannot be determined.

The system must:

1. Preserve both sources.
2. State the nature of the conflict without inventing a hierarchy.
3. Identify which facts are missing.
4. Apply only a documented precedence rule configured and approved by the organization.
5. Otherwise return `conflict`/`review_required` and route to the authorized owner.

Do not assume that a vendor document overrides company policy, or that the newest upload always has the highest authority. Precedence must be explicitly configured and reviewed.

## 10. Approval Safety integration

The document RAG layer supplies evidence; the Approval Service controls the decision lifecycle. Keep them separate.

### 10.1 Approval triggers

Use a deterministic policy function, such as:

```python
evaluate_approval_requirement(
    workflow_result=workflow_result,
    policy_findings=policy_findings,
    rate_validation=rate_validation,
    policy_config=policy_config,
)
```

The function should return a structured result including:

- `approval_required`
- `required_role`
- `risk_level`
- `reason_codes`
- `policy_version`
- `evidence_ids`
- `blocking_findings`
- `missing_evidence`
- `workflow_snapshot_hash`

Possible triggers include:

- configured spending thresholds;
- a policy explicitly requiring human review;
- rate-card mismatch;
- missing mandatory quotations or documents;
- vendor eligibility uncertainty;
- privacy/security review requirement;
- unresolved policy conflict;
- configured exception workflow.

Triggers must be defined in policy configuration or deterministic rules, not invented by the LLM at runtime.

### 10.2 Hard blocks versus approval-required findings

Keep these outcomes distinct:

- **Pass:** sufficient evidence and deterministic rules are satisfied.
- **Review required:** a human must inspect an exception, conflict, or missing evidence.
- **Hard block:** the request cannot proceed under the configured policy. Ordinary approval does not override it.
- **Unknown:** there is insufficient reliable information to decide.

A request that is hard-blocked must not become compliant merely because an approver clicks Approve. If the organization requires exceptions, implement a separate, explicitly authorized exception process with its own policy and audit trail.

### 10.3 Approval lifecycle

Use the lifecycle from the Approval Safety specification:

- `pending`
- `approved`
- `rejected`
- `changes_requested`
- `expired`
- `superseded`

Terminal states are immutable. A material change to request details, vendor, amount, currency, quote, policy evidence, or workflow snapshot should invalidate or supersede the existing approval request and require reevaluation.

Approval decisions must be bound to:

- ticket/request ID and version;
- workflow snapshot hash;
- policy/document versions and evidence IDs;
- actor identity and role obtained from trusted server-side authentication;
- decision timestamp;
- idempotency key;
- decision comment where required.

Do not trust actor identity, role, or approval status sent by the browser. Enforce permissions in the backend. The requester must not approve their own request. A decision endpoint must use an atomic conditional state transition and be safe against retries and concurrent decisions.

### 10.4 Draft-only boundary

An approved request is an internally approved draft only. It must not:

- submit or email a PO to a vendor;
- create an order in a third-party system;
- call a payment API;
- initiate a transfer;
- execute an external purchasing action.

Make this clear in the UI and API documentation. Add tests that assert no external execution path is called.

## 11. Evidence snapshot and auditability

For each request evaluation, store an immutable or append-only snapshot of the evidence needed to explain the result.

Recommended snapshot fields:

- request/ticket ID and version;
- workflow snapshot hash;
- evaluation timestamp;
- policy rule-set version;
- document IDs, content hashes, version labels, and effective dates;
- retrieved evidence IDs and page/section references;
- structured rate IDs and source references;
- rate validation results;
- policy findings and their status;
- missing evidence and conflicts;
- approval requirement and reason codes;
- model/provider/deployment identifiers when available;
- schema version.

Do not store more document text or personal data than needed. Store compact excerpts or references where possible, and apply access control and retention rules.

Suggested audit events:

- `document_uploaded`
- `document_validation_failed`
- `document_extraction_completed`
- `document_indexing_completed`
- `document_marked_needs_review`
- `document_version_superseded`
- `rate_card_extraction_created`
- `rate_card_verified`
- `policy_evaluation_completed`
- `policy_conflict_detected`
- `approval_request_created`
- `approval_decision_attempted`
- `approval_decision_recorded`
- `approval_request_superseded`
- `approval_reconciliation_required`

Audit records should identify the actor from trusted context when applicable and should not include secrets, access tokens, full raw documents, or unnecessary personal data.

## 12. MongoDB collections and indexes

Reuse the existing MongoDB configuration, client, and repository patterns.

Potential collections:

### `policy_documents`

Stores document metadata and version records. Store binary files separately or in a suitable object store; avoid placing large PDF binaries in ordinary metadata documents.

Suggested indexes:

- unique `document_id`
- `content_hash`
- `(document_type, status)`
- `(vendor_id, document_type, status)`
- `(effective_from, effective_to)`

### `policy_chunks`

Stores extracted chunk text and metadata if MongoDB is used for chunk storage.

Suggested indexes:

- `(document_id, content_hash, page_number)`
- metadata fields needed for access and applicability filters
- vector index where supported and configured

### `rate_card_entries`

Stores structured, validated rate-card rows.

Suggested indexes:

- `(vendor_id, item_or_service_code, effective_from, effective_to)`
- `document_id`
- `verification_status`

### `approval_requests`

Use the Approval Safety specification's collection and indexes, including unique approval ID and unique `(ticket_id, request_version)` where compatible with the chosen data model.

### `audit_events`

Use the existing audit pattern. Prefer append-only events and avoid destructive overwrites of decision history.

### `ingestion_jobs` (optional)

Tracks per-file processing attempts, stages, timestamps, safe error codes, and retry state. Keep this separate if the current document model would otherwise become difficult to manage.

Create indexes idempotently and test index creation against the target MongoDB deployment. Vector search indexes may require a separate Atlas configuration and should not be assumed to exist merely because MongoDB is configured.

## 13. API design

Adapt paths to existing conventions; these are proposed endpoint contracts, not a requirement to duplicate existing routes.

### Documents

- `POST /api/policy-documents/upload` — accept multiple PDFs and metadata; return one result per file.
- `GET /api/policy-documents` — list documents visible to the current user, with filters for type, vendor, status, and version.
- `GET /api/policy-documents/{document_id}` — retrieve permitted metadata and ingestion status.
- `POST /api/policy-documents/{document_id}/reprocess` — reprocess an authorized document where supported.
- `POST /api/policy-documents/{document_id}/archive` — archive without deleting audit history, subject to authorization.

### Policy search and evaluation

- `POST /api/policy/search` — retrieve applicable evidence with citations.
- `POST /api/requests/{ticket_id}/evaluate-policy` — evaluate the request using retrieval, structured rate validation, and deterministic rules.
- `GET /api/requests/{ticket_id}/evidence` — return the evidence snapshot permitted for the caller.

### Rate cards

- `GET /api/rate-cards` — list rate-card entries using vendor, item, currency, validity, and verification filters.
- `POST /api/rate-cards/{rate_id}/verify` — verify an extracted rate entry, requiring an authorized role and audit event.

### Approvals

Use the existing Phase 2 Approval Safety contracts where already defined:

- `POST /api/tickets/{ticket_id}/approval-requests`
- `GET /api/approvals?status=pending`
- `GET /api/approvals/{approval_id}`
- `POST /api/approvals/{approval_id}/decision`
- `GET /api/tickets/{ticket_id}/approval`

Do not create duplicate routes if equivalent routes already exist. Maintain one canonical service for approval state transitions.

### Error handling

Use consistent status codes and safe messages:

- `400` malformed request;
- `401` unauthenticated;
- `403` unauthorized;
- `404` resource not found or not visible to the caller;
- `409` conflicting version, stale request, terminal state, or conflicting idempotency key;
- `413` upload too large;
- `422` invalid metadata or domain validation;
- `429` rate limit exceeded where configured;
- `503` required database, authentication, retrieval, or policy dependency unavailable.

Never return credentials, raw stack traces, or sensitive document contents in error responses.

## 14. Pydantic contracts

Define explicit Pydantic models for at least:

- `PolicyDocumentMetadata`
- `DocumentUploadResult`
- `IngestionStatus`
- `PolicyEvidence`
- `PolicyFinding`
- `RateCardEntry`
- `RateValidationResult`
- `PolicyEvaluationResult`
- `ApprovalRequirement`
- `ApprovalDecisionRequest`
- `ApprovalRequestResponse`
- `EvidenceSnapshot`

Use enums for status fields and validate currencies, dates, quantities, and IDs. For monetary values, parse to `Decimal` and serialize consistently. Ignore or reject unknown fields deliberately; do not let arbitrary client payloads set `uploaded_by`, `actor_id`, `role`, `approved_by`, or persisted approval state.

## 15. Streamlit UI behavior

### 15.1 Policy document management

Provide a dedicated Policy Library page with:

- multi-file PDF uploader;
- per-file document type and optional vendor metadata;
- optional department/jurisdiction/effective-date fields;
- upload progress and individual processing statuses;
- clear extraction/indexing failures and retry actions;
- document list with type, vendor, version, effective dates, and status;
- duplicate/version indicators;
- restricted actions for archive, reprocess, or rate verification.

Do not display private document contents to users who lack access.

### 15.2 Request evaluation view

Show:

1. Request summary and vendor.
2. Total and currency with deterministic calculation details.
3. Rate-card result and source reference.
4. Policy findings grouped by severity.
5. Supporting citations with document name, version, page, section, and excerpt.
6. Missing information and conflicting evidence.
7. Required human action and explanation.
8. Workflow/evidence snapshot version.

The UI must distinguish `pass`, `fail`, `unknown`, `conflict`, `review_required`, and `hard_block`; do not collapse them into a single green/red status.

### 15.3 Approval queue

- Show pending requests and their reasons.
- Display relevant policy evidence and rate-card comparison before a decision.
- Show who requested the review, the required role, and any separation-of-duties restriction.
- Require confirmation before approve/reject actions.
- Require comments for rejection and, by default, for requesting changes.
- Refresh authoritative state after decisions.
- On `409`, show the latest server state and explain that the request changed or was already decided.
- On `503`, do not display a success message.
- Never use optimistic UI state as proof that a decision was persisted.
- Display a clear statement: “Approved internally — purchase order remains a draft; no order or payment has been executed.”

## 16. Security and privacy requirements

- Use trusted server-side authentication and authorization for document upload, search, archive, rate verification, and approval decisions.
- Do not treat a client-supplied `X-User-Role` header as trusted proof.
- Apply document-level and tenant/department-level access filters before retrieval, not only after the answer is generated.
- Do not let one user infer the existence or content of documents they cannot access.
- Keep secrets in environment variables or the approved secret store; never commit `.env` or credentials.
- Limit and redact logs.
- Configure retention and deletion behavior with audit requirements in mind.
- Use TLS in deployed environments.
- Apply upload limits and protect against resource exhaustion.
- Treat retrieved document text as untrusted input in LLM prompts.
- Tell the model to cite evidence and return structured output, but enforce critical rules in code.
- If trusted authentication, required policy data, or the authoritative database is unavailable, fail closed for protected operations and approval decisions.
- Keep external procurement/payment integrations absent or disabled for this phase.

## 17. Failure handling and recovery

| Failure | Required behavior |
|---|---|
| One PDF in a batch is corrupt | Mark that file failed; continue processing other valid files |
| Text extraction is empty or poor | Trigger OCR where configured or mark `needs_review` |
| Embedding service unavailable | Mark indexing as failed/retryable; do not claim the document is searchable |
| Vector index unavailable | Use a configured keyword-only fallback only if allowed; clearly report degraded retrieval |
| No relevant evidence | Return `insufficient_evidence`; never fabricate a citation |
| Rate card expired | Do not use it as current without an explicit policy exception; flag review |
| Multiple plausible rate entries | Return `conflict`/`needs_review` |
| Policy versions conflict | Preserve evidence and escalate unless documented precedence resolves it |
| MongoDB unavailable | Fail closed for persistence-dependent decisions; do not claim approval or audit was saved |
| Approval decision races | Use atomic conditional update; return current state or `409` |
| Retry with same idempotency key and same payload | Return the recorded result without a duplicate decision |
| Same idempotency key with different payload | Return `409` |
| Request/evidence snapshot changes | Mark existing approval superseded and require reevaluation |
| LLM timeout or malformed output | Retry within a limit or return a safe unavailable/needs-review result |
| User lacks document permission | Deny access without leaking document contents |

If the system is operating in degraded retrieval mode, it must not silently treat absent evidence as compliance.

## 18. Test plan

### 18.1 Ingestion tests

- Multiple valid PDFs upload successfully.
- One invalid PDF does not prevent other files from processing.
- Duplicate content hash is detected.
- Page numbers remain correct after extraction and chunking.
- Scanned/low-text documents are flagged for OCR or review.
- Metadata validation rejects invalid date ranges.
- Unauthorized users cannot upload, read, archive, or reprocess documents.

### 18.2 Retrieval and citation tests

- Company-wide policy and vendor-specific policy are both retrieved when applicable.
- A document for Vendor A is not incorrectly used as evidence for Vendor B.
- Expired and superseded policies are excluded from current-policy answers by default.
- Relevant evidence includes correct document ID, version, and page.
- No-evidence queries return `insufficient_evidence`.
- Prompt-injection text inside a PDF cannot override system behavior.
- Restricted documents do not leak through vector, keyword, reranking, or citation paths.

### 18.3 Rate-card tests

- Exact vendor and service match.
- Quote below, equal to, and above the verified rate.
- Currency mismatch.
- Quantity break and discount rule.
- Expired and overlapping rate cards.
- Ambiguous item match.
- Missing or unverified extracted price.
- Decimal arithmetic and rounding.
- Tax and surcharge are not invented when source data is missing.

### 18.4 Policy evaluation tests

- Clear pass.
- Clear hard block.
- Human review required.
- Unknown due to missing evidence.
- Conflicting active policy documents.
- Privacy/security review requirement.
- Missing mandatory quote/document.
- Model output cannot override deterministic rules.
- Every important finding references valid evidence IDs.

### 18.5 Approval and persistence tests

- Requester cannot approve their own request.
- Unauthorized roles cannot decide.
- Client-supplied actor/role fields are ignored or rejected.
- Stale request version/hash cannot be approved.
- Duplicate same-key retry is idempotent.
- Reused key with a different payload is rejected.
- Concurrent decisions produce one authoritative transition.
- Terminal approval states cannot be overwritten.
- Material request changes supersede prior approval.
- Restart preserves approval, evidence snapshot, rate references, and audit history.
- No external purchase/payment function is called.

### 18.6 End-to-end acceptance scenario

Create synthetic documents:

1. Company procurement policy with a documented approval threshold and quotation requirement.
2. Vendor A contract.
3. Vendor A rate card with a verified, effective-dated rate.
4. Privacy/security policy describing a review requirement for sensitive data.

Submit a synthetic request with a quote that violates one configured rule or lacks required evidence.

Expected result:

- The correct documents are retrieved.
- Each finding cites a real page/section from the synthetic files.
- The rate comparison is calculated by deterministic code.
- The missing evidence or violation is visible and correctly classified.
- The request is blocked or routed for review according to configured rules.
- A persisted approval request is created only when the rules call for it.
- An unauthorized user and the requester cannot approve it.
- An authorized approver can record an internal decision when the request is eligible for approval.
- The request remains a draft, with no external order or payment.

Use synthetic documents and values for the demo unless real/anonymized company documents have been approved for this environment. Do not upload confidential documents to an unapproved service.

## 19. Suggested code organization

Adapt to the repository's existing structure and avoid duplicate services.

```text
app/
  api/
    policy_documents.py
    policy_search.py
    rate_cards.py
    approvals.py                 # reuse existing approval routes if present
  schemas/
    policy_documents.py
    policy_evidence.py
    rate_cards.py
    approvals.py
  services/
    document_ingestion_service.py
    pdf_extraction_service.py
    chunking_service.py
    embedding_service.py
    policy_retrieval_service.py
    policy_analysis_service.py
    rate_card_extraction_service.py
    rate_validation_service.py
    policy_evaluation_service.py
    evidence_snapshot_service.py
    approval_service.py          # reuse Phase 2 implementation
  repositories/
    policy_document_repository.py
    policy_chunk_repository.py
    rate_card_repository.py
    approval_repository.py
    audit_repository.py
  policy/
    applicability.py
    precedence.py
    deterministic_rules.py
  db/
    client.py                    # reuse existing MongoDB client
    indexes.py                   # extend idempotently
  ui/
    pages/
      policy_library.py
      request_evaluation.py
      approval_queue.py
tests/
  test_document_ingestion.py
  test_policy_retrieval.py
  test_policy_citations.py
  test_rate_card_validation.py
  test_policy_conflicts.py
  test_policy_authorization.py
  test_approval_safety.py
  test_evidence_persistence.py
  test_no_external_execution.py
```

The names above are a suggested organization. Inspect the current repository before adding files and follow established naming and import conventions.

## 20. Implementation sequence

### Phase 2A — Contracts and document inventory

- Inspect the current repository and Phase 1 persistence state.
- Define Pydantic schemas and document metadata/version model.
- Add MongoDB repositories and idempotent indexes.
- Add multi-PDF upload validation and document inventory endpoints.
- Add tests for metadata, authorization, and partial batch failure.

**Exit criterion:** Multiple PDFs can be registered safely, and every file has a durable status and version/hash metadata.

### Phase 2B — Extraction and retrieval

- Add page-aware text extraction.
- Add OCR fallback or explicit review status.
- Implement chunking with metadata.
- Connect to Azure AI Foundry embedding deployment using existing environment configuration.
- Add hybrid retrieval and access/applicability filters.
- Return source-grounded evidence objects.
- Add citation and no-evidence tests.

**Exit criterion:** A policy query retrieves relevant, authorized passages with verifiable source references.

### Phase 2C — Structured rate cards and deterministic policy rules

- Extract rate-card rows.
- Add a review/verification step for ambiguous or critical values.
- Implement `Decimal` calculations and validity checks.
- Define policy applicability and precedence explicitly.
- Implement structured findings and approval-requirement calculation.
- Test expired rates, conflicts, currency mismatch, and hard blocks.

**Exit criterion:** Rate checks and critical rule outcomes are reproducible and do not depend on free-form LLM arithmetic.

### Phase 2D — Approval integration

- Reuse the Phase 2 Approval Safety design.
- Bind approval requests to request versions, evidence snapshots, policy versions, and hashes.
- Implement backend authorization, no-self-approval, atomic transitions, and idempotency.
- Ensure hard blocks cannot be overridden by ordinary approval.
- Persist audit events and recovery state.

**Exit criterion:** Only an authorized eligible request can transition through the approval lifecycle, and every decision is traceable.

### Phase 2E — UI, integration, and verification

- Add Policy Library, request evidence view, rate comparison, and approval queue.
- Display citations and uncertainty/conflicts.
- Add integration tests and restart tests against disposable MongoDB data.
- Run the full test suite and record actual outcomes.
- Confirm no external purchasing/payment code path is invoked.
- Update README and `.env.example` without committing secrets.

**Exit criterion:** A complete synthetic scenario can be demonstrated from multi-PDF upload through evidence-backed evaluation and safe internal approval.

## 21. Configuration

Use environment variables or the approved secret manager for configuration. Example names:

```dotenv
MONGODB_URI=
MONGODB_DATABASE=
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
AZURE_OPENAI_API_VERSION=
AZURE_OPENAI_CHAT_DEPLOYMENT=
AZURE_OPENAI_EMBEDDING_DEPLOYMENT=
POLICY_UPLOAD_MAX_MB=20
POLICY_MAX_PDF_PAGES=500
POLICY_RETRIEVAL_TOP_K=8
POLICY_OCR_ENABLED=false
POLICY_REQUIRE_VERIFIED_RATE_CARDS=true
POLICY_FAIL_CLOSED_ON_RETRIEVAL_FAILURE=true
APPROVAL_AUTH_MODE=local_demo
```

These are example variable names, not assertions about the repository's current configuration. Reuse existing names where available. Do not commit real values. The `local_demo` authentication mode must be explicitly restricted to local/demo use; any deployment intended for shared or production-like use must use trusted authentication and fail closed if it is unavailable.

## 22. Definition of done

Phase 2 is complete only when:

- [ ] Multiple PDFs can be uploaded and processed independently.
- [ ] Document metadata, content hash, status, and versions are persisted.
- [ ] Retrieval respects document access and applicability filters.
- [ ] Citations point to actual source documents and pages.
- [ ] No-evidence, expired-policy, and conflict states are explicit.
- [ ] Rate-card comparisons use verified structured values and deterministic `Decimal` arithmetic.
- [ ] Critical rules are enforced in backend code.
- [ ] Evidence snapshots preserve the exact source versions used in evaluation.
- [ ] Approval requests are bound to request/evidence versions and hashes.
- [ ] Authorization and no-self-approval are enforced server-side.
- [ ] Approval transitions are atomic and idempotent.
- [ ] Audit events are durable and do not leak secrets.
- [ ] UI never claims success before persistence is confirmed.
- [ ] No order, vendor submission, or payment is executed.
- [ ] Automated tests and restart tests have been run, with actual results recorded.
- [ ] README and environment examples describe setup, limitations, and safe demo behavior.

## 23. Recommended implementation defaults

For the hackathon implementation:

- Reuse the existing FastAPI, Pydantic, Streamlit, MongoDB, and policy retrieval components.
- Start with PDFs and add OCR only where needed.
- Use hybrid retrieval and preserve page-level citations.
- Treat rate cards as structured data, not only vector chunks.
- Require verification for ambiguous extracted rates.
- Use explicit, deterministic policy rules for hard blocks and approval triggers.
- Escalate unresolved conflicts instead of guessing precedence.
- Use a single authorized approver role for the initial demo, with no self-approval.
- Store an evidence snapshot with each evaluated request and approval.
- Keep all procurement actions draft-only.
- Test against synthetic documents first; use real/anonymized company documents only if approved for this environment.

---

## Final design rule

**RAG finds the evidence. Structured extraction supplies candidate facts. Deterministic code validates amounts and enforces rules. The authorized human makes the approval decision. The application records an internal decision only; it never executes a purchase or payment.**
