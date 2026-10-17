# Phase 2 — Approval Safety
## Product behavior, security rules, architecture, API contract, and implementation plan

**Project:** WorkplaceOps AI — Request-to-Payment Copilot  
**Repository:** `Sinha28Shivam/Singularity_Squad`  
**Phase:** 2 — Approval Safety  
**Depends on:** Phase 1 MongoDB persistence  
**Primary goal:** Make human approval a real, server-enforced control rather than a UI label or workflow suggestion.

> **Non-negotiable safety boundary:** This is an approval and draft-preparation system. Approving a request does **not** place an order, send a purchase order to a vendor, approve an invoice for payment, transfer money, or execute any external side effect. Phase 2 records a human decision and updates the internal workflow state only.

---

## 1. Executive summary

Phase 1 makes workflow tickets, draft purchase orders (POs), and audit events persistent. Phase 2 adds a durable approval lifecycle with backend authorization, explicit decision records, separation of duties, replay protection, and a visible pending-approval experience.

The system must distinguish these three things:

1. **Workflow evaluation:** agents and deterministic policy code analyze the request and recommend an outcome.
2. **Approval decision:** an authenticated, authorized human approves or rejects a specific version of the evaluated request.
3. **Execution:** any real-world action such as placing an order or initiating payment. **Out of scope and disabled in Phase 2.**

No model or agent may grant approval, impersonate a human, override an authorization check, or convert a recommendation into an approval.

### Phase 2 success definition

- Every approval-required ticket has a persisted approval request with a clear reason and status.
- Only an authenticated user with the required role can decide it.
- A requester cannot approve their own request.
- A decision applies to the exact request/version reviewed.
- Repeated, stale, unauthorized, or conflicting decisions cannot silently change the outcome.
- Every request, decision, and rejected attempt that is safe to record is auditable.
- The API is authoritative; hiding or disabling a UI button is never the security control.
- No approval endpoint performs procurement, invoice-payment, or external vendor actions.

---

## 2. Scope

### In scope

- Deterministic approval requirements and policy reasons.
- Persistent approval-request records and decision history.
- User identity and role checks at the API layer.
- Maker-checker rule: the request creator cannot approve their own request.
- Approve, reject, and request-changes decisions.
- Pending, approved, rejected, expired, and superseded states.
- Approval request list/detail APIs.
- Streamlit UI for pending approvals and decision confirmation.
- Ticket detail showing the approval state, policy reasons, and decision history.
- Audit events, concurrency protection, idempotency, safe errors, and tests.
- Configurable approval policy for the synthetic hackathon data.

### Explicitly out of scope

- Sending POs to vendors or creating external orders.
- Paying invoices, initiating transfers, or integrating with banking/payment systems.
- Automatically approving requests with an LLM.
- Treating an agent's `Critic`, `pass`, or `final_status` as a human approval.
- Production identity-provider integration unless separately planned.
- Multi-person quorum or multi-level approval chains unless added as a later requirement.
- Changing the underlying procurement, billing, or policy calculations without tests.

---

## 3. Core principles

1. **Fail closed.** If identity, role, policy, or persistence cannot be verified, do not approve.
2. **Backend is authoritative.** The API rechecks permissions and state on every decision.
3. **No self-approval.** The requester and approving actor must be different identities.
4. **Specific-version approval.** A decision refers to the exact ticket/workflow version and approval request reviewed.
5. **Immutable history.** Never overwrite an earlier decision to make it look as if it did not happen.
6. **Least privilege.** Users receive only the permissions required for their role.
7. **Human intent is explicit.** Require a deliberate decision action and confirmation for approve/reject.
8. **LLM output is untrusted.** LLMs may summarize reasons but cannot determine authorization or persist an approval.
9. **Safe retries.** Duplicate requests and repeated decision submissions must not create duplicate or contradictory outcomes.
10. **No side effects.** Approval records a decision only; it does not trigger an order, email to a vendor, or payment.

---

## 4. Actors and permissions

