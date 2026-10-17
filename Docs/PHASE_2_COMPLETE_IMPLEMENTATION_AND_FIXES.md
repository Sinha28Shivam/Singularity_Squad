# Phase 2 Completion Specification: Approval Safety + Policy RAG

**Project:** Singularity Squad — WorkplaceOps AI Request-to-Payment Copilot  
**Repository:** `Sinha28Shivam/Singularity_Squad`  
**Reviewed commit:** `5836cf8b427b254b9153d2534a3378610eb0d99f`  
**Purpose:** Implementation-ready scope, fixes, tests, and acceptance criteria for completing Phase 2.  
**Important:** This is a specification, not a claim that the listed work is already implemented.

## 1. Executive summary

The repository already contains a substantial approval lifecycle and a Policy RAG foundation: PDF ingestion, chunking, retrieval, citations, structured rate-card extraction, evidence snapshots, approval requests, and a Streamlit policy library. The approval implementation includes meaningful safeguards such as role checks, requester/approver separation, versioned snapshots, conditional decision updates, idempotency, expiry, and reconciliation flags.

The main remaining gap is the bridge between **retrieved policy documents** and **verified, executable policy rules that affect real ticket decisions**. Rate-card validation also needs to be connected to the normal ticket evaluation path. The project should not be declared complete just because individual modules exist.

### Current status assessment

| Area | Status based on source review | Required action |
|---|---|---|
| MongoDB ticket/audit persistence | Implemented in code; runtime proof required | Run full tests and restart tests |
| Approval lifecycle | Largely implemented | Verify auth, races, stale snapshots, expiry, reconciliation |
| PDF ingestion and retrieval | Foundation implemented | Verify citations, versions, degradation, OCR handling |
| Fixed topic-based policy checks | Implemented but not fully dynamic | Add generic verified policy rules and interpreter |
| Rate-card extraction | Implemented for supported layouts | Add fallbacks, confidence handling, provenance checks |
| Automatic rate validation in ticket flow | Integration gap | Pass quote data into evaluation and snapshot |
| OCR for scanned PDFs | Incomplete in reviewed ingestion path | Add OCR or explicit review-only workflow |
| CI/test evidence | Not independently confirmed | Run tests and publish CI result for exact commit |

## 2. Non-negotiable safety boundary

The system may intake requests, retrieve policies, evaluate structured facts, draft POs, identify risks, request human review, record decisions, and maintain audit/evidence snapshots.

It must **not** place orders, send POs to vendors, call external procurement systems, or initiate payments in this phase.

- A human approval is an internal authorization record only.
- An LLM response, Critic result, `final_status == "pass"`, or policy summary is never human approval.
- A request submitter cannot approve their own request.
- Client-provided usernames/roles/headers cannot establish authorization.
- Unverified rules and unverified rates are not authoritative.
- Missing evidence, retrieval failure, or parsing failure must never silently become a pass.
- The PO's operational `status` remains `draft` after approval; store `approval_status` separately.
- A failed database write must never be reported as a successful persisted decision.

## 3. Target architecture

```text
Request / Ticket
    |
    v
Existing workflow agents -> structured WorkflowResult
    |                                 |
    v                                 v
Policy RAG                      Rate-card validation
retrieve current evidence       vendor + item + quantity + quote
    |                                 |
    +---------------+-----------------+
                    v
          Deterministic policy evaluation
        verified rules + typed structured facts
                    |
                    v
             Safety/decision gate
       hard blocks / missing info / review
                    |
                    v
         Persist evidence + snapshot hash
                    |
                    v
            Versioned approval request
                    |
                    v
         Authenticated human decision
                    |
                    v
        Atomic state transition + audit
                    |
                    v
             Streamlit reviewer UI

External order/vendor/payment execution: OUT OF SCOPE
```

Keep concerns separated: authentication is server-owned; ticket state is stored in MongoDB; approval state is controlled by conditional repository updates; rule authority comes from verified/versioned rules; rate authority comes from verified current rate-card rows; the snapshot records exactly what the reviewer saw.

Suggested responsibilities (reuse existing modules where they already exist):

- `app/approvals/`: authorization, policy, schemas, lifecycle service, errors.
- `app/policy_rag/`: ingestion, retrieval, embeddings, evaluation, rate cards, rule extraction, rule validation, generic rule engine.
- `app/repositories/`: approval, policy document/chunk, evidence snapshot, rate card, and new policy-rule persistence.
- `app/routers/`: policy document/search APIs and optional rule-review APIs.
- `ui/pages_portal/`: approval queue and policy library/rule review.
- `tests/`: authorization, lifecycle, retrieval, rule engine, rate validation, end-to-end acceptance.

