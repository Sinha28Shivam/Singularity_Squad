# WorkplaceOps AI — Phase 5 Product-Grade User Experience
## Requirements, Architecture, Implementation Plan, and Acceptance Criteria

**Status:** Proposed specification  
**Repository:** `Sinha28Shivam/Singularity_Squad`  
**Stack:** React 19 + Vite, FastAPI, Pydantic, MongoDB  
**Purpose:** Upgrade the current application with trustworthy workflow transparency, role-specific experiences, explainable decisions, operational observability, and production-grade security and recovery.

## 1. Executive summary

Phase 5 has five workstreams:

1. Workflow timelines and clear pending-action indicators.
2. Role-specific views for employees/requesters, procurement, Finance, and approvers, plus operational administration.
3. Evidence-based explanations for vendor recommendations and policy decisions.
4. Durable operational metrics, failure alerts, and traceable logs.
5. Monitoring, secrets management, backups/restore, and security testing.

Implement these incrementally. Begin with a repository/state/permission audit, then implement timeline and pending-action contracts, then role-specific UI and explanations, then observability and production hardening.

### Non-negotiable safety invariants

- FastAPI remains authoritative for authentication, authorization, state transitions, policy evaluation, approval eligibility, and financial safeguards.
- React role-based navigation is a UX feature, never a security boundary.
- Display only persisted or explicitly returned server state; never fabricate workflow progress, results, counts, or success.
- Vendor comparisons are advisory. Vendor outreach, contract awards, PO dispatch, and real payments remain disabled unless separately reviewed and explicitly authorized.
- Payments remain simulation-only; retain `mode: "SIMULATION"` and `money_transferred: false`. No bank or payment-gateway call is introduced.
- Distinguish submitted, extracted, declared, verified, rejected, expired, and missing evidence.
- Consequential operations require server-side validation, authenticated actor, audit history, appropriate reason, and idempotency/concurrency protection.
- Never put secrets in source control or frontend `VITE_*` variables.
- Logs, metrics, and alerts must not leak passwords, tokens, database URIs, raw PDFs, or unnecessary personal/business data.

## 2. Current baseline and assumptions

Repository documentation and the recent React migration describe this baseline. Verify it against the actual `master` checkout before coding.

- React UI in `frontend/`, Vite development port `5173`; FastAPI under `app/`, typically port `8000`; MongoDB persistence.
- Shared API client, bearer-token authentication, and `sessionStorage`.
- Existing roles documented as `user`, `admin`, and `approver`.
- Existing capabilities include requests/workflows, policy library/rules, approvals, vendor sourcing, RFQs/quotes, and payment simulation.
- Vendor comparison is advisory and quote verification is human-controlled.
- Some sourcing/payment metrics are process-local and reset on restart.
- CORS is configured by `CORS_ALLOWED_ORIGINS`; development origins are not production settings.
- Recent UI support includes server-side ticket search/pagination, published upload limits/rule facts, candidate transition information, and saved comparison details.
- Production hosting, monitoring, secret rotation, backup/restore, and incident runbooks need explicit validation.

### Role-model decision

The current roles do not directly match all requested personas. Do not solve this only with navigation links. In Phase 5A, decide whether to reuse existing roles for task-oriented views or introduce distinct procurement and Finance capabilities/roles. Recommended: define explicit backend capabilities and map roles to them. Do not silently promote existing accounts. New permissions default to denied until assigned. Preserve admin/approver separation and self-approval prevention.

## 3. Goals, non-goals, and success measures

### Goals
- Users can answer: what happened, what is happening, what is blocked, who must act, and what happens next.
- Every role sees the relevant queue and only authorized actions.
- Vendor/policy explanations point to decisive factors and evidence.
- Operators can detect, investigate, and recover from failed or stalled work.
- Required metrics survive restarts or are explicitly labeled ephemeral.
- Alerts have severity, owner, runbook, and tested delivery.
- Secrets, backups, restore procedures, and security tests are exercised.

### Non-goals
- Replacing React, FastAPI, MongoDB, or the current orchestrator.
- Adding automatic vendor awards, supplier emails, PO dispatch, or real payments.
- Letting an LLM become the authority for a policy or approval decision.
- Building a large analytics platform before metrics have defined operational value.
- Logging raw prompts, PDFs, credentials, or full request payloads by default.

### Suggested success measures