Use stable user IDs from a trusted authentication mechanism. Display names are for presentation only and must not be used as authorization identifiers.

| Role | View ticket | Create/submit request | View pending approvals | Approve/reject | View audit history |
|---|---:|---:|---:|---:|---:|
| `requester` | Own/authorized tickets | Yes | No, unless also assigned | No | Own/authorized tickets |
| `approver` | Authorized tickets | No special privilege | Assigned/eligible requests | Yes, if eligible and not requester | Authorized tickets |
| `admin` | Authorized tickets | Operational support | Yes | **Not automatically** | Yes |
| `system` | Workflow service only | Create approval records | N/A | **Never casts a human decision** | Append system events |

### Authorization rules

- A role name supplied by the browser or request body is never trusted.
- The backend obtains identity and roles from verified authentication context.
- An `admin` role does not automatically bypass separation of duties.
- Approver eligibility must be evaluated server-side using role, assignment/policy, requester identity, request status, and version.
- Unauthenticated requests return `401`; authenticated but unauthorized requests return `403`.
- Do not include secrets, tokens, full connection strings, or unnecessary sensitive request text in logs.

### Hackathon authentication adapter

If a full identity provider is unavailable, implement a small `CurrentUser`/authentication dependency with a **local-demo-only** adapter:
- Demo users and their roles come from server-side environment/configuration, not from an arbitrary client-supplied role.
- The demo adapter is disabled outside explicit local/demo mode.
- Production-like configuration must fail closed if trusted authentication is not configured.
- Clearly label demo authentication as unsuitable for deployment.
- Never claim a user is authenticated merely because they sent `X-User-Role: approver`.

Keep the authorization service independent of the UI so a real identity provider can replace the demo adapter later.

---

## 5. What requires human approval?

Approval requirements must come from deterministic policy code and trusted workflow fields, not from an LLM-generated claim.

For the hackathon, define a single policy function, for example `evaluate_approval_requirement(workflow_result, policy_config)`, that returns:
- `required: bool`
- `reasons: list[str]`
- `risk_level: low | medium | high`
- `policy_version: str`
- `required_role: str`
- any applicable amount/threshold facts used by the rule

### Suggested initial rules

Keep thresholds configurable in one policy module/config file, and use the project's existing synthetic policies as the source of truth.

Require approval when any of these apply:
- The workflow result is `needs_human_review` or equivalent.
- The policy advisor flags a human-review requirement.
- A procurement request exceeds a configured amount threshold.
- The request has a policy exception or an unresolved policy conflict.
- A deterministic control explicitly marks the request as requiring approval.

Do **not** require approval solely because an LLM says “approve this.” Do not allow the LLM to lower risk or clear a deterministic policy flag.

If the request is blocked by a hard policy rule, keep it blocked; an approval should not automatically override the block. If a rule allows an exception, represent that as a distinct, explicit exception-approval reason and enforce the permitted approver role.

### Decision table

| Workflow/policy outcome | Approval behavior | Can approver make it executable? |
|---|---|---|
| `pass`, no approval rule matched | No approval request required | No; still draft-only |
| `pass`, approval threshold/rule matched | Create `pending` approval request | No; approval only records internal authorization |
| `needs_clarification` | Ask for missing information; do not create a misleading approval request yet | No |
| `blocked` by hard policy | Remain blocked; show reasons | No |
| `needs_human_review` / allowed exception | Create pending request with explicit reason and eligible role | No; decision does not override unrelated hard blocks |
| Persistence/auth/policy service unavailable | Fail closed; do not report approval success | No |

The exact field names must match the current `WorkflowResult` schema. Avoid introducing a second, conflicting source of truth for `final_status`.

---

## 6. Approval lifecycle and state machine

Keep the approval request state separate from the workflow's `final_status` and the PO's `status`.

### Approval request states

- `pending` — waiting for an eligible human decision.
- `approved` — approved by an eligible human for the recorded version.
- `rejected` — rejected by an eligible human.
- `changes_requested` — approver asks for changes; requester must revise and resubmit.
- `expired` — deadline elapsed without a decision.
- `superseded` — request version was replaced by a revised submission.

