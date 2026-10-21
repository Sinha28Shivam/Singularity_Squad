# WorkplaceOps AI — React UI Migration & Minimal Design Specification

**Status:** Proposed implementation specification  
**Target:** Replace the Streamlit presentation layer with a React.js UI while retaining the existing FastAPI backend and MongoDB persistence.  
**Design direction:** White-first, calm, minimal, readable, and understandable to non-technical users.

## 1. Goal

Build a lightweight web interface for WorkplaceOps AI that helps users submit and track workplace requests, admins manage requests and sourcing, and approvers review decisions. The UI should explain what is happening in plain language without exposing users to unnecessary agent or implementation details.

This document specifies the intended changes and visual behavior. It does not claim that the React application has already been implemented.

## 2. Core principles

1. **White-first interface:** white page and panel surfaces, subtle neutral borders, dark readable text, and one restrained blue accent.
2. **Plain language:** use labels such as “Requests”, “Vendor quotes”, and “Needs your review” rather than internal agent names or technical jargon.
3. **Progressive detail:** show the decision or current status first; put evidence, policy citations, technical diagnostics, and audit details behind expandable sections or detail views.
4. **Consistent actions:** one clear primary action per screen; destructive or consequential actions require confirmation.
5. **Accessible by default:** semantic headings, labelled inputs, keyboard navigation, visible focus, adequate contrast, and status labels that do not rely on color alone.
6. **Responsive layout:** work well on desktop and remain usable on tablet and mobile widths.
7. **Backend remains authoritative:** all permission checks, workflow validation, policy evaluation, approval eligibility, and financial safeguards stay in FastAPI.

## 3. Proposed technology

- **React** for the UI.
- **Vite** for local development and production builds.
- **JavaScript** (no TypeScript requirement for the first iteration).
- **React Router** for page navigation.
- **Plain CSS or CSS modules** for styling.
- **Lucide React** for a small, consistent icon set.
- **Fetch or a small shared API client** for FastAPI requests.

Avoid introducing a large component framework, global state library, or animation library until there is a demonstrated need.

## 4. Visual design system

### Color palette

| Purpose | Value | Guidance |
|---|---|---|
| Main page | `#FFFFFF` | Default background |
| Subtle surface | `#F8FAFC` | Search areas, quiet panels, table headers |
| Primary text | `#172033` | Headings and body text |
| Secondary text | `#64748B` | Supporting labels and descriptions |
| Borders | `#E5E7EB` | Cards, dividers, inputs |
| Primary action | `#2563EB` | Main buttons and selected navigation |
| Success | `#15803D` | Completed or approved states, with text label |
| Warning | `#A16207` | Needs review or attention |
| Error | `#B91C1C` | Errors and rejected states |

Use color sparingly. Avoid gradients, glass effects, large colored backgrounds, excessive shadows, and decorative charts that do not help a user make a decision.

### Typography and spacing

- Use a system sans-serif stack (for example, Inter when already available, otherwise the platform system font).
- Body text: approximately 14–16 px; page title: 24–28 px; section title: 16–20 px.
- Use a consistent 4 px spacing scale, with 16–24 px as the common gap between content sections.
- Prefer sentence case and short, descriptive labels.
- Keep text line lengths comfortable; do not compress important information into tiny labels.

### Components

- Buttons: compact, clear text labels, consistent height, visible focus state.
- Inputs: persistent labels, short helper text, inline validation messages.
- Cards: white surface, thin neutral border, small radius, minimal or no shadow.
- Tables: readable column headings, comfortable row height, restrained separators, empty states.
- Status badges: muted backgrounds plus explicit text such as “Pending review”; never color alone.
- Dialogs: use for confirmation and focused tasks, not for long workflows that deserve a full page.
- Loading and error states: explain what happened and provide a retry path where appropriate.

## 5. Application layout and navigation

### Global shell

- A narrow left sidebar on desktop; collapsible navigation on smaller screens.
- Header containing the WorkplaceOps AI name, current page title or breadcrumb, and a compact user/profile menu.
- Main content area with a sensible maximum width and consistent horizontal padding.
- Avoid adding a dashboard full of charts unless they answer a concrete operational question.

### Navigation by role

The backend remains responsible for access control. The UI can display navigation based on the authenticated role for clarity, but this is not a security boundary.

**User**
- My Requests
- New Request
- Account / Sign out

**Admin**
- Overview
- All Requests
- Vendor Sourcing
- RFQs & Quotes
- Policy Library
- Policy Rules
- Payments (Simulation), only when enabled by configuration