| Measure | Initial acceptance target |
|---|---|
| State visibility | Every supported work item has current status, last update, and next action where applicable |
| Timeline coverage | Every supported state transition emits an auditable event |
| Pending-action clarity | Actionable queue items identify owner/role and reason where known |
| Explanation traceability | Material factors link to structured facts/evidence |
| Authorization | Protected endpoints have positive and negative role/ownership tests |
| Observability | API requests have correlation IDs; failures can be traced by request ID |
| Alert quality | Every production alert has severity, owner, runbook, and tested delivery |
| Recovery | Encrypted backups and a successful restore drill meet agreed RPO/RTO |
| Security | No unaccepted critical/high findings at release |
| Usability | Critical role journeys work on desktop/mobile and with keyboard |

Set final SLOs and thresholds after measuring baseline traffic and failure rates.

## 4. Role-specific requirements

### Employee / requester
- “My requests” shows only the authenticated user's records unless backend policy permits otherwise.
- List request ID, summary, created/updated time, status, and next action.
- Detail page shows chronological timeline, actor, timestamp, explanation, and evidence where appropriate.
- Highlight missing information and requested changes.
- Distinguish workflow completion, approval, draft PO creation, invoice reconciliation, and payment simulation.
- Never imply that submission places an order or approval dispatches a PO.
- Preserve entered form data after recoverable errors.

### Procurement
- Queue for sourcing tasks with status, age, deadlines, and next action.
- Keep the internal vendor catalog separate from candidate onboarding.
- Show candidate provenance, status, allowed decisions, and review history.
- RFQs show status, response deadline, candidate count, quote count, and next action.
- Quotes distinguish submitted values, extracted PDF values, verified values, and revisions.
- Comparisons show hard gates separately from ranking/score and remain advisory.
- A procurement role must not automatically inherit admin or approver permissions.

### Finance
- Focus on invoice reconciliation, exceptions, approved request/PO context, duplicate checks, and payment simulation records where authorized.
- Distinguish invoice matched, approval current, eligible for simulation, simulator accepted, and money transferred.
- Display backend eligibility and reasons; do not independently reimplement authoritative checks in React.
- Finance must not approve its own requests or edit policy rules without separate authorization.
- Payment simulation remains disabled by backend configuration when off; hiding a page is not enforcement.

### Approver
- Queue/history with relevant statuses and filters.
- Show request snapshot, draft PO summary, reasons, policy-set/version, evidence, and stale/superseded state.
- Only show actions the backend permits for the current actor/state.
- Require confirmation and reason where applicable.
- Preserve self-approval prevention and policy-version invalidation.
- Display a decision only after server confirmation.
- State clearly that approval is internal authorization, not PO dispatch or payment.

### Administrator / operations
- Operational overview: API/database health, workflow failures, queue age, error rates, alerts, and backup freshness where available.
- Drill down by request/correlation ID, safe failure category, and time window.
- Separate read-only operations from configuration/role administration.
- Display only metrics with defined source and semantics; distinguish unknown from zero.
- Admin access does not grant approver privileges.

## 5. Workflow timeline and pending actions

### Timeline event contract

Use existing audit/event records when sufficient; avoid a duplicate timeline store without a clear reason. Timeline events should be append-only and include, where applicable:

- `event_id`, `request_id`/`ticket_id`, `event_type`
- `from_status`, `to_status`
- `occurred_at` (UTC)
- `actor_type` and safe actor display label
- plain-language `summary`, safe `reason_code`
- `correlation_id`, `workflow_run_id`/`approval_id`
- `evidence_refs`, schema version

Potential events: request submitted/updated/resubmitted; workflow started/completed/blocked/failed; information requested/supplied; RFQ created/closed/cancelled; quote submitted/revised/verified; comparison generated; human sourcing decision; approval requested/approved/rejected/changes requested/expired/superseded; invoice matched/mismatch; payment simulation created/failed/accepted; operator retry/recovery.

Only show workflow stages when the backend persists reliable stage data. Never infer success from missing errors.

### Separate state, events, metrics, and alerts
- Current state answers “what is true now?”
- Timeline events answer “how did we get here?”
- Metrics are aggregates, not audit history.
- Alerts are operational notifications, not business states.

Permissions must always validate current persisted state/version, not a timeline event alone.

### Pending-action contract

Possible response shape (illustrative; not an existing endpoint claim):

```json
{
  "action_required": true,
  "action_owner": "approver",
  "action_code": "approval_review",
  "label": "Approval needed",
  "reason": "The request is awaiting an authorized approver.",
  "due_at": null,
  "blocked": false,
  "blocked_reason": null
}
```