### Allowed transitions

```text
                 approve
             ┌───────────────> approved
             │
created ──> pending ─────────> rejected
             │                 reject
             │
             ├───────────────> changes_requested
             │                 request changes
             │
             ├───────────────> expired
             │                 deadline reached
             │
             └───────────────> superseded
                               revised submission
```

Rules:
- `approved`, `rejected`, `expired`, and `superseded` are terminal for that approval-request version.
- A terminal request cannot be edited back to `pending`.
- A revised request creates a **new approval-request version**; retain the prior record as `superseded`.
- If an approval-required ticket is edited after approval, the previous approval must not authorize the new content. Mark the old approval superseded and require review of the new version.
- `changes_requested` does not silently restart processing; it must be followed by an explicit revised submission.
- An approval decision never changes a hard-blocked workflow to `pass` by itself.

### Ticket status interaction

Prefer a clear, documented mapping between ticket state and approval state, for example:
- Ticket remains `pending_approval` while an approval request is pending.
- On approval, ticket may move to `approved` or `approved_draft` **only if the deterministic workflow has no independent hard block**.
- On rejection, ticket moves to `rejected` (or a clearly defined rejected terminal state).
- On changes requested, ticket moves to `changes_requested`.
- On expiry, ticket moves to `approval_expired`.
- On a persistence failure, ticket is not reported as successfully approved.

Do not overwrite the workflow's original `final_status`; store the approval outcome separately.

---

## 7. Approval request data model

Add a dedicated `approval_requests` collection. This keeps the decision lifecycle clear and avoids mixing approval history with the ticket's workflow snapshot.

Suggested document:

```json
{
  "approval_id": "APR-<unique-id>",
  "ticket_id": "REQ-...",
  "request_version": 1,
  "ticket_version": 1,
  "status": "pending",
  "requester_id": "user-123",
  "required_role": "approver",
  "assigned_approver_id": null,
  "reason_codes": ["AMOUNT_THRESHOLD"],
  "reasons": ["Request exceeds the configured approval threshold."],
  "risk_level": "medium",
  "amount": 76125,
  "currency": "INR",
  "policy_version": "approval-policy-v1",
  "workflow_snapshot_hash": "<hash-of-canonical-review-fields>",
  "created_at": "<UTC timestamp>",
  "expires_at": "<UTC timestamp>",
  "decisions": [],
  "schema_version": 1
}
```

The JSON is illustrative; match existing Pydantic conventions and current domain fields.

### Decision record

Append a decision record rather than overwriting prior history:

```json
{
  "decision_id": "DEC-<unique-id>",
  "action": "approve",
  "actor_id": "approver-456",
  "actor_role": "approver",
  "comment": "Reviewed the policy and budget.",
  "decided_at": "<UTC timestamp>",
  "idempotency_key": "<client-generated-unique-key>",
  "request_version": 1,
  "workflow_snapshot_hash": "<same-hash-that-was-reviewed>"
}
```

### Model requirements

- Store timestamps in UTC.
- Validate action/status with enums or strict Pydantic literals.
- Bound comment length; trim whitespace and reject empty comments if policy requires a comment.
- Do not store authentication tokens or secrets.
- Store the actor ID derived from authenticated server context, never from a trusted-looking body field.
- Store the policy version and review snapshot/hash to make the decision explainable later.
- Keep request text out of list endpoints unless strictly needed; return a short, safe summary instead.
- Use a canonical serialization method before hashing review-relevant fields. Document which fields are covered.
- Do not treat a hash as authorization; it only helps detect changes to reviewed content.

---

## 8. MongoDB indexes and concurrency safety

Add indexes through the existing `app/db/indexes.py` setup:
- Unique `approval_id`.
- Unique `(ticket_id, request_version)`.
- Index `(status, created_at)` for pending queues.
- Index `(requester_id, created_at)` for requester history.
- If decisions are stored separately, unique `decision_id` and a unique idempotency key scoped to actor/request.