These are target responsibilities, not a requirement to rename stable code or create duplicate modules.

## 4. Approval Safety requirements

### 4.1 Roles and trusted identity

Roles:

- `user`: submit and view own requests.
- `approver`: decide requests for which their role is eligible.
- `admin`: manage tickets, documents, configuration, and operations; admin access does not automatically bypass separation of duties.
- `system`: run background lifecycle actions such as expiry/reconciliation; never a human approver.

Resolve identity from a trusted server-side session, signed token, or configured authentication adapter. Never trust an actor/role from the request body or a browser-controlled `X-User-Role` header. If a demo identity mechanism exists, it must be explicitly enabled by server configuration and clearly documented as demo-only. Protected operations must fail closed if trusted auth is unavailable.

Authorization must be enforced in the service layer, not only in route dependencies or Streamlit UI.

### 4.2 Approval policy

Keep the approval decision deterministic and versioned. It may consider:

- whether a draft PO exists;
- amount and high-risk thresholds;
- workflow warnings;
- applicable verified policy findings;
- rate-card mismatch, unknown, conflict, or unverified rate;
- missing evidence;
- hard-block findings;
- clarification requirement;
- required approver role and expiry.

Policy evaluation may add review reasons, raise risk, mark a result unknown, or apply a hard block when a verified rule explicitly requires it. It must not clear an existing hard block, convert unknown evidence into pass, or approve anything.

### 4.3 Lifecycle states

Supported states:

- `pending`
- `approved`
- `rejected`
- `changes_requested`
- `expired`
- `superseded`

Allowed transitions are only from `pending` to one of the other states. Terminal states are immutable. A material ticket edit, new workflow run, PO change, or changed evidence must not reuse a previous approval. It must supersede/create a new version according to the lifecycle contract.

Do not overwrite the workflow's `final_status` to represent human approval. Keep workflow result and human approval state separate.

### 4.4 Snapshot and hash

Before creating an approval request, create a canonical review snapshot containing:

- ticket ID and run/request version;
- structured request fields;
- vendor identity;
- PO ID, line items, quantities, unit prices, taxes, total, currency;
- workflow status and reconciliation status where relevant;
- policy findings and their statuses;
- verified rule IDs and versions;
- source document IDs, content hashes, versions, pages, sections, and evidence IDs;
- rate-card result, matched rate ID, source version/page, and comparison facts;
- missing evidence and conflicts;
- approval-policy version and rule-engine version.

Canonicalize JSON with stable key ordering and stable date/number serialization, then compute SHA-256. Store the exact snapshot and hash. Bind every decision to that snapshot and request version. Do not put secrets or unnecessary raw private text into the public snapshot.

### 4.5 Decision behavior

Supported actions: `approve`, `reject`, `request_changes`.

The request body may contain action, comment, idempotency key, request version, and snapshot hash. Actor identity and role must be server-resolved.

- Reject and request-changes require a non-empty comment.
- Validate role, self-approval, status, expiry, version, snapshot hash, and idempotency.
- Use an atomic conditional update so only one concurrent decision wins.
- Same idempotency key + same actor/payload returns the prior result.
- Reusing a key with a different payload returns `409 Conflict`.
- Stale hash/version or terminal state returns `409 Conflict`.
- Do not report success before the authoritative decision write succeeds.
- If secondary ticket/PO/audit writes fail, mark reconciliation durably and expose it to admins.

Typical HTTP semantics:

| Status | Meaning |
|---|---|
| `200` | Read or idempotent replay succeeded |
| `201` | New resource created |
| `400` | Malformed request |
| `401` | Missing/invalid authentication |
| `403` | Authenticated but unauthorized |
| `404` | Not found or intentionally concealed unauthorized resource |
| `409` | Stale version, terminal state, race, or conflicting idempotency key |
| `422` | Schema/domain validation error |
| `503` | Required database/auth/policy dependency unavailable |

### 4.6 Expiry and superseding

- Enforce expiry server-side, not only in the UI.
- Expiry is idempotent and never auto-approves.
- A new workflow run supersedes the old pending request before the new version becomes actionable.
- Ticket edits invalidate the old snapshot.
- Expired/superseded requests remain in history.
- Reconciliation operations must be idempotent and auditable.