Requirements:
- Derive action summaries server-side from persisted state and allowed transitions.
- Use defined enums and schema validation.
- Distinguish blocked from waiting for a person.
- Show due dates only when configured.
- Terminal items have no active action unless a supported follow-up exists.
- Unknown future event/action types must render safely.

### Stalled work
Define per-stage thresholds after measuring normal behavior. Each rule needs applicable states, threshold/clock, exclusions, notification destination, dedupe key/cooldown, escalation, owner, and runbook. A timeout must never auto-approve, auto-reject, or award a vendor.

## 6. Explainable vendor and policy decisions

### General rules
- Build explanations from deterministic structured results first.
- An LLM may paraphrase for readability, but must not invent evidence, policy clauses, prices, dates, eligibility, scores, or decision reasons.
- Preserve the machine-readable result for tests and audit.
- Distinguish verified, extracted, declared, missing, expired, and unverified evidence.
- Provide source title/version/page/section or stable evidence reference.
- Do not reveal hidden chain-of-thought. Show concise, evidence-based rationale.

### Vendor comparison explanation
Show:
1. Eligibility: eligible / ineligible / needs verification.
2. Hard gates: budget, delivery, currency compatibility, warranty, mandatory compliance evidence.
3. Cost calculation: unit price × quantity + tax + delivery + other fees.
4. Delivery/warranty values and accepted scope.
5. Compliance evidence state/expiry.
6. Score components/weights only if actually computed.
7. Why the quote ranked above/below alternatives.
8. Missing data, caveats, reviewer notes, evidence references.
9. Human decision/reason separately from automated comparison.

Do not claim the lowest-cost eligible quote is universally “best”. Any weighted ranking must define weights, normalization, hard gates, tie-breaking, versioning, and tests first.

### Policy explanation
Show actual outcome vocabulary, rule IDs/versions, facts and their provenance, conditions passed/failed, expected versus observed values where safe, policy citations, missing/conflicting evidence, next permitted action, and stale policy-set status.

### Example structured explanation

```json
{
  "explanation_version": 1,
  "outcome": "needs_review",
  "summary": "Delivery is later than the requested date.",
  "factors": [
    {
      "code": "delivery_after_required_date",
      "label": "Delivery date",
      "result": "fail",
      "expected": "2026-12-15",
      "observed": "2026-12-18",
      "evidence_refs": ["quote:Q-123"]
    }
  ],
  "limitations": ["Quotation terms were verified by a reviewer."],
  "policy_refs": [],
  "generated_at": "2026-10-21T12:00:00Z"
}
```

This is an example contract, not a claim that these exact fields already exist. Reuse existing schemas and avoid duplicating persisted facts.

## 7. Operational metrics and alerts

### Initial metric catalogue

**API/service:** request rate/count by route template and status, p50/p95/p99 latency, exceptions, timeouts, database availability/failures, provider latency/errors.

**Workflow:** runs by stage/outcome, duration, failure category, retries, repeated failures, age of items by pending state, requests awaiting information, pending approvals and age, stale/superseded approvals.

**Procurement:** candidates awaiting review, open RFQs/deadlines, quotes awaiting verification, verification failures, comparisons by eligibility reason, time to sourcing decision.

**Finance:** reconciliation outcomes, duplicate flags, blocked simulation attempts by safe reason code, simulation outcomes. No metric may imply simulated money moved.

**Security/reliability:** auth failures, rate-limit events, authorization denials by route/role, configuration failures, backup age/status, restore-test result, alert delivery failures.

Each metric needs name, unit, owner, source, time window, interpretation, privacy class, and retention. Avoid unbounded labels such as ticket IDs, usernames, email addresses, raw URLs, exception messages, or vendor names.

Replace process-local counters when cross-restart reporting is required. Use append-only operational events and/or an appropriate metrics backend. Telemetry failures should not break business workflows unless a required audit/security record cannot be persisted. Define fail-closed behavior for audit-critical writes.

### Alert requirements

Each alert must define unique key, severity, trigger/window, deduplication/cooldown, destination, owner/on-call role, runbook, resolution condition, audit trail, and test procedure.

Start with high-signal alerts:
- API unavailable/repeated health-check failure
- MongoDB unavailable or persistence failures over threshold
- workflow failure rate over baseline
- queue items older than defined threshold
- repeated required provider failures
- backup missed or too old
- restore verification failed
- required configuration/secret validation failed
- auth/authorization anomalies over measured threshold
- alert channel delivery failure

