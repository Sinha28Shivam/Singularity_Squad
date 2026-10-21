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

## Implementation status (2026-10-21, branch `feat/vendor-sourcing-phase-2a`)

Implements Phase 2A (stabilize) and 2B (admin UI) of `Docs/WorkplaceOps_Master_Review_and_Vendor_Discovery_RFQ_Plan.md`. 2C is partly done: an explanatory, versioned score, while ranking stays rule-based. 2D (external discovery) and 2E (supplier communication) are **not implemented** and stay disabled.

### Baseline before this work

On `master` 4ea84c7 (PR #2) merged with the orchestration hardening, the full suite gave **893 passed, 2 failed**. The two failures were `test_candidate_starts_pending_and_requires_explicit_review` and `test_candidate_reject_and_invalid_transition`. The cause: `decide_candidate` used `find_one_and_update(..., projection={"_id": 0})`, which returns `None` under mongomock, so every decision came back 409. This is the plan's warning that the merged tests had never been run.

### Review findings addressed

| # | Finding | Resolution |
|---|---|---|
| P0.1 / P0.7 | Writes and audit events not transactional | **Single-document outbox.** Each state change and its pending audit event(s) are written in one atomic document write. Events are then delivered to `vendor_candidate_events` / `vendor_rfq_events` with deterministic `event_id = {entity_id}:{seq}`, so delivery is idempotent. If delivery fails, the committed state is returned with `audit_pending: true` (never a 503 after a committed write). `POST /candidates/repair-events` and `POST /rfqs/repair-events` (also run on reads) re-deliver events exactly once. The RFQ repair also restores a quote left superseded by an interrupted revision. |
| P0.2 | PDF bytes stored in MongoDB | `app/vendor_sourcing/storage.py` keeps a private file store (`VENDOR_QUOTE_STORE_DIR`, default `data/vendor_quote_store`, git-ignored). Keys are generated by the server, writes are atomic, path traversal is blocked, and `purge_older_than(days)` enforces retention. MongoDB keeps only `pdf_storage_key`, `pdf_sha256`, `pdf_size` and `pdf_page_count`; the key is never returned to clients. Admin download at `GET /rfqs/{id}/quotes/{qid}/pdf` re-checks the sha256 and sends `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`. |
| P0.3 | Raw-body PDF contract | Documented in OpenAPI: raw `application/pdf` body with `vendor_id`, `quote_reference` and `filename` query parameters. The stream is capped at 5 MiB before it is fully read (413). Wrong content type gives 415. Bad signature, malformed, encrypted, 0 pages or more than 20 pages give 422. The filename is reduced to a sanitized basename. |
| P0.4 | Verification overwrote submitted values | A quote keeps immutable `submitted` values and separate `verified` values, plus `verification.diff` / `fields_changed`, reviewer, note and time. Comparisons use the verified values. |
| P0.5 | Incomplete RFQ states | `POST /rfqs/{id}/close` and `/cancel` require a reason; an invalid transition gives 409. `GET /rfqs?status=&request_id=&limit=&offset=` lists RFQs. Quotes are refused once an RFQ is closed, cancelled or past its deadline. Comparing a cancelled RFQ gives 409. Each RFQ has a `status_history`. |
| P0.6 | Compliance as booleans | Each requirement is `{status: verified\|declared\|missing, evidence_ref, expires_on}`. Only `verified` evidence that hasn't expired by the RFQ delivery date passes. `declared` gives `review_required` (not eligible). Legacy `true` is treated as `declared`. Candidates carry `compliance_evidence` metadata, which can never be marked verified on submission. |
| P1.8 | No UI | Admin **🏪 Vendor sourcing** page (see below). |
| P1.10 | No weights | `data/vendor_scoring_policy.json` (`vsp-1`, Decimal weights that must sum to exactly 1.0, checked on load). The per-row `score` components are **explanatory only**. Eligibility gates plus lowest verified total, then earliest delivery, then quote ID still decide the rank. |
| P1.11 | Simplistic warranty | The RFQ can set `warranty_scope_required`. The reviewer records `warranty_scope_accepted`, and the months and scope gates are reported separately. |
| P1.12 | Duplicate/revision contract | A duplicate `(rfq, vendor)` quote gives 409. Changes go through `POST /rfqs/{id}/quotes/{qid}/revise`, which creates a new quote with `revision` n+1 and `revision_of`, and marks the previous one `superseded`. Unique `(rfq_id, vendor_id, revision)` plus a partial unique index allow one active quote per vendor. |
| P1.13 | External discovery | Still disabled (`external_discovery_enabled: false`). |
| P1.14 | RBAC | Every route requires an admin. Users and approvers get 403 and unauthenticated callers 401. Verification stays with admins; a separate procurement-reviewer role is deferred. |
| P1.15 | Error mapping | Responses use the uniform `{"detail": …}` shape: validation 422/409, not found 404, persistence 503. |
| P2.16 | Metrics | `GET /api/vendor-sourcing/metrics` (in-process counters for candidates, decisions, RFQs, quotes, verifications, ineligible reasons, comparisons and persistence failures). |
| P2.18 | Audit metadata | Events carry actor, action, entity ID, previous and new state, reason, timestamp and correlation ID (`X-Correlation-ID` header, sanitized). |
| P2.19 | Pagination | Candidates and RFQs take `limit` (1–200) and `offset` (0–10000), and responses include `total` / `next_offset`. |
| Concurrency | Duplicate or concurrent writes | A unique partial index on the active normalized name gives one 201 and one 409. Candidate decisions are compare-and-set, so exactly one wins. One accepted comparison is allowed per RFQ. |

**Human decision on a comparison:** every comparison is saved in `vendor_rfq_comparisons` with `rfq_snapshot_hash`, a per-quote `quote_snapshot_hash`, the scoring version and `human_decision: pending`. `POST /rfqs/{id}/comparisons/{cid}/decision` (`accept` with a quote, or `reject`, plus a reason) records the human choice and closes the RFQ. **It does not award, create or dispatch a PO, contact anyone or pay.**

### Admin UI (`ui/pages_portal/vendor_sourcing.py`)

- **Every tab:** an advisory banner, a confirmation checkbox and a reason for every decision, close, cancel, verify or selection, and safe `detail` error messages.
- **Tabs:**
  - **Search:** the internal catalog and the candidate registry are shown separately, with a "no external search" note.
  - **Candidates:** register with evidence rows; a review queue showing provenance, evidence and history, with approve/reject/suspend.
  - **RFQs:** create, prefilled from a validated ticket; list with status filter; detail; close/cancel.
  - **Quotes:** structured entry, revision, PDF upload (raw bytes), and the extracted text shown as **untrusted** in a disabled box. The verify form shows a submitted-vs-verified diff.
  - **Compare:** total components, delivery, warranty, compliance, reasons, rank, and score with policy version, plus the human decision.
  - **Audit:** events and metrics.
- **Wording:** the page never shows "Awarded", "PO sent" or "Paid".

### Evidence (actually executed)

| Check | Result |
|---|---|
| `python -m pytest tests/test_vendor_sourcing.py tests/test_vendor_rfq.py -q` | passed (the 2 baseline failures are fixed) |
| All vendor test files + UI (`test_vendor_*`, `test_ui_vendor_sourcing.py`) | **143 passed**, including a `real_mongo` test for the unique partial index, concurrency and persistence across clients |
| Full suite `python -m pytest -q` | **1029 passed**, 0 failed (baseline 893 passed / 2 failed) |
| pyflakes on app/ui/tests/scripts/experiments, `git diff --check` | clean |
| Live API, real uvicorn and throwaway MongoDB | **40/40**: RBAC; catalog first; candidate onboarding with duplicate 409; audit events; RFQ that diverges from the ticket gives 422; RFQ 201; structured and duplicate quotes; PDF 415/422/413/>20 pages/201; no PDF bytes in Mongo; filename sanitized; download headers; user gets 403 on download; unverified quotes excluded; diff kept; a `declared` quote is not eligible even though it is cheaper; the verified quote is recommended with total 75,125.00; score version and hashes present; human decision closes the RFQ; quote after close and cancel of a closed RFQ give 409; no non-draft PO and no payment; metrics; after restart the RFQ, quotes and PDF persist and the partial index exists |
| Live UI, AppTest against the real API | **8/8**: banner; tabs; review queue; decision button disabled until confirmed; decision persisted; validated ticket offered; user and approver refused |
| CI / type checking | **Not run**: the repository has no CI workflow and no type-checker configuration |

### Known limitations and deferred work

- **Outbox, not a transaction:** the outbox uses single-document atomicity. No multi-document MongoDB transaction is used, so the app also works on standalone MongoDB.
- **Metrics are per process:** they reset on restart.
- **File storage is local only:** the PDF store is a local directory, and object storage remains a deployment concern.
- **Verification role:** admins verify quotes; a separate procurement-reviewer role is deferred.
- **Not implemented:** 2D (external discovery adapter) and 2E (supplier communication) need separate design and security review and explicit authorization.
- **Not production-ready:** don't describe this as production-ready on the strength of these tests alone. A reviewer still needs to approve it.