### 4.7 Draft-only guarantee

- Keep PO `status == "draft"` even after approval.
- Store internal approval in a separate field.
- Do not add outbound order/vendor/payment calls.
- Add tests that intercept outbound HTTP/client calls and prove no external execution occurs.
- UI must say: **“Internal approval recorded. This does not send the PO to a vendor or initiate payment.”**

## 5. Dynamic policy rules — key missing capability

### 5.1 Required outcome

A new uploaded policy PDF must be able to introduce a new candidate rule without adding a new Python branch for that specific clause. The system must retrieve the source passage, propose a normalized rule, require authorized human verification, and then evaluate applicable requests using a generic interpreter.

Example only: a newly uploaded document might state that events above a certain attendee threshold require an emergency plan. The system should extract the candidate, cite its source page, require verification, then evaluate the verified rule. This example is illustrative, not a claim about existing policy content.

### 5.2 Rule lifecycle

```text
PDF -> text/pages -> chunks/index -> candidate rule extraction
    -> schema/source validation -> authorized human verification
    -> versioned verified rule -> deterministic evaluation
    -> findings + citations -> approval snapshot
```

Rule states:

- `candidate`
- `needs_review`
- `verified`
- `rejected`
- `superseded`
- `disabled`

Only verified, active, current rules from ready documents may drive authoritative deterministic decisions. Candidates can be shown for review but must not silently become active rules.

### 5.3 Rule schema

Add a strict Pydantic model with equivalent fields to:

```python
class PolicyRule(BaseModel):
    rule_id: str
    logical_key: str
    version: int
    status: Literal[
        "candidate", "needs_review", "verified",
        "rejected", "superseded", "disabled"
    ]
    title: str
    description: str
    category: str
    severity: Literal["info", "review_required", "hard_block"]
    effect: Literal["require_review", "hard_block", "informational"]
    applicability: dict
    conditions: list[dict]
    required_actions: list[str]
    source_document_id: str
    source_content_hash: str
    source_version: int
    source_version_label: str | None = None
    source_page: int
    source_section: str | None = None
    source_excerpt: str
    evidence_ids: list[str]
    extraction_method: str
    extraction_confidence: Literal["low", "medium", "high"]
    verified_by: str | None = None
    verified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
```

Adapt the exact model to existing conventions, but preserve versioning, state, provenance, bounded logic, and verification metadata.

### 5.4 Constrained rule language

Never execute arbitrary Python, `eval`, `exec`, SQL, or executable strings supplied by an LLM or document. Use an allowlisted field/operator DSL.

Example normalized rule:

```json
{
  "rule_id": "RULE-EVENT-SAFETY-001",
  "category": "safety",
  "severity": "review_required",
  "effect": "require_review",
  "applicability": {"event_category": ["team_event", "town_hall"]},
  "conditions": [
    {
      "field": "request.attendees",
      "operator": "greater_than_or_equal",
      "value": 150,
      "value_type": "integer"
    }
  ],
  "required_actions": ["Provide a documented emergency and first-aid plan."],
  "source": {"document_id": "doc_12345678", "version": 2, "page": 4}
}
```

This is an illustrative schema example, not an actual company rule.

Supported operators should be limited and typed, such as `equals`, `not_equals`, `greater_than`, `greater_than_or_equal`, `less_than`, `less_than_or_equal`, `in`, `not_in`, `contains`, `exists`, `missing`, `date_before`, `date_after`, `all`, and `any`.

Validate field paths against known structured request/workflow fields. Reject unknown fields and operators. Rules cannot read secrets, arbitrary MongoDB paths, files, or execute code.

### 5.5 Generic interpreter semantics

For each request:

1. Load applicable verified rules and current document versions.
2. Filter by applicability: date, category, vendor, location, service, and configured scope.
3. Evaluate conditions against typed structured facts.
4. Produce `pass`, `fail`, `unknown`, `conflict`, or `not_applicable` findings.
5. Attach rule ID/version and evidence IDs.
6. Missing/malformed input yields `unknown`, not pass; never treat missing numeric data as zero.
7. Missing evidence is not proof of compliance.
8. Apply a hard block only when an explicitly verified rule requires it.
9. Conflicting current rules require review unless a documented precedence rule exists.
10. Bind rule versions and rule-engine version to the evaluation hash and approval snapshot.

The generic engine must handle new threshold rules without a new code branch for each threshold.

### 5.6 Conflicts and rule-review UI