**Approver**
- Pending Approvals
- Approval History
- Policy Review, where applicable
- Account / Sign out

If a role is not authorized for a page or API, the backend must deny the request even if the route is opened manually.

## 6. Screen specifications

### 6.1 Sign in and account creation

- Center a simple form on a white background.
- Show product name, one-sentence purpose, and only the required fields.
- Provide clear validation and a readable authentication error.
- Never display secrets, tokens, or server exception details.
- Keep account creation available only through the existing supported registration behavior.

### 6.2 User overview — My Requests

Display:
- Page heading: “My requests”.
- Primary action: “New request”.
- Search or simple status filter when useful.
- Table/list columns: Request ID, summary, submitted date, current status, next action.
- Empty state: explain that no requests exist yet and offer “Create a request”.
- Request detail view: request summary, workflow outcome, missing information or review reasons, approval state, and edit/resubmit action when the backend permits it.

Avoid making users understand internal multi-agent architecture to follow a request.

### 6.3 New request form

Use a clear, single-column form or two-column layout only for short related fields. Group fields by meaning:
1. What is needed?
2. When and where?
3. Budget and quantity.
4. Additional context.

- Keep required fields visibly marked.
- Validate dates, amounts, and required fields before submission where possible; backend validation remains authoritative.
- Preserve entered data after recoverable errors.
- Confirm submission and show the created request ID.
- Do not imply that submitting a request places an order.

### 6.4 Admin overview and request queue

Keep the overview operational rather than decorative:
- Small summary counts such as “Needs review”, “In progress”, and “Completed” only when supported by API data.
- Main table with request ID, requester, summary, submitted date, status, and action.
- Filters for status and search.
- Clear loading, empty, and error states.
- A request detail page with a “Run workflow” or “Re-run workflow” action only when the existing API and request state permit it.
- Show agent workflow results as a readable sequence of steps, each with outcome, explanation, and relevant evidence. Hide raw payloads by default.

Admins must not gain approval privileges merely because the UI exposes request management.

### 6.5 Workflow result and policy evidence

Present the high-level result first:
- Overall outcome: Passed, Needs review, or Blocked.
- Plain-language explanation and list of unresolved items.
- Relevant policy citations and source details in expandable sections.
- Clearly distinguish verified data from extracted or unverified content.
- Display failures in plain language and include a request/correlation ID if available for support.

Do not invent progress, results, policy evidence, or successful completion states in the UI.

### 6.6 Approvals

- A queue of items requiring the approver's attention.
- Detail view shows request, draft PO summary, reasons for review, policy-set/version context where applicable, and supporting evidence.
- Actions: Approve, Reject, or Request changes, only as supported by the backend.
- Require an explicit confirmation for consequential decisions; request a reason where the API requires one.
- Show the resulting decision only after the server confirms it.
- Clearly state that approval is internal authorization and does not dispatch a PO or transfer money.
- Preserve the backend's prohibition on self-approval.

### 6.7 Vendor sourcing

Use a tabbed or sub-navigation layout with a small number of clear sections:
- **Vendor directory:** search and filter existing vendors.
- **Candidates:** review submitted candidate records and their status.
- **Candidate details:** vendor name, services, contact information, provenance, review history, and approve/reject/suspend controls as allowed by API.
- **RFQs:** list and create draft sourcing records.
- **Quotes:** structured quote entry or PDF upload, verification status, and comparison.

For PDF upload, show file constraints and upload progress. Explain that extracted terms are unverified until a reviewer verifies them. Use a clear comparison table with total cost, delivery, warranty, mandatory compliance, budget status, and eligibility. Label the result “Decision support only”; do not present it as an automatic award.

External vendor discovery and supplier contact remain disabled until separately implemented and authorized.

### 6.8 Policy library and policy rules

- List documents and rules with simple status, title, updated date, and available actions.
- Document upload screen shows allowed formats and useful size constraints from the API.
- Display source citations, extracted candidate rules, and review flags.
- Candidate policy rules require authorized human review before activation.
- Make active versus pending/rejected rules visually distinct and text-labelled.
- Explain that a change to active policy versions may invalidate a prior approval, consistent with backend behavior.

### 6.9 Payments (Simulation)

Only show the page if the feature is enabled and the backend allows the current user to access it.

- Use a persistent, visible banner: **Simulation only — no money is transferred.**
- Show the simulated amount, currency, eligibility result, and receipt/reference returned by the backend.
- Require confirmation before creating a simulation instruction.
- Display the backend's returned simulation status and audit reference.
- Never include a real payment gateway, real bank integration, or a control that implies a real transfer is possible.

## 7. Frontend structure