Avoid alerting on every validation error. Never put secrets or full payloads in alert messages.

### Operations dashboard
Answer: Is the service available? Is persistence healthy? Are workflows failing/accumulating? Which queues need action? Are backups current and restorable? Which alerts are active? What changed recently?

Every metric shows time window, last-updated time, and source. Show “unknown/unavailable” rather than zero when data is unavailable.

## 8. Monitoring, logs, and correlation

- Accept a valid `X-Correlation-ID` or generate one; return it in the response and propagate it through route, workflow, repositories, and provider calls where supported.
- Validate length/format; do not treat correlation IDs as authorization tokens.
- Structured logs should include UTC timestamp, severity, environment/version, event name, route template, status/duration, correlation ID, request ID where appropriate, stage/outcome, and safe error category.
- Redact tokens, passwords, DB URIs, raw PDF text, full prompts/responses by default, and unnecessary personal/financial data.
- Separate liveness, readiness, and dependency status. Public health output must not expose stack traces, internal hostnames, database URI, or secret values; detailed diagnostics require operator authorization.
- Monitoring should not be a prerequisite for business success unless it represents a required audit/security write.

## 9. Security and secrets management

### Authorization
- Create a route × action × role/capability matrix.
- Enforce permissions in FastAPI; frontend guards only improve UX.
- Test allowed and denied cases for sensitive actions.
- Preserve ownership checks and self-approval prevention.
- Prevent privilege escalation via signup, profile edits, crafted payloads, or role migration.
- Use least privilege for users and service accounts.
- Separate read-only operational access from configuration-changing admin actions.

### Secrets
Inventory LLM keys, MongoDB credentials/URI, admin bootstrap password, approver provisioning credentials, session/token signing material if applicable, alert credentials, backup encryption keys, and deployment credentials.

Requirements:
- Local `.env` is ignored; `.env.example` contains placeholders only.
- Production secrets are injected by a selected secret manager/protected deployment environment.
- Never expose secrets in `VITE_*` variables or source.
- Rotate exposed/suspected compromised secrets; document and test rotation.
- Separate development/staging/production credentials.
- Restrict and audit secret access.
- Validate required configuration at startup without logging values.
- Fail closed for safety-sensitive features.

### Web and application security
Review/test XSS and unsafe HTML, CSRF implications of auth model, CORS, CSP/security headers, clickjacking, HTTPS, brute-force/rate limiting, session expiry/logout, IDOR, strict Pydantic validation, file-upload/PDF resource limits, dependencies, secret leakage, injection from vendor/policy documents, audit integrity, and data retention.

Treat PDF and policy content as untrusted data, never as instructions that can override system policy.

### Security test plan
- unit/API tests for authorization, validation, ownership, idempotency, stale versions and concurrency
- frontend tests for safe error display and unauthorized navigation (not a substitute for API tests)
- dependency and secret scans in CI
- static analysis and staging dynamic tests
- threat-model review for auth, approvals, uploads, sourcing, and payment simulation
- finding register with severity, owner, fix version, and retest evidence
- no destructive production testing without explicit authorization and recovery plan

## 10. Backups, restore, and disaster recovery

### Data inventory
Inventory MongoDB collections (tickets, workflow results, approvals, audit/events, policy registry, candidates, RFQs, quotes, simulated payments, users/sessions as applicable), policy documents and derived chunks, private quote PDFs (`VENDOR_QUOTE_STORE_DIR` or replacement), configuration, and infrastructure definitions. Identify source-of-truth versus reconstructible data and retention obligations. Recover secrets through the secret manager's supported process, not plaintext application backups.

### Backup requirements
- Agree RPO and RTO with the project owner before choosing schedules.
- Automate encrypted backups for MongoDB and private document storage.
- Store backups separately from the primary failure domain with access control.
- Define retention and privacy/deletion policy.
- Alert on failed/missed jobs and stale backups.
- Verify integrity and document hashes where available.
- Preserve consistency between DB references and private files.
- Record completed/validated results, not merely job start.

### Restore drill
Restore into isolated non-production; verify collections/indexes, roles, audit history, policies, quote PDFs, checksums, application startup, representative reads, measured duration vs RTO and data loss vs RPO. Record date, backup ID, operator, outcome, duration, issues, and follow-up. Repeat on an agreed cadence.