- Do not automatically choose between contradictory current verified rules unless policy defines precedence.
- Superseded documents/rules must not silently affect new decisions.
- A new document version must not rewrite old snapshots.
- Rule review UI shows title, conditions, effect, severity, source excerpt/page/version, confidence, validation errors, and history.
- Only authorized reviewers may verify/reject/disable/supersede rules.
- A verified rule's conditions/effect cannot be silently edited; changes create a new version and audit event.

## 6. RAG retrieval, citations, and reliability

### 6.1 Evidence quality

- Filter by document readiness, effective dates, version, document type, vendor, and scope before ranking.
- Return source document ID, version, content hash, page, section, chunk ID, excerpt, and evidence ID.
- Ensure every citation refers to stored source text actually retrieved.
- Do not let an LLM invent page numbers, quotes, source titles, or document IDs.
- Insufficient evidence must be explicit.
- Add retrieval tests with expected document/page for representative queries.
- Preserve the exact document versions used for each historical decision.

### 6.2 Embedding degradation

The local hashed-vector fallback can support deterministic offline retrieval, but it is not equivalent to a semantic embedding model. Report the actual mode instead of labelling every run `hybrid`.

Expose at least:

- `retrieval_mode`;
- `embedding_provider`;
- `embedding_model`;
- `embedding_available`;
- `degradation_reason`;
- `retrieval_config_version`.

If the intended provider is unavailable, use an explicitly approved fallback or require review for mandatory policy checks. Never silently pass a required check because retrieval degraded.

### 6.3 Prompt-injection resistance

Treat PDF content and retrieved passages as untrusted data. They cannot override system behavior, call tools, set roles, approve requests, disable audit, reveal secrets, or modify configuration.

LLMs may summarize evidence or propose rule candidates. Deterministic validation and authorized human verification control what becomes active. Add a regression test with a malicious instruction embedded in a PDF.

### 6.4 LLM summary

The summary is explanatory only. It must not alter deterministic findings, totals, risk, approval requirements, or hard-block state. If the LLM is unavailable, structured findings remain usable. Never derive authoritative numeric thresholds from a generated summary.

## 7. Rate-card extraction and validation

### 7.1 Extraction

Current parsing supports particular document layouts. Preserve the deterministic parser, but treat all extracted rows as candidates until verified.

Every row should carry:

- rate ID;
- document ID/content hash/version/source page;
- vendor key and normalized vendor identity;
- item/service code and description;
- decimal unit rate, currency, and unit;
- quantity bounds and effective dates when stated;
- tax treatment only if supported by source;
- extraction method/confidence;
- verification status, verifier, and timestamp.

Do not invent tax, units, effective dates, or aliases. Ambiguous/low-confidence rows remain `needs_review`. Identical uploads are idempotent. Changed content must not silently inherit an old verification decision.

Add a table-extraction or LLM-assisted strategy for other PDF layouts, with strict schema validation and source citations. Keep extraction separate from verification.

### 7.2 Deterministic quote validation

Use `Decimal`, never binary floating point, for money. Match by vendor, item/service code, currency, unit, quantity bounds, effective date, verification state, and current ready document version.

Statuses: `pass`, `fail`, `unknown`, `needs_review`, `conflict`.

- No verified applicable rate: `unknown`/`needs_review`.
- Multiple applicable verified rows without precedence: `conflict`.
- Quote above configured tolerance: `fail`.
- A below-rate quote must follow an explicitly documented policy; do not assume it is automatically a violation.
- Do not invent tax treatment.
- Store quoted/expected rates and totals, quantity, currency, difference, source rate ID, document version, and page.
- Document whether tolerance applies per unit or total and use it consistently.
- Validate currency, unit, quantity, and dates before comparison.

### 7.3 Critical integration fix

The reviewed ticket path calls `evaluate_and_snapshot(ticket, workflow_result)` and that helper calls `evaluate_ticket()` without a `rate_quote`. Thus, the separate rate-validation API does not by itself prove automatic rate checking in the normal ticket flow.

Implement:

1. Extract vendor and line-item facts from the structured workflow result.
2. Determine whether a verified rate-card rule applies.
3. Build `RateQuote` objects only from known structured fields; never guess missing values.
4. Validate every applicable line item.
5. Merge validation results into policy findings and reason codes.
6. Include matched rate IDs, source versions/pages, and comparison facts in the snapshot.
7. Create the final evidence snapshot only after the rate result is available.
8. Require review for unknown, conflict, unverified, or failed results according to configured policy.
9. Never let a rate validation result auto-approve a ticket.