Suggested initial structure:

```text
frontend/
  index.html
  package.json
  vite.config.js
  .env.example
  src/
    main.jsx
    App.jsx
    api/
      client.js
      auth.js
      requests.js
      approvals.js
      vendorSourcing.js
      policies.js
      payments.js
    components/
      AppLayout.jsx
      Sidebar.jsx
      PageHeader.jsx
      StatusBadge.jsx
      DataTable.jsx
      EmptyState.jsx
      ErrorState.jsx
      LoadingState.jsx
      ConfirmDialog.jsx
    pages/
      LoginPage.jsx
      MyRequestsPage.jsx
      NewRequestPage.jsx
      RequestDetailPage.jsx
      AdminOverviewPage.jsx
      RequestQueuePage.jsx
      ApprovalsPage.jsx
      VendorDirectoryPage.jsx
      CandidatesPage.jsx
      RfqsPage.jsx
      QuoteComparisonPage.jsx
      PolicyLibraryPage.jsx
      PolicyRulesPage.jsx
      SimulatedPaymentsPage.jsx
    styles/
      tokens.css
      global.css
      components.css
```

This is a starting point, not a requirement to create every page at once. Implement only the pages needed for each migration increment.

## 8. API integration and authentication

- Use one shared API client with a configurable base URL, defaulting locally to the FastAPI server at `http://localhost:8000`.
- Read the backend's actual OpenAPI schema and current Streamlit API usage before wiring each page. Do not invent endpoints or response fields.
- Handle JSON and PDF upload endpoints correctly; the existing PDF quote endpoint accepts raw `application/pdf` request bodies, not necessarily a multipart form.
- Centralize HTTP error handling, unauthorized responses, request timeouts, and safe user-facing messages.
- Follow the existing backend authentication/session mechanism. Do not store credentials or privileged secrets in source code or Vite-exposed environment variables.
- Configure CORS on FastAPI for the specific local/deployed frontend origins. Do not use an unrestricted wildcard with credentials.
- Never trust client-supplied role names or client-only permission checks.
- Avoid logging tokens, passwords, sensitive request contents, or PDF contents in the browser console.

## 9. Migration plan

### Phase A — Inspect and establish the frontend

- Review `ui/app.py`, existing API calls, authentication flow, and FastAPI OpenAPI endpoints.
- Add the Vite React project in `frontend/`.
- Establish the design tokens, app shell, routing, shared API client, and common states.
- Add local setup instructions and an environment example.

### Phase B — Core request journey

- Implement login/account flow using existing backend behavior.
- Implement My Requests, New Request, request detail, and admin request queue.
- Connect all screens to real API responses.
- Test role-specific navigation and backend authorization.

### Phase C — Approval and policy workflow

- Implement approval queue, decision confirmation, workflow results, policy evidence, policy library, and policy rules.
- Verify that policy-version and self-approval safeguards remain unchanged.

### Phase D — Vendor sourcing and RFQ

- Implement vendor directory, candidate review, RFQ creation/detail, structured/PDF quote submission, quote verification, and comparison.
- Keep sourcing actions as drafts and decision support; no external messages or awards.

### Phase E — Verification and cutover

- Run the existing Python test suite and add/adjust backend tests only where needed.
- Add frontend tests for key user journeys and error/loading states.
- Verify production build, API origin/CORS, authentication, and deployment configuration.
- Keep Streamlit available during the transition. Remove it only after feature parity is reviewed and the replacement is accepted.

## 10. Acceptance criteria

### Visual and usability
- [ ] Default screens are white or near-white, with neutral borders and one restrained accent color.
- [ ] A non-technical user can find their requests, current status, and next action without knowing agent terminology.
- [ ] Forms have clear labels, validation, helpful error messages, and preserve data after recoverable errors.
- [ ] Tables support readable status labels, search/filter where needed, and useful empty/loading/error states.
- [ ] Layout works at desktop and mobile widths, with keyboard-accessible navigation and dialogs.
- [ ] No decorative metrics or fake records are shown as live data.

### Functional and security
- [ ] All data is obtained from the existing FastAPI backend.
- [ ] Existing role boundaries and backend authorization checks remain authoritative.
- [ ] Users cannot view another user's private requests unless the backend permits it.
- [ ] Approvers cannot approve their own requests.
- [ ] Workflow, policy, and approval decisions are shown only after server responses.
- [ ] Candidate, RFQ, and quote workflows respect verification and review states.
- [ ] No supplier outreach, contract award, PO dispatch, or real payment is added by the frontend migration.
- [ ] Payment screens clearly state simulation mode and no money transferred.
- [x] Existing Streamlit UI remained usable until the React replacement was validated (removed after validation, 2026-10-21).
- [ ] Frontend production build and Python tests are run and their actual results recorded; do not claim success before execution.