## 11. Proposed architecture

```text
React browser
  ├── Employee/requester workspace
  ├── Procurement workspace
  ├── Finance workspace
  ├── Approver workspace
  └── Operations/admin workspace
          │ HTTPS + authenticated API
          ▼
FastAPI
  ├── Authentication + capability authorization
  ├── Request/workflow services
  ├── Approval service
  ├── Vendor sourcing/RFQ/quote services
  ├── Policy evaluation/evidence
  ├── Finance reconciliation/payment simulation
  ├── Timeline/pending-action projection
  ├── Audit/operational events
  ├── Metrics/alert evaluation
  └── Health/readiness endpoints
          │
          ├── MongoDB: business state and audit events
          ├── Private file/object storage: documents/PDFs
          ├── Monitoring/logging/alert destination
          └── Secret manager
```

Keep business rules in backend services; avoid duplicate policy logic in React. Use versioned schemas. Define how state writes and required audit events stay consistent (transaction if supported, otherwise outbox/reconciliation). Avoid distributed infrastructure unless required by the deployment scale.

## 12. API and data contracts

Inspect current routers/schemas first. Reuse existing endpoints when sufficient.

### Timeline
Possible route: `GET /api/{scope}/tickets/{ticket_id}/timeline?limit=&cursor=`.
- authorize event access
- stable ordering by timestamp + event ID
- paginate long history
- sanitize actor labels/summaries
- omit raw secrets/PDF text/private metadata

Match existing route conventions such as `/api/my/*`, `/api/admin/*`, and approval routes.

### Pending action
Consider adding to existing queue/detail responses:
- `action_required`, `action_owner`, `action_code`, `action_label`, `action_reason`
- `due_at`, `blocked`, `blocked_reason`, `last_transition_at`

Derive from backend state and allowed transitions. Test that every offered action is actually allowed for that actor and state.

### Explanation
Prefer versioned structured explanations on existing policy/comparison responses:
- stable factor codes
- evidence refs/source version
- expected/observed values where safe
- verification state
- score components only if computed
- limitations and policy version

### Protected operational APIs
Possible endpoints, only if needed:
- `GET /api/admin/operations/summary?window=...`
- `GET /api/admin/operations/queues`
- `GET /api/admin/operations/alerts?status=active`
- `GET /api/admin/operations/alerts/{alert_id}`
- `POST /api/admin/operations/alerts/{alert_id}/acknowledge`

Define alert ownership/acknowledgement semantics first. Keep public `/api/health` minimal.

### Write consistency
For each important transition, document source-of-truth write, audit event write, transaction/outbox strategy, idempotency, retry behavior, and API behavior if the state succeeds but required event persistence fails. Do not claim a transition is fully audited if its required audit event failed.

## 13. Frontend component plan

Keep the current white-first design, plain CSS, and shared API client.

Suggested shared components:
- `WorkflowTimeline`: chronology, actor/time, evidence disclosure, mobile layout
- `PendingActionBanner`: owner, action label, reason, CTA only when authorized
- `StatusSummary`: current state, last updated, blocked/pending/completed distinction
- `DecisionExplanation`: summary, factors, evidence links, limitations, score disclosure
- `EvidenceReference`: source/version/page/section and verification status
- `QueueFilters`: server-side filtering and pagination
- `MetricCard`: value, time window, freshness, availability
- `AlertList` / `AlertDetail`: severity, trigger, owner, runbook, state
- `RoleWorkspaceLayout`: shared shell and role-appropriate navigation
- `SafeErrorState`: plain-language error, safe retry, correlation ID
- common loading/empty/unavailable states

Interaction rules:
- Every async view has loading, empty, error, and unavailable states.
- Retry only when safe; use idempotency where supported.
- A UI-disabled control is not a security control.
- Update state only from confirmed server responses after writes.
- No optimistic success for approvals, quote verification, or payment simulation.
- Statuses must include text, not color alone; support keyboard and focus.
- Use server pagination for large queues.
- Do not show decorative charts without operational purpose.

## 14. Implementation roadmap

### Phase 5A — Discovery, roles, and event/state audit
Tasks:
1. Checkout latest `master`; inspect React routes, FastAPI routers, service schemas, audit/event collections, metrics.
2. Inventory states for requests/workflows/approvals/candidates/RFQs/quotes/invoices/payment simulations.
3. Map transitions to code and audit writes; identify missing durable events.
4. Map each endpoint to role, capability, ownership rule, and allowed state.
5. Decide employee/procurement/Finance/approver/admin capability matrix and role migration.
6. Define metric catalogue, alert ownership, deployment target, data classification, RPO/RTO, retention.
7. Record architecture decisions.