If required quote fields are absent, return unknown/review with the missing fields listed.

## 8. Evaluation-to-approval integration

Recommended sequence:

1. Validate structured request and workflow result.
2. Preserve blocked/clarification states.
3. Load current applicable verified policy rules.
4. Retrieve evidence and evaluate rules.
5. Validate rate cards when applicable.
6. Detect missing evidence and conflicts.
7. Build structured policy evaluation.
8. Merge findings into approval requirement with monotonic safety semantics.
9. Build canonical review snapshot and hash.
10. Persist evidence snapshot.
11. Create versioned approval request if required.
12. Update ticket state only after required writes succeed.
13. Return server-derived status.

Do not create an approvable request when policy evaluation failed or the evidence snapshot is incomplete. Preserve diagnostics without leaking stack traces to clients.

### Clarification + hard-block edge case

Review the policy merge path around `needs_clarification`. An early return must not hide a hard block or required review finding.

Expected behavior:

- Clarification remains visible.
- An independently applicable hard block remains visible and cannot be overridden.
- Findings are still stored and shown.
- Blocked requests do not expose ordinary approval actions.
- Tests cover clarification + review, clarification + hard block, and missing condition inputs.

## 9. Persistence, versioning, and MongoDB

Reuse existing collections if they already serve the purpose. Logical records include tickets, POs, approvals, audit, policy documents, chunks, evidence snapshots, rate-card rows, policy events, and a new `policy_rules` collection.

Ensure equivalent indexes exist:

- unique approval ID;
- unique `(ticket_id, request_version)`;
- approval queue `(status, created_at)`;
- requester history `(requester_id, created_at)`;
- unique document ID and content-hash lookup;
- logical key/version for policy documents;
- chunk lookup by document/version/page;
- unique rule ID/version and lookup by status/category/logical key;
- unique rate ID and rate lookup by vendor/item/verification/effective date;
- evidence snapshot lookup by ticket/evaluation version;
- audit lookup by ticket/sequence/time.

Test duplicate writes and concurrency, not just index creation.

MongoDB multi-collection writes are not inherently atomic. Keep the authoritative conditional approval transition and make secondary updates idempotent/repairable.

- Mark `reconciliation_required` durably on secondary failure.
- Keep enough information to retry/diagnose.
- Provide an admin-only reconciliation path or documented repair command.
- Never rewrite an original decision to hide a failure.
- Do not report success if the authoritative decision write failed.
- Historical snapshots keep their original document/rule/rate versions even after newer uploads.

## 10. API and UI requirements

### API

Retain existing routes where possible. Ensure API behavior supports approval creation/list/detail/decision, evidence access, audit access, policy upload/search, rule review, rate-card verification, and quote validation.

- Use server-resolved identity and enforce permissions in services.
- Sanitize errors; never expose stack traces, credentials, bearer tokens, or filesystem paths.
- Validate PDF signature, extension, size, page count, and metadata.
- Use server-generated storage names; prevent path traversal.
- Protect upload, verify, supersede, disable, and download operations.
- Prefer archival/superseding over deleting policy used in past decisions.
- Do not let a client edit a verified rule's effect/conditions without creating a new version and verification event.

### Streamlit requester view

Show server-derived status, workflow summary, clarification/approval state, redacted findings/citations, missing evidence/conflicts, and the draft-only notice. Never expose another user's tickets or unauthorized evidence.

### Approver queue

Show requester/version, risk, reasons, PO total/currency, vendor/line items, policy findings/citations, rate-card result/source, missing evidence, snapshot version/hash, expiry, and reconciliation warnings.

### Decision UX

- Confirm before submitting.
- Require comments where configured.
- Disable duplicate submissions while in flight.
- No optimistic success.
- On 409, refresh state and explain stale/already-decided request.
- On 503, show no success message.
- Refresh approval, ticket, PO, and audit after a decision.
- Distinguish PO `status` from `approval_status`.
- If evidence is missing or cannot open, show the failure rather than hiding the citation.

### Policy library

Show document status, version, effective dates, extraction/index/OCR status, retrieval degradation, candidate rules, and rates needing verification. `candidate`, `needs_review`, `needs_ocr`, and failed documents are not authoritative evidence.

## 11. Audit and observability

Audit at least:

- document upload, duplicate, extraction, indexing, OCR-needed, failure, superseding;
- rule candidate creation/edit/verify/reject/disable/supersede;
- rate extraction/verification/rejection/conflict;
- policy evaluation, retrieval degradation, insufficient evidence, rule conflict;
- approval requested/decision attempt/accepted/rejected/expiry/superseding;
- unauthorized/self-approval attempts;
- reconciliation required/completed;
- config changes that affect approval thresholds, rule precedence, or retrieval.

Include timestamp, actor ID/role, action, resource ID, version, outcome, and safe reason code. Never log secrets, tokens, authorization headers, or entire sensitive documents. Durable audit is required for compliance-relevant events; application logger output is not a substitute.

## 12. Failure handling

| Failure | Expected behavior |
|---|---|
| MongoDB unavailable | Fail; no false success |
| Invalid approval policy | Fail closed; no approval request |
| PDF extraction failure | Safe failure status; do not use as evidence |
| Scanned PDF without OCR | `needs_ocr`; not authoritative |
| Embedding provider unavailable | Explicit degraded mode; configured fail-safe |
| No relevant policy evidence | Unknown/insufficient evidence and review |
| Conflicting rules | Conflict; no silent precedence |
| Unverified rate | Unknown/needs review |
| Multiple matching rates | Conflict unless precedence is configured |
| LLM summary unavailable | Keep deterministic findings |
| Concurrent decision | One atomic transition wins; other receives conflict/replay |
| Secondary write fails | Durable reconciliation marker |
| Stale snapshot | Reject decision with conflict |
| Auth provider unavailable | Fail closed for protected operations |
| Unknown rule field/operator | Reject candidate |
| Prompt injection in PDF | Treat as untrusted content |

## 13. Mandatory test plan

### 13.1 Approval authorization and lifecycle

- Unauthenticated caller cannot access protected approvals.
- User cannot decide.
- Requester cannot self-approve.
- Wrong role cannot decide.
- Admin cannot bypass the dedicated approval path through legacy routes.
- Client-supplied actor/role cannot override trusted identity.
- Unauthorized access does not reveal another user's sensitive details.
- Valid approve/reject/request-changes behavior.
- Required comments enforced.
- Same idempotency key/payload replays; different payload conflicts.
- Concurrent approve/reject yields exactly one transition.
- Expired/superseded/terminal requests cannot be decided.
- Stale version/hash returns conflict.
- New workflow run and ticket edit invalidate old pending approvals.
- PO stays `draft` after approval.
- No external execution calls.
- Approval and audit survive restart.
- Secondary failures trigger reconciliation and can be repaired idempotently.

### 13.2 Dynamic policy-rule tests — required

- Upload produces candidate rule with exact source page/excerpt.
- Invalid schema/unsupported operator is rejected.
- Candidate does not affect authoritative decisions before verification.
- Only authorized reviewer can verify.
- A newly verified threshold works without adding a Python condition.
- Missing facts return unknown, not pass.
- Missing evidence is not compliance.
- Verified hard block remains blocked.
- Review rule triggers review.
- Conflicting rules return conflict.
- Superseded rule not used for new decisions.
- Old snapshots retain old rule/document versions.
- PDF prompt-injection does not change tools, role, config, or approval state.
- LLM proposal cannot self-verify.
- Clarification plus hard block preserves the block.

### 13.3 RAG tests

- Known query retrieves expected source document/page.
- Citation excerpt matches stored source.
- Unsupported query returns insufficient evidence.
- Superseded/expired documents are filtered correctly.
- Duplicate upload is idempotent; new version is indexed separately.
- Embedding failure is labelled degraded.
- Provider/model used is visible.
- Hashed-vector fallback is not labelled semantic embeddings.
- Vendor/type/date filters work.
- Prompt-injection is treated as evidence, not instruction.
- Requester-safe redaction omits private excerpts/hashes/scores where required.

### 13.4 Rate-card tests

- Extracted rows default to `needs_review`.
- Low-confidence rows cannot be used as verified rates.
- Exact verified match passes.
- Above-tolerance quote fails.
- Below-rate behavior follows documented policy.
- Currency/unit/quantity/date mismatch requires review.
- Rejected rows ignored.
- Multiple verified matches conflict.
- Unknown vendor/item returns unknown.
- Tax is not invented.
- Decimal arithmetic and rounding are deterministic.
- Changed document does not silently inherit verification.
- Every row cites exact source version/page.
- Normal ticket-run automatically invokes rate validation when structured quote exists.
- Mismatch is visible in approval UI/snapshot.
- Missing quote fields never produce false pass.