## 11. Out of scope for the first UI release

- Replacing FastAPI or MongoDB.
- Rewriting agents or procurement business rules.
- Adding a new identity provider.
- Enabling external supplier discovery, supplier email, contract award, or PO dispatch.
- Integrating a real payment gateway.
- Adding complex analytics, animation, or a large UI framework.
- Removing Streamlit before the React UI has been tested and accepted.

## 12. Developer run plan

The intended local development workflow is:

```powershell
# Terminal 1 — existing API
python -m uvicorn app.main:app --reload --port 8000

# Terminal 2 — React UI
cd frontend
npm install
npm run dev
```

The exact scripts and environment variables should be confirmed in the created `package.json` and `.env.example`. Keep API keys and backend secrets in the backend environment, never in frontend variables prefixed with `VITE_`.

## Final design decision

Build a **small, white-first React application** with a stable navigation shell, clear page titles, simple forms, readable tables, muted status indicators, and detail views for complex information. Keep the backend and its safeguards unchanged, migrate feature-by-feature, and retain Streamlit until the React application demonstrates equivalent behavior.

---

## Implementation status (2026-10-21, branch `feat/react-minimal-ui`)

Phases A–E are complete, including the cutover. The React UI in `frontend/` is now the only UI. After the API gaps were closed and Streamlit-only features were ported, the Streamlit UI (`ui/`), its 7 AppTest files and the `streamlit`/`requests` dependencies were removed (see *Completion* below).

### What was built

- **Foundation (Phase A):**
  - **Tooling:** Vite 8, React 19, React Router 7, lucide-react and plain CSS. There is no UI framework, state library or animation library.
  - **Design:** design tokens (§4 palette and 4 px spacing scale), an app shell with a narrow sidebar that collapses into a menu below 900 px, and a top bar.
  - **API client:** one shared client with a bearer token kept in `sessionStorage`, a timeout, and safe error messages that never show HTML, tracebacks or raw bodies.
  - **Shared components:** auth context, `useApi`, `PageHeader`, `DataTable`, `StatusBadge` (text label always shown), loading/empty/error states, an accessible `ConfirmDialog` (closes only after the server confirms) and accessible `Tabs`.