### Atomic decision update

Two approvers can open the same pending request at the same time. Only one decision may win.

Use a conditional atomic update with the current state/version, for example:
- Match `approval_id`, `status == "pending"`, `request_version`, and expected snapshot/version.
- Update the terminal status and append the decision in the same document update, or use a MongoDB transaction if multiple collections must change atomically and the deployment supports transactions.
- If the update matches zero documents, reload current state and return `409 Conflict` with a safe explanation such as “This approval has already been decided or the request has changed.”

Do not implement the decision as “read pending -> decide -> write status” without a conditional write. That pattern permits concurrent decisions to overwrite one another.

### Transaction boundary

Approval request, ticket state, and audit history can span collections. Prefer a transaction where supported. If the current development MongoDB topology does not support transactions:
- Use a single-document conditional state transition as the authoritative decision commit.
- Make secondary ticket/audit updates idempotent and recoverable.
- If a secondary write fails after the decision commits, report the decision's actual durable state accurately and flag reconciliation needed; do not tell the user that the decision did not happen if it did.
- Add a reconciliation/diagnostic path for incomplete secondary updates.
- Document that multi-collection writes are not atomic.

---

## 9. API contract

Use the existing FastAPI conventions and response models. Final paths can be aligned with current routing, but the authorization semantics must remain.

### Suggested endpoints

| Method and path | Purpose | Access |
|---|---|---|
| `POST /api/tickets/{ticket_id}/approval-requests` | Evaluate eligibility and create a pending approval request when required | Requester/system policy path |
| `GET /api/approvals?status=pending` | List eligible pending approvals | Approver |
| `GET /api/approvals/{approval_id}` | View details and decision history | Authorized requester/approver |
| `POST /api/approvals/{approval_id}/decision` | Submit `approve`, `reject`, or `request_changes` | Eligible approver |
| `GET /api/tickets/{ticket_id}/approval` | Read current approval status/history for a ticket | Authorized ticket reader |
| `POST /api/tickets/{ticket_id}/resubmit` | Create a new version after changes, if this is included in Phase 2 | Requester |

Avoid allowing a client to choose arbitrary `requester_id`, `actor_id`, `required_role`, `status`, `policy_version`, `workflow_snapshot_hash`, or `persisted` fields.

### Decision request body

```json
{
  "action": "approve",
  "comment": "Reviewed the request and policy requirements.",
  "idempotency_key": "a-client-generated-unique-value",
  "request_version": 1,
  "workflow_snapshot_hash": "<hash-returned-by-the-API>"
}
```

The API must derive actor identity/roles from trusted authentication context. The body must not contain a role that the server accepts as proof of authorization.

### Response behavior

- `200` — decision recorded or the same idempotent decision is replayed.
- `201` — new approval request created.
- `400` — malformed action/comment.
- `401` — not authenticated.
- `403` — role not allowed or self-approval attempt.
- `404` — request/ticket not found or not visible to this user (choose a consistent policy to avoid leaking existence).
- `409` — already decided, stale version/hash, or conflicting idempotency key.
- `422` — request does not satisfy domain validation.
- `503` — required database/auth/policy dependency unavailable; no false success response.

Do not leak stack traces, database URIs, or internal exception details in API responses.

---

## 10. Approval decision rules

### Approve

The backend must verify all of the following before committing:
1. Authenticated identity is present and trusted.
2. User has the required approval role and is eligible for this request.
3. Actor is not the requester.
4. Request status is `pending`.
5. Request has not expired.
6. Submitted request version and review hash match the current record.
7. The workflow is not hard-blocked by an unrelated policy.
8. Required persistence is available.
9. Atomic conditional state transition succeeds.

Then record `approved`, append the decision/audit event, and show the user that the request was approved **for internal workflow purposes only**. Do not create a purchase, send a PO, or pay an invoice.

### Reject