Deliverables: role/capability matrix, state-transition/event map, metrics/alert catalogue, security/backup inventory, prioritized gap list.
Exit: no protected route unaccounted for; unknowns explicitly marked; no privilege changes.

### Phase 5B — Timeline and pending-action backend
Tasks:
1. Define event schema/types and append-only semantics.
2. Reuse suitable audit records.
3. Add missing transition events with actor/time/correlation/safe summary/evidence refs.
4. Add authorized, paginated timeline query.
5. Add backend-generated pending-action summaries.
6. Define transaction/outbox/reconciliation behavior.
7. Backfill only reconstructible history; label gaps rather than invent events.
8. Test concurrency, retries, unauthorized reads, missing events, and audit-write failures.

Exit: all new transitions emit expected events; state/timeline consistent; unauthorized access denied; actions agree with backend rules.

### Phase 5C — Workflow transparency UI
Tasks: timeline on requester/admin detail pages; current status/last update/action banner/block reason; show only persisted stages; loading/empty/error/unavailable states; pagination/evidence disclosures; accessibility and responsive tests.
Exit: users can tell current state, what happened, who acts next, and why blocked; no fake progress/success.

### Phase 5D — Role-specific workspaces and authorization
Tasks: finalize capability matrix; implement backend authorization; add reviewed role assignment/migration; build procurement queue and sourcing actions; Finance reconciliation/exception/simulation view; improve approver context/stale state; separate operations dashboard; test role × route × state combinations and signup escalation.
Exit: each role sees correct work; every write is server-authorized; admin does not gain approver powers; no unintended privilege migration.

### Phase 5E — Explainable decisions
Tasks: inspect current evaluation/comparison schemas; define factor codes/evidence refs; return structured deterministic factors; show hard gates/cost/delivery/warranty/compliance; show policy version/citations/stale state; add “How was this decided?” disclosures; isolate optional LLM paraphrase; golden-test pass/warning/block/tie/missing/expired/stale cases.
Exit: factors trace to evidence; verification states are clear; explanations cannot change outcomes; scores match stored calculations.

### Phase 5F — Metrics and alerts
Tasks: define metric catalogue/windows; replace process-local counters when durable reporting is required; add structured logs/correlation IDs; add safe liveness/readiness/dependency checks; add protected ops summary/queues/alerts; implement dedupe/cooldown/owner/runbook; test trigger/recovery/delivery failures; define retention/cardinality; specify audit fail-closed behavior.
Exit: operators trace alert → correlation ID → workflow; metrics persist or are labeled ephemeral; alerts are actionable; no secrets/payloads leak.

### Phase 5G — Secrets, backups, deployment, security
Tasks: select deployment topology; configure TLS/origins/CORS; move production secrets to a secret manager/protected environment; rotate exposed credentials; automate encrypted DB/private-file backups; agree RPO/RTO/retention; alert on backup freshness; perform isolated restore drill; configure security headers/rate limits; add dependency/secret scans; test auth/IDOR/uploads/prompt injection/payment-simulation boundaries; write deployment/rollback/incident/rotation/restore runbooks; stage acceptance test.
Exit: restore meets agreed RPO/RTO; secrets absent from source/frontend; production config differs from local defaults; no unaccepted critical/high findings.

### Phase 5H — Acceptance and rollout
Run all suites from clean checkout; role-specific browser E2E; mobile/keyboard/error states; review UI copy; security review; staged rollout/feature flag where available; monitor errors/latency/queue age; document rollback triggers and test rollback.
Exit: signed-off checklist, no safety regression, tested rollback, operator readiness.

## 15. Test strategy

### Backend
- event creation for every transition
- timeline ordering/pagination/ownership
- capability matrix, stale state/version, concurrency, idempotency
- audit write failure and recovery
- pending-action consistency
- deterministic vendor gates/score breakdown
- policy provenance/stale-policy behavior
- metric semantics and persistence
- alert deduplication/recovery
- safe health endpoint
- configuration/secret validation
- backup manifest/checksum and restore smoke tests

