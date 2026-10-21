# WorkplaceOps AI — Master Branch Review & Vendor Discovery/RFQ Implementation Plan

**Repository:** `Sinha28Shivam/Singularity_Squad`  
**Reviewed branch:** `master`  
**Review date:** 21 October 2026  
**Latest relevant merge:** PR [#2 — Vendor discovery, onboarding, RFQ and quote comparison](https://github.com/Sinha28Shivam/Singularity_Squad/pull/2)  
**Merge commit:** [`4ea84c7`](https://github.com/Sinha28Shivam/Singularity_Squad/commit/4ea84c7fad00b0b987d1f41f843cee80a7fc07c5)

---

## 1. Executive summary

WorkplaceOps AI is a request-to-payment copilot for procurement workflows. The current system accepts a purchase/event request, checks required information and policy, matches vendors, calculates a **draft purchase order**, optionally reconciles an invoice, records audit information, and requires human approval before an internal authorization is recorded.

The latest merge adds the first backend slice of **Phase 2 — Vendor Discovery and RFQ**:

1. Search the existing vendor catalog before considering any external discovery.
2. Register new vendor candidates without automatically approving them.
3. Review and approve/reject/suspend candidates with audit events.
4. Create an RFQ tied to a validated, persisted workflow ticket.
5. Accept structured quotations or PDF submissions.
6. Keep extracted PDF values unverified until a human reviews the commercial terms.
7. Compare verified quotes by total cost, delivery, warranty, compliance and budget.
8. Keep recommendations advisory: no supplier outreach, contract award, PO dispatch, or real payment is performed.

**Important status distinction:** the Phase 2 code is present on `master` after PR #2 merged. That does not, by itself, prove the test suite has passed or that the feature is ready for production. The merge PR explicitly stated that tests had not been run in the implementation environment.

---

## 2. What the current system already does

The current repository has more than a basic agent demo. The main capabilities visible in `README.md` and the application structure are:

- **Role-based workflow:** users submit and track their own tickets; admins run workflows and manage the queue; separately configured approvers decide approval requests.
- **Specialist-agent pipeline:** intake → policy advisor/RAG → vendor matcher → procurement → billing/invoice reconciliation → critic → human approval.
- **Persistence:** MongoDB repositories store tickets, draft purchase orders, audit entries, users, sessions and other workflow records.
- **Policy document RAG:** policy PDFs are ingested and retrieved with citations; rate-card evidence is intended to be reviewed and checked deterministically.
- **Dynamic policy rules:** rule candidates have a review/approval lifecycle, and active rule-set versions are used for deterministic evaluation.
- **Payment simulation:** payment instructions are simulated only, guarded by eligibility checks, idempotency and audit records. There is no real bank/gateway transfer.
- **Parallel analysis:** independent policy evaluation and vendor matching run concurrently; the rest of the workflow stays downstream of that join. The repository documents the LangGraph experiment as isolated and not used by the application.
- **Vendor sourcing (new):** internal-first vendor search, candidate onboarding, RFQ records, quotation intake, PDF text extraction, manual verification and deterministic comparison.

The application should continue to treat LLM output as untrusted language-processing output. Arithmetic, eligibility gates and final approval controls must remain deterministic and enforced server-side.

---

## 3. Phase 2 implementation currently present on `master`

### 3.1 Vendor discovery and candidate registry

Relevant files:

- `app/vendor_sourcing/schemas.py`
- `app/vendor_sourcing/repository.py`
- `app/vendor_sourcing/service.py`
- `app/routers/vendor_sourcing.py`
- `app/db/indexes.py`

Current behavior:

- Search the existing synthetic `data/vendors.json` catalog first.
- Search the persistent candidate registry as well.
- External discovery is explicitly disabled; search responses communicate that no external search was run.
- Candidate submissions start as `pending_review`.
- Candidate status transitions are explicit: pending review can be approved, rejected or suspended; approved candidates can be suspended; suspended candidates require an explicit approval transition to be reinstated.
- Candidate creation and decisions produce audit events.
- Candidate creation checks for an already-active candidate with the same normalized name.
- Vendor sourcing endpoints are admin-only through `require_admin`.

### 3.2 RFQ lifecycle

Relevant files:

- `app/vendor_sourcing/rfq_schemas.py`
- `app/vendor_sourcing/rfq_service.py`
- `app/routers/vendor_sourcing.py`
- `app/db/indexes.py`

The RFQ schema captures:

- Source request/ticket ID
- Service and quantity
- Budget ceiling and currency
- Required delivery date and location
- Warranty requirement
- Mandatory compliance requirements
- Timezone-aware response deadline
- Invited vendor/candidate IDs

RFQ creation checks that the source ticket exists, has a completed `pass`/`warning` workflow result, has no missing required fields, and contains a valid request snapshot. The RFQ service compares the RFQ's service, quantity, budget, date, currency and location against that snapshot. Only approved vendors/candidates can be selected.

The RFQ record stores a requirements snapshot hash and audit event. The current implementation does **not** send RFQs to suppliers; it records the sourcing request internally.

### 3.3 Quotation intake and review

The API supports:

- Structured quote submission
- PDF quote submission
- Human verification of normalized quote terms
- Quote comparison

Structured quote fields include supplier currency, unit price, tax, delivery fee, other fees, delivery date, warranty, compliance declarations and notes.

PDF handling currently:

- Requires `application/pdf`
- Limits request body to 5 MiB
- Checks the `%PDF-` signature
- Rejects encrypted PDFs
- Limits PDFs to 1–20 pages
- Extracts text and stores a SHA-256 hash
- Marks the quote `pending_verification`
- Does not treat extracted text as verified commercial data

The PDF is not automatically scored from its extracted text. An admin must submit normalized commercial terms through the verification endpoint. This is the correct initial safety boundary for PDF content, which must be treated as untrusted data.

### 3.4 Quote comparison

The current comparison logic:

- Uses Python `Decimal` for money calculations.
- Calculates total as `unit_price × quantity + tax + delivery fee + other fees`.
- Rejects quotes with a currency mismatch against the RFQ.
- Excludes quotations that have not been verified.
- Treats delivery date, minimum warranty, mandatory compliance and budget as eligibility gates.
- Ranks eligible quotes by lowest comparable total, then earliest delivery, then stable quote ID.
- Returns an advisory recommendation and explicitly states that no award or purchase order is created or sent.

This is a useful deterministic baseline. It is not yet a complete weighted procurement evaluation model.

---

## 4. API surface currently introduced

All routes are under `/api/vendor-sourcing` and currently require admin authorization.

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/vendors?q=&service=&status=&limit=` | Search internal catalog and candidate registry |
| `POST` | `/candidates` | Register a candidate as pending review |
| `POST` | `/candidates/{candidate_id}/decision` | Approve, reject or suspend a candidate with a reason |
| `POST` | `/rfqs` | Create an RFQ from validated requirements |
| `GET` | `/rfqs/{rfq_id}` | Read an RFQ and its quotations |
| `POST` | `/rfqs/{rfq_id}/quotes` | Submit structured quote |
| `POST` | `/rfqs/{rfq_id}/quotes/pdf` | Upload PDF quote |
| `POST` | `/rfqs/{rfq_id}/quotes/{quote_id}/verify` | Verify normalized commercial terms |
| `POST` | `/rfqs/{rfq_id}/compare` | Compare quotations deterministically |

---

## 5. Review findings and risks to address next

The following items should be treated as validation/hardening work before describing this as production-ready. They are based on reviewing the merged source; they are not claims that a live test run has reproduced each issue.

### P0 — Correctness and safety

1. **RFQ event creation is not transactional with the RFQ insert.** The RFQ may be inserted and then audit-event persistence may fail. The API can report an error even though the RFQ record exists. Similar partial-write risk exists around quote insert, audit event and quote count. Use MongoDB transactions where deployment supports them, or an outbox/state design with idempotent repair.
2. **PDF upload persistence stores raw PDF bytes in MongoDB.** The API response strips `pdf_bytes`, but the binary is still embedded in the document. Prefer private object/file storage, with metadata and hash in MongoDB; enforce retention and access controls.
3. **PDF upload endpoint uses raw request streaming rather than a multipart file contract.** It can work for a raw `application/pdf` body, but the API contract and client examples need to make that explicit. Add size, timeout, parser-resource and malformed-file tests.
4. **Quote verification needs stronger cross-field validation.** The reviewer can submit normalized fields that differ from the submitted structured quote. This may be intentional for correction, but the system should store submitted values and verified values separately and capture the difference/reason rather than silently overwriting the original.
5. **RFQ state transitions are incomplete.** The schema describes `draft`, `open`, `closed`, and `cancelled`, but only creation/read/submission/comparison are currently exposed. Add explicit close/cancel operations, enforce deadline/status checks consistently, and prevent post-close quote submissions.
6. **Compliance evidence is represented mainly as boolean declarations.** A boolean declaration is not proof of a valid document. Mandatory compliance should reference evidence metadata, expiry, verification state and reviewer, or remain `review_required`.
7. **Audit records are append-style but do not provide a transaction boundary.** Ensure important business state changes and their audit/outbox events cannot silently diverge.

### P1 — Workflow integration and procurement quality

8. **No Streamlit sourcing UI yet.** The API is present, but admins need pages for candidate review, RFQ creation, quote verification, comparison and audit history.
9. **No supplier communication adapter.** This is intentionally out of scope for this phase. A future outbound email/portal integration should require a separate security review, explicit approval and a delivery log.
10. **Comparison weights are not yet implemented.** The current result is an eligibility gate plus a deterministic lowest-cost tie-break. Delivery and warranty are gates, not configurable weighted scores. Add versioned weights only after a clear policy is agreed, and show raw facts next to the score.
11. **Warranty normalization is simplistic.** Warranty months are compared numerically, while warranty scope is stored as text. Define what “warranty” means for each service category and require manual review for ambiguous scope.
12. **Quote idempotency/duplicate handling needs a deliberate contract.** A unique `(rfq_id, vendor_id)` index prevents multiple quotes from one vendor per RFQ. Decide whether revisions are forbidden or represented as versioned quote submissions; do not silently overwrite.
13. **External vendor discovery remains disabled.** When later added, it should be a pluggable adapter with source provenance, retrieval timestamp, duplicate checks and human onboarding review—not a direct path to approved status.
14. **Validate endpoint behavior against the actual auth/RBAC setup.** These routes currently use `require_admin`, including quote verification. Decide whether verification belongs to admins, procurement reviewers, or a distinct compliance-review role.
15. **Check error mapping and failure recovery.** Expected validation conflicts should be distinguishable from persistence failures, and partial writes should be recoverable without duplicate candidates, quotes or audit events.

### P2 — Operability and maintainability

16. Add metrics for candidate onboarding, RFQ response rate, quote verification time, ineligible quote reasons, comparison outcomes and persistence failures.
17. Add retention rules for uploaded PDFs and extracted text, and avoid logging sensitive contact/compliance details.
18. Add structured audit metadata: actor, action, entity ID, previous state, new state, reason, timestamp and correlation/request ID.
19. Add indexes and pagination based on real query patterns. Avoid returning an unbounded number of candidates or quotes.
20. Add operational docs for MongoDB deployment, backup/restore, private file storage, migration/index creation and recovery from failed audit writes.

---

## 6. Recommended implementation roadmap

### Phase 2A — Stabilize the merged backend

**Goal:** make data and audit writes reliable before adding more features.

- [ ] Run the targeted and full test suite on the current `master`.
- [ ] Add integration tests against a real MongoDB instance for index creation and persistence/restart behavior.
- [ ] Decide transaction/outbox strategy for candidate, RFQ and quote state changes.
- [ ] Move PDF bytes to private file/object storage; keep metadata/hash in MongoDB.
- [ ] Preserve original submitted quote fields separately from verified fields.
- [ ] Add RFQ close/cancel endpoints and tests for all invalid state transitions.
- [ ] Add malformed, encrypted, oversized and over-page-limit PDF tests.
- [ ] Add duplicate-submission, concurrent-review and audit-failure tests.
- [ ] Add tests for mixed currencies, invalid decimals, budget boundary, delivery boundary, missing compliance and insufficient warranty.

**Exit criteria:** all targeted and full tests pass; persistence failures do not create ambiguous/undetected partial state; non-verified quotes can never be recommended.

### Phase 2B — Admin sourcing UI

- [ ] Vendor search page with internal catalog and candidate-registry separation.
- [ ] Candidate review page showing source/provenance, services, regions and evidence.
- [ ] Approve/reject/suspend flow requiring a reason and confirmation.
- [ ] RFQ creation form prefilled from a validated ticket snapshot.
- [ ] RFQ list/detail page with response deadline and status.
- [ ] Structured quote entry and PDF upload.
- [ ] PDF extracted-text review with editable normalized fields and explicit verification.
- [ ] Comparison table showing total components, delivery, warranty, compliance evidence, ineligibility reasons and ranking.
- [ ] Audit history and safe API error messages.

**Exit criteria:** an admin can complete a full simulated sourcing process locally without manually calling endpoints.

### Phase 2C — Better comparison model

- [ ] Define a category-specific scoring policy with business stakeholders.
- [ ] Keep mandatory compliance and deadline/budget checks as hard gates.
- [ ] Normalize warranty scope and delivery commitments.
- [ ] Add versioned scoring weights that sum to 1.0, with the active scoring version saved to each comparison.
- [ ] Display raw quote values beside any composite score.
- [ ] Make ties and disqualification reasons deterministic and explainable.
- [ ] Require human sign-off on the recommended supplier; do not auto-award.

**Exit criteria:** comparison results are reproducible from immutable RFQ/quote snapshots and explain every score and exclusion.

### Phase 2D — Controlled external discovery (future)

- [ ] Define allowed discovery sources and terms of use.
- [ ] Implement an adapter interface; keep external discovery off by default.
- [ ] Store provenance, source URL/reference, observation timestamp and normalized vendor facts.
- [ ] Deduplicate candidates against internal vendors and active candidates.
- [ ] Create only `pending_review` candidates.
- [ ] Require human verification of identity, compliance and commercial capability.
- [ ] Add rate limits, timeouts, allowlists and audit events.

**Exit criteria:** external discovery can suggest candidates but cannot approve vendors, send RFQs, award work, dispatch POs or pay suppliers.

### Phase 2E — Supplier communication (separate approval required)

Supplier outreach is deliberately excluded from the current implementation. Before any email or portal is built, define explicit authorization, recipient verification, message preview, approval, send idempotency, delivery status, cancellation, audit retention and incident handling. Do not enable it by default.

---

## 7. Suggested end-to-end workflow

```text
User submits purchase request
        |
        v
Existing WorkplaceOps workflow validates request and policy
        |
        v
Admin opens Vendor Sourcing for a completed, persisted ticket
        |
        v
Search internal approved vendors + candidate registry
        |
        +--> Suitable approved vendors exist
        |          |
        |          v
        |     Create RFQ snapshot for selected approved vendors
        |
        +--> Candidate needed
                   |
                   v
             Register candidate as pending_review
                   |
                   v
             Human onboarding review
                   |
                   +--> reject / request evidence
                   |
                   +--> approve candidate
                              |
                              v
                         Create RFQ
                              |
                              v
              Quotes arrive via structured entry or PDF upload
                              |
                              v
               PDF terms remain unverified until reviewed
                              |
                              v
               Human verifies normalized quote terms
                              |
                              v
            Deterministic cost + delivery + warranty + compliance gates
                              |
                              v
                  Explainable comparison report
                              |
                              v
                    Human selects/approves outcome
                              |
                              v
             Existing approval and draft-PO controls continue
                              |
                              v
                No real PO dispatch or payment occurs
```

---

## 8. Data model to keep

### Vendor candidate

- `candidate_id`
- Normalized vendor name
- Services and operating regions
- Contact reference (minimize personal data)
- Source type and provenance
- Compliance evidence metadata and expiry
- Status and status history
- Created/updated actor and timestamps
- Review decision, reason and evidence
- Audit events

### RFQ

- `rfq_id`
- Source ticket/request ID
- Immutable validated requirements snapshot + hash
- Service/scope, quantity, delivery date/location
- Currency and budget ceiling
- Mandatory compliance requirements
- Warranty requirement
- Response deadline and RFQ status
- Invited approved vendor IDs
- Scoring-policy version (when scoring is added)
- Created/updated actor and timestamps

### Quote

- `quote_id`, `rfq_id`, `vendor_id`
- Quote reference and submission type
- Original structured values or original PDF metadata/hash
- Extracted text and parser status
- Normalized values and currency
- Submitted values kept separately from verified values
- Verification status, reviewer, timestamp and note
- Duplicate/revision relationship
- Audit events

### Comparison

- RFQ snapshot hash
- Quote snapshot hashes
- Currency and itemized total components
- Delivery and warranty evaluation
- Compliance evidence and gate outcomes
- Eligibility result and explicit reasons
- Scoring policy version and component scores, if applicable
- Recommendation and human decision state

---

## 9. Test and release checklist

Run from the repository root in the project's configured Python environment:

```powershell
python -m pip install -r requirements.txt
python -m pytest tests/test_vendor_sourcing.py tests/test_vendor_rfq.py -q
python -m pytest -q
git diff --check
```

Also verify:

- [ ] The API starts with MongoDB configured and indexes are created.
- [ ] Admin-only endpoints reject unauthenticated and non-admin callers.
- [ ] RFQs cannot be created from missing, failed, or incomplete workflow tickets.
- [ ] RFQ requirements cannot diverge from the validated ticket snapshot.
- [ ] Unapproved vendors cannot receive RFQs or submit quotes.
- [ ] A quote is not eligible for comparison until human verification is complete.
- [ ] PDF upload limits are enforced before unbounded body accumulation.
- [ ] Mixed currencies never enter the same ranking.
- [ ] Missing mandatory compliance evidence fails closed.
- [ ] Duplicate and concurrent submissions/reviews behave predictably.
- [ ] Audit/persistence failures are visible and recoverable.
- [ ] Existing approval and payment-simulation safety tests remain green.
- [ ] No external supplier email, real award, PO dispatch or real payment is triggered.

**Release rule:** do not call the feature production-ready based solely on a successful merge. Record actual test output and any known limitations in the PR/release notes.

---

## 10. Non-goals and invariants

The following must remain true:

- The existing vendor catalog is read-only during discovery.
- Candidate submission never implies approval.
- External discovery is disabled unless separately implemented and authorized.
- PDF text is untrusted and never executes instructions or changes policy.
- Quote extraction does not imply verification.
- Comparisons are advisory and deterministic.
- Mandatory compliance, policy and approval gates cannot be bypassed by a score.
- The LLM cannot decide approval, invent compliance evidence, or override deterministic totals.
- Supplier outreach remains disabled until a separate security review and explicit authorization.
- Contract award, PO dispatch and payment remain manual/simulated; no real financial side effects are added by this phase.

---

## 11. Current overall assessment

**Architecture direction:** sound for an incremental MVP. The internal-first search, explicit onboarding review, persisted source-ticket requirement, PDF verification gate, Decimal totals and compliance/delivery gates are good foundations.

**Current maturity:** backend prototype / feature slice. The next priority should be reliability, test evidence, persistence/audit consistency, PDF storage, RFQ lifecycle completion and admin UI—not adding autonomous supplier outreach.

**Recommended next action:** complete Phase 2A stabilization and validation first, then build the admin UI on top of the stable API.