- **Requests (Phase B):** sign in and create account; My requests; New request, grouped by meaning; request details with edit & resubmit when the backend allows it; admin overview using server counts only; all requests; admin request details. Admin details include run/re-run with optional invoice, the workflow result as plain-language steps with evidence behind disclosures, an approval panel (admins can't approve), close with reason, notes, invoice match and audit.
- **Approvals and policy (Phase C):** pending approvals and history; approval details with approve / reject / request changes; policy library (multipart upload, reprocess, archive, extract rules); policy rules (active set, create, validate/approve/reject); approver policy review (rules and rate cards).
  - Decisions use a stable idempotency key, the request version and the snapshot hash. The page honours `can_decide` and `stale_policy`, and admins see it read-only.
- **Sourcing and payments (Phase D):** vendor directory (internal catalog kept separate from the candidate registry); candidates; RFQ list and create from a validated ticket; RFQ details (structured and raw-PDF quotes, revise, verify with a submitted-vs-verified diff, untrusted extracted text, compare plus human decision, close/cancel, events); Payments (Simulation).
  - The PDF upload is checked for type and size in the browser before sending.
  - Payments appear only when `/api/health` reports `payments_simulation_enabled`, and keep their banner and confirmation.

### Backend changes (minimal and additive)

- **CORS:** `CORSMiddleware` with an explicit allow-list (`CORS_ALLOWED_ORIGINS`, default the Vite dev origins). `*` is ignored and credentials are off.
- **Payments flag:** `GET /api/health` now also returns `payments_simulation_enabled`, a non-secret boolean, so navigation can hide the page. The payments API still enforces the setting.
- **Tests:** `tests/test_react_ui_backend_support.py`.

### Evidence (actually executed)

| Check | Result |
|---|---|
| `npm test` (Vitest + Testing Library, 18 files) | **140 passed** |
| `npm run build` | succeeded (main bundle ≈ 287 kB, 91.5 kB gzip; pages lazy-loaded) |
| `npm audit --omit=dev` | 0 vulnerabilities |
| `python -m pytest -q` | **1032 passed** (1029 + 3 new backend-support tests) |
| Real-browser E2E with Playwright Chromium against Vite, uvicorn and a throwaway MongoDB | **23/23** (details below) |
| Wording scan of the source | no "Paid", "Awarded", "PO sent" or `console.log`; no `localStorage` token storage; no `dangerouslySetInnerHTML` |

**Browser E2E coverage:**
- white page background;
- a requester creates a request and sees its ID, and the request appears in My requests;
- an admin runs the workflow and sees a plain-language result, with no Approve control;
- an approver approves through the confirmation dialog, and the result appears only after the server confirms, with the safety copy;
- the requester then sees "Approved";
- at mobile width (390 px) there is no page overflow and the menu opens;
- the payments banner shows, and the vendors, RFQs, policy library and policy rules pages render;
- keyboard focus is visible;
- a requester opening an admin URL is redirected;
- no console errors appear in any role.

### Completion: gaps closed, parity and cutover (2026-10-21)

Every follow-up from the first release is closed.

**API gaps closed (additive, backward compatible; tests in `tests/test_ui_support_endpoints.py`):**

| Gap | Resolution |
|---|---|
| Ticket search only covered the loaded page | `GET /api/my/tickets` and `GET /api/admin/tickets` accept `q`, `status`, `limit` and `offset`, and return `total` and `next_offset`. `q` is matched as a case-insensitive literal (regex-escaped) against the ticket ID and title, and for admins also the owner. Requesters still only see their own tickets. The UI search is server-side, debounced, with "Load more" and "Showing N of total". |
| Upload limits not published | `GET /api/policy-documents/limits` returns `upload_max_mb`, `upload_batch_max_mb`, `max_files_per_batch`, `max_pdf_pages` and `allowed_content_types`. The upload screen shows them and checks files before upload; the server still enforces them. |
| Rule facts hard-coded in the UI | `GET /api/policy-rules/facts` returns the facts (name, type, description, currency) and the operators, from the backend registry. The create-rule form uses it. |
| Candidate transitions duplicated in the UI | Candidate responses include `allowed_decisions`, computed from the repository's transition table. There is also `GET /api/vendor-sourcing/candidates/transitions`. The decision buttons follow the server. |
| No single comparison / score breakdown | `GET /api/vendor-sourcing/rfqs/{rfq_id}/comparisons/{comparison_id}` returns the stored comparison with per-row `score.components`, `weights` and `raw`. Each row has a "How this score was calculated" disclosure, labelled *explanatory only*. |
| Two error shapes | Kept as is: `{detail}` for most endpoints, `{error, message, reasons}` for payments. The shared client handles both. |

**Streamlit features ported before removal:**
- **Policy search** (`POST /api/policy/search`): a Search tab in the policy library with filters, citations (file, version, page, section), score and retrieval-mode notices.
- **Invoice re-check** (`POST /api/reconcile`): an ad-hoc check on the admin request page, labelled "Preview only — not saved".
- **Sourcing metrics** (`GET /api/vendor-sourcing/metrics`): an activity section that loads on demand.

**API coverage check:** every endpoint the Streamlit UI called is also called by the React UI. The only exceptions are `/api/health` (used by the menu) and the legacy `/api/workflow` and `/api/samples` demo endpoints, which the portal flow doesn't need.

**Cutover:**
- **Removed:** `ui/` and `tests/test_ui_{approvals,payments,policy,portal,rules,tickets,vendor_sourcing}.py`.
- **Dependencies:** `streamlit` and `requests` were dropped from `requirements.txt`; nothing else imported them, and tests use `httpx`.
- **Rollback:** `git revert` of the cutover commit restores Streamlit.

**Evidence after cutover (actually executed):**

| Check | Result |
|---|---|
| `npm test` | **165 passed** (21 files) |
| `npm run build` / `npm audit --omit=dev` | succeeded / 0 vulnerabilities |
| `python -m pytest -q` (Streamlit tests removed) | **906 passed**, 0 failed |
| Real-browser E2E (Playwright Chromium, Vite, uvicorn and a throwaway MongoDB with the policy PDFs seeded) | **28/28**: the 23 earlier checks plus server-side search finding a ticket, the invoice preview panel, policy search returning cited results, the sourcing activity section, and no console errors |

### Known limitations (accepted)

- **Approver role:** quote verification stays an admin task; a separate procurement-reviewer role is not part of this migration.
- **Metrics are per process:** sourcing and payment metrics reset when the API restarts.
- **Deployment:** production hosting (serving `frontend/dist` and setting `CORS_ALLOWED_ORIGINS` to the deployed origin) is not covered here. Run `npm run build`, and configure the API's allowed origins for the deployed host.
