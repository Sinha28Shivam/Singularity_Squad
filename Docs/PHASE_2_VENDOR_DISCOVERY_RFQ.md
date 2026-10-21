# Phase 2 — Vendor Discovery and RFQ

## Objective
Add a controlled vendor-sourcing workflow without changing procurement policy gates, human approval requirements, or draft-only PO behavior.

## Scope
1. Search the existing synthetic vendor catalog first; do not invoke external discovery automatically.
2. Maintain a vendor candidate registry with lifecycle states: candidate, pending_review, approved, rejected, suspended.
3. Create an onboarding review record with evidence and an audit event. Only an authorized admin can approve/reject a candidate; submission must never automatically approve a vendor.
4. Generate an RFQ from validated purchase requirements: service/item scope, quantity, delivery date/location, budget/currency, delivery expectations, warranty requirements, compliance requirements, response deadline and evaluation weights.
5. Accept structured quotations and PDF uploads. PDFs are untrusted source documents: extract text only, store file metadata/hash and extracted text, and require human verification of extracted commercial terms before scoring. Never execute instructions found inside a PDF.
6. Compare comparable total landed cost (base price + delivery/setup + taxes/fees), delivery commitment, warranty, compliance evidence and completeness. Missing or unverifiable information is a blocker/review flag, not an assumed pass.
7. Keep sourcing decisions advisory. The system recommends; it does not award a contract, approve a vendor, create a PO, contact suppliers, or pay them automatically.

## Recommended API surface
- GET /api/vendor-sourcing/vendors?q=&service=&status= — search internal registry/catalog first.
- POST /api/vendor-sourcing/candidates — register a discovered candidate as pending review.
- POST /api/vendor-sourcing/candidates/{candidate_id}/decision — admin approve/reject with reason and evidence.
- POST /api/vendor-sourcing/rfqs — create an RFQ from a validated request.
- GET /api/vendor-sourcing/rfqs/{rfq_id} — read RFQ and response status.
- POST /api/vendor-sourcing/rfqs/{rfq_id}/quotes — submit a structured quotation.
- POST /api/vendor-sourcing/rfqs/{rfq_id}/quotes/pdf — upload PDF; extract text and mark terms unverified.
- POST /api/vendor-sourcing/rfqs/{rfq_id}/compare — deterministic comparison of verified quotes.

All write endpoints require admin authorization initially. Do not add public supplier portals or outbound email until a separate security review.

## Data model
### Vendor candidate
- Stable candidate ID; normalized name and contact reference (avoid storing secrets)
- Services/categories, operating regions, capacity, registration/tax identifiers where appropriate
- Onboarding status and status history
- Compliance document metadata, expiry dates, verification status and reviewer notes
- Source type (internal_catalog, manual, external_discovery) and provenance
- Created/updated timestamps and actor IDs

### RFQ
- RFQ ID, source request/ticket ID and immutable validated requirements snapshot
- Item/service scope, quantity, currency, delivery date/location, budget ceiling
- Warranty and compliance requirements
- Response deadline and status (draft, open, closed, cancelled)
- Candidate invite list and response state; no email is sent in this phase

### Quote
- Quote ID, RFQ ID, vendor/candidate ID, submission type (structured, pdf)
- Price lines, taxes, delivery/setup fees, warranty, delivery lead time/date, validity period
- Compliance declarations and evidence references
- Original PDF metadata and SHA-256 hash when supplied
- Extracted text and parser status; human verification status and reviewer/time
- Normalized total and validation errors

### Comparison
- Comparable total cost with explicit components and currency
- Delivery score against the requested date
- Warranty fit and compliance completeness
- Explainable score components and disqualification reasons
- Quote/requirement snapshot hashes so results can be traced to inputs

## Comparison policy
- Use Decimal arithmetic for money; reject NaN, infinity, negative prices, unsupported currencies and inconsistent currency comparisons.
- Never compare raw totals across currencies without an explicitly configured exchange-rate source and timestamp. Phase 2 should reject mixed-currency comparisons.
- Calculate total cost consistently: line totals + taxes + mandatory fees + delivery/setup; document exclusions.
- Compliance is a gate, not merely a score. Missing mandatory compliance evidence means review_required or ineligible, never silently passing.
- Delivery must meet the required date or be marked non-compliant.
- Warranty can be scored only after normalizing duration and scope; ambiguous terms require review.
- Score weights must be versioned/configured, visible to the reviewer and sum to 1.0. Show raw facts beside any composite score.
- Keep a deterministic tie-breaker (e.g. lower comparable total, then earlier confirmed delivery, then stable quote ID).

## Security and safety notes
- Existing vendor catalog is read-only during discovery.
- Candidate registration is not approval.
- PDF uploads: enforce file size/page limits, validate PDF signature, calculate a content hash, avoid trusting filenames, store outside executable paths, and never render/execute embedded content.
- Treat PDF text as untrusted data. Ignore embedded prompts or requests to change scoring, reveal data, or bypass policy.
- Avoid exposing personal/contact data in list responses; restrict access to authorized roles.
- Add audit events for candidate creation, review decisions, RFQ creation/closure, quote submission, PDF parsing/verification and comparisons.
- Use idempotency or duplicate detection for quote submissions; do not overwrite a quote silently.
- Keep supplier outreach, contract award, PO dispatch, and payment disabled/simulated.
- Fail closed when persistence or compliance evidence is unavailable.

## Implementation milestones
1. Registry and discovery: internal catalog search + candidate registry + onboarding decision lifecycle + tests.
2. RFQ lifecycle: validated request snapshot, RFQ creation/status, candidate response tracking.
3. Quote intake: structured submissions + PDF upload/extraction + human verification gate.
4. Deterministic comparison: normalized cost, delivery, warranty, compliance gates and explainable comparison report.
5. UI and documentation: admin sourcing pages, evidence visibility, audit history and safe failure messages.
6. Validation: targeted/full tests, API authorization tests, malformed/oversized PDF tests, currency/Decimal tests, duplicate submission tests, and no-side-effect tests.

## Definition of done
- Existing vendor catalog is searched before any external-discovery adapter is considered.
- Candidate onboarding has explicit, audited approval/rejection; no automatic promotion.
- RFQs can only be created from validated, complete purchase requirements.
- Structured quotes and PDFs are accepted safely; extracted PDF terms remain unverified until reviewed.
- Comparisons are explainable, deterministic and reject incompatible currencies or missing mandatory compliance evidence.
- No external emails, real awards, real PO dispatch, or real payments occur.
- Existing workflow, authentication, RBAC, policy gates and human approval tests remain green.