### Frontend
- timeline empty/normal/long/partial history
- pending vs blocked vs completed
- requester ownership, procurement permissions, Finance simulation wording, approver stale-policy confirmation
- explanation factors and evidence links
- metrics unavailable vs zero
- alert active/acknowledged/resolved
- timeout/session expiry/401/403/404/409/422/503
- responsive layout, keyboard focus, dialog behavior, text-labelled statuses

### E2E
1. Request submitted → timeline event appears.
2. Workflow passes/blocks/fails → actual persisted result appears.
3. Information requested → requester sees next action and resubmits if allowed.
4. Approval requested → approver sees it; unauthorized role cannot decide.
5. Policy change → stale approval explained and backend rejects invalid decision.
6. RFQ → quote submitted → unverified status → reviewer verification → explained comparison → recorded human decision.
7. Finance views reconciliation/simulation; safety banner remains and no real payment call exists.
8. Operational failure correlates to request and appropriate alert.
9. Isolated backup restore preserves representative records and files.

### Security/resilience
Test role escalation/IDOR, oversized/malicious PDFs, document prompt injection, log redaction, rate limits, dependency/secret scans, DB/audit outage, alert provider outage, partial backup/restore, concurrent approvals and quote revisions.

### Local validation
Run from repo root after confirming current scripts:
```powershell
python -m pytest -q
cd frontend
npm ci
npm test
npm run build
npm audit --omit=dev
```
Also run current lint/static/security checks and real-Mongo tests where available. Record actual results; do not copy previous PR counts as current evidence.

## 16. CI/CD and release

CI should install lockfile dependencies; run Python/frontend tests and production build; run lint/static checks and dependency/secret scans; reject committed `.env`/credentials; validate index/migration changes against test MongoDB; publish artifacts only after checks pass.

Deployment should have separate dev/staging/prod configuration, explicit frontend origin/CORS, TLS, backend-only secrets, readiness checks, release SHA/version, migration/rollback plan, post-deploy smoke tests, and no automatic enablement of supplier outreach, awards, dispatch, or real payments.

## 17. Suggested project structure

Adapt to the repository rather than forcing the exact tree.

```text
Docs/
  PHASE_5_PRODUCT_UX_REQUIREMENTS.md
  PHASE_5_ROLE_CAPABILITY_MATRIX.md
  PHASE_5_METRICS_AND_ALERTS.md
  PHASE_5_BACKUP_RESTORE_RUNBOOK.md
  PHASE_5_SECURITY_CHECKLIST.md
  PHASE_5_DEPLOYMENT_RUNBOOK.md

app/
  observability/
    correlation.py
    logging_config.py
    metrics.py
    alerts.py
  timeline/
    schemas.py
    service.py
    repository.py
  routers/
    operations.py

frontend/src/
  components/
    WorkflowTimeline.jsx
    PendingActionBanner.jsx
    StatusSummary.jsx
    DecisionExplanation.jsx
    EvidenceReference.jsx
    MetricCard.jsx
    AlertList.jsx
  pages/
    ProcurementWorkspacePage.jsx
    FinanceWorkspacePage.jsx
    OperationsDashboardPage.jsx
```

Only create modules that have an approved, concrete responsibility.

## 18. Priority and delivery plan

| Priority | Work | Why |
|---|---|---|
| P0 | Role/capability matrix and state audit | Prevents privilege expansion and UI/backend mismatch |
| P0 | Timeline/pending-action contract | Foundation for trustworthy status |
| P0 | Security/secrets/backup inventory | Identifies production blockers early |
| P1 | Timeline UI | Immediate user value |
| P1 | Explainable policy/vendor decisions | Improves human review |
| P1 | Correlation IDs and structured logs | Debugging across components |
| P1 | Durable metrics and high-signal alerts | Detect failures/backlogs |
| P1 | Backup automation and restore drill | Protects business data |
| P2 | Dedicated procurement/Finance workspaces | Depends on role/capability decision |
| P2 | Full operational dashboard and alert acknowledgement | Depends on metric/alert semantics |
| P2 | Staging security validation and rollout | Final release gate |

Suggested PR sequence: (1) discovery/contracts, (2) timeline backend, (3) timeline UI, (4) RBAC/role views, (5) explanation contracts/UI, (6) metrics/alerts, (7) backups/security/deployment, (8) final acceptance.

## 19. Risks and mitigations