- Requires an eligible approver and a valid pending version.
- Store the rejection and optional/required reason.
- Update the visible ticket/approval state.
- Preserve the draft PO for audit/inspection if one already exists; do not submit it externally.
- A rejected request cannot be approved later by changing its status. Resubmission creates a new version.

### Request changes

- Record a decision with a bounded comment describing the requested changes.
- Move the request to `changes_requested`.
- Require an explicit revised submission before creating a new approval version.
- The old request remains visible in history.

### Expire

- Expiration is evaluated server-side; do not rely on the browser clock.
- An expired request cannot be approved.
- Record the expiration event, either via a scheduled job or when an API reads/decides the request.
- Do not auto-approve on timeout.

### Repeated submissions and idempotency

- Require an idempotency key for decision submissions.
- Repeating the same key, actor, and same action should return the original decision safely.
- Reusing the same key for a different action/body must return `409`.
- A different key must not permit a second conflicting decision after the request is terminal.

---

## 11. UI behavior (Streamlit)

### Requester view

Show:
- Ticket ID and workflow outcome.
- Approval status badge/text.
- Deterministic reasons approval is required.
- Amount/currency and policy facts used for the decision.
- Who requested approval and when.
- Current approver/role if assigned.
- Decision history and comments.
- Clear message: “Approval records internal authorization only. No order or payment is executed.”

Requester can:
- Submit a request if policy says approval is required.
- See the current status and history.
- Resubmit after `changes_requested`, creating a new version.
- Not see an enabled approve/reject control for their own request.

### Approver view

Show a pending-approval queue with:
- Approval ID and ticket ID.
- Request summary and amount/currency where applicable.
- Risk level and exact policy reason codes.
- Requester identity.
- Request version, creation time, expiry, and current status.
- Snapshot of the fields being approved.
- Existing decisions/history.

Approver actions:
- `Approve`
- `Reject`
- `Request changes`

Require a deliberate confirmation step before submission. Require a reason/comment for rejection and request-changes; decide whether approval comments are optional or required in policy. Disable buttons while a request is in flight to prevent accidental double-clicks, but still rely on backend idempotency and atomic state checks.

After submission:
- Refresh from the server.
- Display the server's recorded decision and timestamp.
- If `409`, explain the request was already decided or changed and reload it.
- If `503`, do not show success; state that the outcome could not be confirmed and instruct the user to reload before retrying.
- Never infer success from a button click alone.

### UI must not

- Treat a hidden/disabled button as the permission check.
- Let a user edit their own role or actor ID.
- Show an approval as successful before the API confirms it.
- change a terminal decision optimistically.
- Label a draft PO as “ordered,” “purchased,” or “paid.”
- Expose request text in list pages unnecessarily.

---

## 12. Audit requirements

Append auditable events for:
- Approval request created.
- Approval request submitted/resubmitted.
- Decision attempted but denied, where safe and useful.
- Approved, rejected, or changes requested.
- Expired or superseded.
- Stale-version/conflict outcome.
- Reconciliation required after a partial persistence failure.

Each event should include:
- Event ID, ticket ID, approval ID, request version.
- Event type, actor ID (or system actor), actor role from trusted context.
- UTC timestamp.
- Decision/reason code and comment where applicable.
- Workflow snapshot hash/policy version.
- Outcome and safe error category if applicable.

Never write secrets, bearer tokens, raw connection strings, or full request bodies to audit logs. Treat audit history as append-only from application code; do not expose an API to edit/delete decisions.

---

## 13. Failure handling and safe behavior

| Failure | Required behavior |
|---|---|
| Database unavailable before decision | Fail closed; return safe `503`; do not claim approved |
| User not authenticated | `401`; no decision written |
| User lacks role | `403`; no decision written |
| Requester attempts own approval | `403`; no decision written |
| Approval already terminal | `409`; preserve original decision |
| Stale request version/hash | `409`; require refresh/review |
| Two approvers decide concurrently | One conditional transition wins; the other receives `409` |
| Idempotency key reused with different payload | `409`; do not mutate state |
| Audit write fails after decision state commits | Preserve truthful decision status, flag reconciliation, and avoid reporting that no decision occurred |
| Policy evaluation fails | Fail closed; do not create an approval or treat it as unnecessary |
| Expiry deadline passed | Reject decision as expired; never auto-approve |
| LLM says approval is granted | Ignore it as authorization; only the authenticated decision endpoint can record a human decision |