### 13.5 End-to-end acceptance tests — required

**A. New policy rule**

1. Upload a synthetic policy containing a threshold absent from the code.
2. Confirm a candidate rule with exact page citation is created.
3. Confirm it is inactive before verification.
4. Verify it as an authorized reviewer.
5. Submit one request below the threshold and one at/above it.
6. Confirm the verified rule changes the result without changing Python code or prompt.
7. Confirm the approval snapshot includes rule version and document hash.

**B. Rate-card mismatch**

1. Upload a synthetic rate card and verify a row.
2. Submit a ticket with an above-tolerance quote.
3. Confirm normal ticket execution automatically validates the quote.
4. Confirm the mismatch and evidence reach the approval snapshot/queue.
5. Confirm no external order/vendor/payment action occurs.

**C. Degraded retrieval**

1. Make the configured embedding provider unavailable.
2. Submit a request requiring a policy check.
3. Confirm mode and degradation reason are visible.
4. Confirm the system does not silently pass the mandatory check.
5. Confirm the UI explains uncertainty/review state.

**D. Concurrent approval**

1. Create a pending request.
2. Submit approve and reject concurrently with different idempotency keys.
3. Confirm exactly one transition wins.
4. Confirm the other receives conflict.
5. Confirm projections are consistent or visibly flagged for reconciliation.

## 14. Test execution and CI

Run the repository's documented test command; the expected baseline is:

```bash
python -m pytest -q
```

Also run lint/type checks if configured, real-Mongo integration tests against a disposable database, separate-process restart tests, UI tests, ingestion/retrieval acceptance tests, rate-card workflow tests, no-external-execution tests, BOM/encoding scan, and a secret scan.

Add CI that installs declared dependencies and runs the supported test suite. Integration tests should use a MongoDB service container or a separately required integration job. A skipped integration test is not proof of integration success. Do not claim tests passed unless output from the exact reviewed commit confirms it.

## 15. Environment and configuration

Document configuration for MongoDB URI/database, trusted auth/demo mode, policy store directory, upload/page limits, embedding provider/model, credentials via environment variables, retrieval settings, rule activation/verification, approval policy/version, rate tolerance/currency, OCR if implemented, and logging.

- Never commit secrets.
- Use synthetic/anonymized data for tests.
- Fail closed if required protected-operation config is missing.
- Separate demo defaults from production-like config.
- Version/audit changes that affect approval outcomes.
- Do not expose internal filesystem paths in API responses.

## 16. Implementation sequence

### 2A — Baseline and contract freeze
Run existing tests, capture failures, confirm API/Pydantic contracts, document current configuration, and add tests for clarification + hard block and degraded retrieval.

**Exit:** repeatable baseline with known failures.

### 2B — Approval hardening
Verify trusted actor resolution, role checks, self-approval, idempotency, concurrent decisions, expiry, superseding, snapshot coverage, reconciliation, and draft-only behavior.

**Exit:** approval and no-external-execution tests pass.

### 2C — Rule model and review
Add strict candidate/verified rule schema, repository/indexes, source-cited extraction, review API/UI, verify/reject/disable/supersede lifecycle, and audit.

**Exit:** candidates cannot affect authoritative decisions until verified.

### 2D — Generic rule interpreter
Define allowed fields/operators, typed condition evaluation, unknown/missing semantics, conflict behavior, evidence binding, and rule version snapshots.

**Exit:** new verified threshold rule works without per-rule Python logic.

### 2E — Rate-card integration
Normalize quote data from workflow result, call deterministic validation in normal ticket path, map results to findings/reasons, bind provenance, add extraction fallbacks, and test mismatch end to end.

**Exit:** rate mismatches/unknowns are automatically visible in approval flow.

### 2F — RAG reliability and OCR
Expose actual provider/mode, add expected-source retrieval tests, implement OCR or explicit review path, test effective dates/superseding/prompt injection, and verify redaction.

**Exit:** retrieval quality and degradation are visible and fail safely.

### 2G — UI, docs, CI, release verification
Complete reviewer views, candidate-rule review, conflict/degraded states, README/flow docs, CI, MongoDB restart tests, secret scan, and final acceptance run.

**Exit:** demo acceptance checklist passes on final commit.

## 17. Suggested work split for 3–4 people