| Risk | Mitigation |
|---|---|
| UI offers invalid action | Derive actions from backend state; test against transitions |
| New roles gain unintended powers | Capability matrix, deny-by-default migration, negative API tests |
| Timeline diverges from state | Append-only events, consistency strategy, idempotency/reconciliation |
| LLM invents explanations | Deterministic structured factors; paraphrase cannot change facts/outcome |
| Dashboard shows false zero | Include availability/freshness; distinguish zero from unknown |
| Alert fatigue | Thresholds, dedupe, cooldown, owner, runbook |
| Metrics leak identifiers | Low-cardinality labels, minimization, retention/access control |
| Backup cannot restore private files | Back up files and DB references, checksum and restore drill |
| Secrets leak in bundle/logs | Secret scanning, no `VITE_*` secrets, redaction tests |
| Telemetry outage breaks workflow | Separate best-effort telemetry from required audit writes |
| Scope grows too large | Small PRs, explicit exit criteria, prioritize foundations |

## 20. Definition of Done

### Workflow transparency
- [ ] Status, last update, timeline, and next action visible for supported items.
- [ ] Timeline events persisted and authorized.
- [ ] Pending, blocked, failed, and completed are distinct.
- [ ] No fabricated progress or optimistic success.

### Roles
- [ ] Employee, procurement, Finance, approver, admin responsibilities documented.
- [ ] Backend capability checks are authoritative.
- [ ] No new privilege without explicit assignment/migration.
- [ ] Ownership and self-approval negative tests pass.

### Explanations
- [ ] Policy outcomes link to rule/version/evidence.
- [ ] Vendor comparison shows gates, costs, and missing/unverified evidence.
- [ ] Explanations match deterministic backend outputs.
- [ ] Score claims are tested and human decisions remain distinct.

### Observability
- [ ] Correlation IDs connect API logs to workflow events.
- [ ] Metric definitions/windows/sources documented.
- [ ] Required metrics survive restarts or are labeled ephemeral.
- [ ] Alerts are deduplicated, actionable, owned, and tested.
- [ ] Health endpoints do not leak internals.

### Security and recovery
- [ ] Secrets absent from source and frontend bundle.
- [ ] Production configuration isolated from development.
- [ ] Auth, authorization, upload, injection, and dependency tests completed.
- [ ] Encrypted backups and retention configured.
- [ ] Restore drill meets agreed RPO/RTO.
- [ ] Incident, rotation, deployment, rollback, and restore runbooks exist.
- [ ] No critical/high finding remains without documented risk acceptance.

### Safety
- [ ] Vendor outreach and awards remain disabled.
- [ ] PO dispatch remains disabled.
- [ ] Payment remains simulation-only; no money is transferred.
- [ ] Consequential actions are validated and audited by backend.

## 21. Decisions to resolve in Phase 5A

Record each answer in an architecture decision record:
1. Deployment target for React, FastAPI, MongoDB, and private files.
2. Whether procurement/Finance are separate backend roles or capabilities.
3. Who assigns roles and how changes are audited.
4. Whether existing audit records suffice or timeline/outbox is needed.
5. Monitoring/metrics destination.
6. Alert destination and on-call ownership.
7. Backup method, encryption, retention, and storage location.
8. RPO/RTO.
9. Log, event, document, and backup retention/privacy.
10. Required CI and staging security tests.
11. SLOs after baseline measurement.
12. Whether current hard-gate/cost comparison is sufficient or weighted ranking is required.

## 22. First implementation ticket

**Title:** `Phase 5A: Audit workflow states, roles, events, and production-readiness gaps`

Scope:
1. Inspect current `master` without changing business logic.
2. Document workflow states and transitions.
3. Map transition code to audit/event writes.
4. Build route × role × action authorization matrix.
5. Identify process-local metrics and operational signals.
6. Inventory secrets, collections, private file storage, and backup gaps.
7. Propose RPO/RTO and deployment decisions for owner approval.
8. Turn findings into small tickets with priority and acceptance criteria.

Acceptance:
- Findings reference actual repository modules/routes.
- Unknown behavior is marked “needs verification”, not guessed.
- No code or permission changes in this discovery ticket.
- Next implementation PR has clear scope and test plan.

## 23. Final recommendation

Treat Phase 5 as a reliability and trust initiative, not just a UI redesign.

**Discover states and permissions → persist a trustworthy timeline → show pending actions → implement role-specific views with server-enforced capabilities → explain decisions from evidence → add durable monitoring and alerts → validate secrets/backups/restore/security → release through staging.**

This sequence improves usability without weakening backend controls or enabling unauthorized procurement/payment execution.