For auditability, decide whether denied attempts should be written to a separate security log when the primary database is unavailable. Do not let logging failure turn a denied action into an allowed action.

---

## 14. Suggested code organization

Align names with the current repository, but keep responsibilities separated:

```text
app/
  approvals/
    __init__.py
    schemas.py             # ApprovalRequest, Decision, enums
    policy.py              # deterministic approval requirement rules
    service.py             # lifecycle and decision orchestration
    authorization.py       # current-user and role/eligibility checks
  repositories/
    approval_repository.py # MongoDB reads, atomic transitions, idempotency
  db/
    indexes.py             # approval indexes
  main.py                  # approval routes and auth dependency
  schemas.py               # reuse existing workflow/ticket types where appropriate
ui/
  streamlit_app.py         # pending queue, details, decision confirmation
tests/
  test_approval_policy.py
  test_approval_authorization.py
  test_approval_api.py
  test_approval_concurrency.py
  test_approval_idempotency.py
  test_approval_ui.py
  test_approval_persistence.py
```

Do not create duplicate definitions of `WorkflowResult` or ticket status. Reuse existing types and add approval-specific schemas separately.

---

## 15. Test plan

### Policy tests
- Approval requirement is deterministic for the same input/config.
- Each configured threshold/exception rule has boundary tests.
- Hard-blocked workflows cannot be made approved by the approval policy.
- LLM-generated text cannot change authorization requirements.
- Policy version and reason codes are populated.

### Authorization tests
- Unauthenticated user cannot decide.
- Requester cannot approve their own request.
- Requester role cannot approve.
- Eligible approver can decide.
- Wrong role and wrong assignment are denied.
- Client-supplied actor ID/role is ignored or rejected.
- Admin cannot bypass self-approval or terminal-state checks.

### Lifecycle/API tests
- Create a pending approval request for a qualifying workflow.
- No approval request is created for a workflow that does not require it.
- Approve/reject/request changes produces the correct state and audit event.
- Hard-blocked workflow remains blocked after attempted approval.
- A terminal request cannot be decided again.
- Stale version/hash returns `409`.
- Expired request cannot be approved.
- Safe `401`, `403`, `404`, `409`, `422`, and `503` responses contain no internals.
- API never claims an order was placed or an invoice paid.

### Idempotency/concurrency tests
- Same key and same payload returns the original decision.
- Same key with different payload returns `409`.
- Two concurrent decisions result in exactly one winning terminal decision.
- Duplicate clicks do not duplicate decision/audit records.
- A stale client cannot approve a revised request.

### Persistence/recovery tests
- Approval requests survive API restart against real MongoDB.
- Decision and history survive restart.
- Required indexes exist.
- Failure between decision commit and secondary audit/ticket update is diagnosable and recoverable.
- A failure never yields a false success response.

### UI tests
- Requester sees pending state but cannot decide.
- Approver sees eligible pending requests.
- UI requires confirmation and shows server result.
- On `409`, UI refreshes state.
- On `503`, UI does not display approval success.
- UI labels the PO as draft and explicitly states that no order/payment occurred.

---

## 16. Implementation sequence

### Phase 2A — Domain and policy
1. Inspect existing `WorkflowResult`, ticket statuses, approval-related flags, and policy rules.
2. Define approval enums, schemas, and deterministic `evaluate_approval_requirement()`.
3. Agree on threshold/role configuration and test its boundaries.
4. Keep the LLM out of the authorization decision.

### Phase 2B — Persistence and lifecycle
1. Add `approval_requests` repository and MongoDB indexes.
2. Implement request creation and immutable decision history.
3. Implement atomic conditional decision transitions and idempotency.
4. Implement version/snapshot checks and expiry behavior.
5. Define partial-failure/reconciliation behavior.