| Workstream | Responsibilities | Dependencies |
|---|---|---|
| A. Approval safety | Auth, state machine, races, idempotency, expiry, reconciliation | Existing approval schema/repository |
| B. Policy rules | Candidate schema, citations, review, generic interpreter | Document ingestion and structured request facts |
| C. Rate cards | Extraction robustness, verification, quote normalization, workflow integration | WorkflowResult/PO contract |
| D. RAG quality/UI/CI | Retrieval evaluation, degradation, OCR path, reviewer UX, CI/acceptance | Stable API contracts |

Agree on contracts first. One person should own integration and final acceptance. Avoid simultaneous uncoordinated edits to the central evaluator.

## 18. Demo acceptance checklist

### Approval
- [ ] User submits a ticket and workflow creates a draft PO.
- [ ] Ticket becomes pending approval when required.
- [ ] Requester cannot approve their own ticket.
- [ ] Unauthorized role cannot decide.
- [ ] Approver sees exact snapshot, policy reasons, and citations.
- [ ] Approve/reject/request-changes works with comment rules.
- [ ] Stale/duplicate decisions cannot mutate final state.
- [ ] Approval survives restart and audit shows actor/time.
- [ ] PO remains draft; no external order or payment occurs.

### RAG
- [ ] Multiple PDFs can be uploaded and versioned.
- [ ] Duplicate content is idempotent.
- [ ] Citations point to correct source pages.
- [ ] Unsupported questions return insufficient evidence.
- [ ] Scanned/unreadable PDFs are not treated as ready evidence.
- [ ] Embedding degradation is visible.
- [ ] Prompt injection in a PDF cannot override safety.

### Dynamic rules
- [ ] New policy creates candidate rule with source page.
- [ ] Candidate is inactive until authorized verification.
- [ ] Generic interpreter applies verified rule without per-rule code changes.
- [ ] Snapshot contains rule/document version.
- [ ] Conflicts/missing facts produce review, not silent pass.

### Rate cards
- [ ] Extracted rows need verification.
- [ ] Verified match passes and mismatch is detected.
- [ ] Unknown/conflicting match does not silently pass.
- [ ] Normal ticket flow invokes rate validation automatically.
- [ ] Snapshot and approval UI contain result and provenance.

## 19. Definition of done

Phase 2 is complete only when:

1. Approval decisions are authenticated, authorized, version-bound, idempotent, and concurrency-safe.
2. Self-approval is prevented.
3. LLM output cannot grant approval or clear hard blocks.
4. Approval/audit survive restart; secondary failures are visible and repairable.
5. PO stays draft and no external procurement/payment execution exists.
6. Policy documents and rules are versioned and provenance-preserving.
7. Candidate rules require authorized verification.
8. A generic constrained rule engine applies new verified rules without per-rule Python branches.
9. Unknown/missing/conflicting/degraded evidence cannot silently pass.
10. Rate-card validation is integrated into the normal ticket workflow.
11. Rate validation uses verified current entries and deterministic decimal arithmetic.
12. Snapshot includes exact rule, document, and rate versions used.
13. Scanned PDFs use OCR or remain explicitly non-authoritative.
14. Prompt-injection tests pass.
15. Full unit/integration suites pass on the exact reviewed commit.
16. CI evidence exists, including MongoDB integration tests.
17. Final diff is checked for secrets, executable untrusted rules, and external action calls.
18. README/flow docs accurately distinguish implemented, partial, and unverified features.

## 20. Next implementation PR

Keep the first PR narrow:

1. Add a regression test for clarification + hard-block merging.
2. Add a test proving ticket execution calls rate validation when quote data exists.
3. Add a test proving missing quote data returns unknown/review, not pass.
4. Add strict policy-rule schema and repository contract, without activating candidates.
5. Add one end-to-end synthetic test for a newly verified threshold rule.
6. Update documentation to distinguish existing fixed-topic evaluation from the dynamic rule engine.

Do not merge the dynamic engine until citations, verification, unknown/conflict behavior, and snapshot binding are tested.

## 21. Final engineering verdict

The latest code has a meaningful Phase 2 foundation. The main remaining work is to connect policy evidence and rate-card validation reliably to the ticket-to-approval path and introduce a controlled generic policy-rule engine.

Prioritize correctness, provenance, and safe uncertainty over adding more agents. The system should explain which verified rule applied, which document version/page supports it, what facts triggered it, and why human review is required. Retrieval alone does not prove compliance, and internal approval does not execute a purchase.

**This file is the completion specification and acceptance contract, not proof that every listed item is already implemented.**