### Phase 2C — API security
1. Add the trusted `CurrentUser` dependency/auth adapter.
2. Enforce role, eligibility, no-self-approval, version, expiry, and status checks in the service layer.
3. Add endpoints and safe error responses.
4. Ensure no decision route invokes external procurement/payment actions.

### Phase 2D — UI
1. Add requester approval status and history.
2. Add pending-approval queue and detail view.
3. Add approve/reject/request-changes confirmation.
4. Refresh from server after every decision and handle conflicts safely.

### Phase 2E — Verification
1. Run all existing Phase 1 tests.
2. Run approval unit/API/UI tests.
3. Run real MongoDB persistence/restart tests.
4. Test concurrent decisions and idempotency.
5. Manually verify no order/payment side effects are possible.
6. Document demo-auth limitations and configuration.

---

## 17. Definition of done

Phase 2 is complete only when all applicable items below are demonstrated:

- [ ] Approval requirements come from deterministic server-side policy.
- [ ] Approval-required workflows create durable pending approval records.
- [ ] No approval is possible without trusted authentication and an eligible role.
- [ ] Requesters cannot approve their own requests.
- [ ] Client-supplied role/actor identity cannot grant permission.
- [ ] Decisions are tied to a specific request version and review snapshot.
- [ ] Concurrent conflicting decisions cannot both succeed.
- [ ] Idempotent retries do not duplicate or contradict decisions.
- [ ] Rejection and request-changes outcomes are preserved as history.
- [ ] Expired requests cannot be approved.
- [ ] Ticket/approval/audit state is consistent or a partial state is explicitly flagged for reconciliation.
- [ ] UI displays the backend's authoritative state and handles `409`/`503` safely.
- [ ] Tests prove the core policy, authorization, lifecycle, concurrency, and persistence behavior.
- [ ] All existing Phase 1 tests continue to pass.
- [ ] Approval never sends a PO, places an order, or initiates payment.
- [ ] Demo authentication is clearly restricted to local/demo use and is not represented as production security.

Do not mark an item complete based only on code being present. Record the test or manual acceptance evidence.

---

## 18. Demo acceptance scenario

Use synthetic data only.

1. Submit a request that deterministically requires approval.
2. Verify the workflow creates a ticket and a `pending` approval request with a policy reason.
3. Sign in as the requester and confirm there is no eligible decision action.
4. Attempt an API approval as the requester; verify `403` and no approval state change.
5. Sign in as a different eligible approver and open the request detail.
6. Review the exact amount, policy reason, request version, and snapshot.
7. Approve it and verify the server returns the persisted decision.
8. Refresh/restart the API and verify the decision and audit history remain available.
9. Submit a second decision for the same request and verify `409` or a safe idempotent replay, depending on the key.
10. Verify the ticket is internally marked approved, but the PO remains `draft` and no external order/payment action occurs.
11. Repeat with a rejected request and a hard-blocked request; ensure neither becomes executable.

---

## 19. Decisions to finalize before implementation

Use these as implementation defaults unless the existing code/policy requires otherwise:

- **Approval roles:** start with one `approver` role; keep policy-driven eligibility extensible.
- **Separation of duties:** requester may never approve their own request.
- **Decision actions:** `approve`, `reject`, `request_changes`.
- **Comments:** required for `reject` and `request_changes`; approval comment optional or required according to policy.
- **Expiry:** configurable duration, evaluated by server time; never auto-approve.
- **Versioning:** increment approval request version whenever reviewed fields change.
- **Persistence:** dedicated `approval_requests` collection with atomic conditional state transition.
- **Authentication:** trusted auth dependency; local demo adapter only for local demo/test runs.
- **Execution boundary:** approval is an internal authorization record only. Order creation and payment remain out of scope.

**Phase 2 deliverable:** a verifiable, auditable approval control that is enforced by the backend and remains safe under unauthorized requests, retries, stale data, concurrency, and persistence failures.
