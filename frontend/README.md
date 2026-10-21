# WorkplaceOps AI — React UI

A small, white-first React interface for the existing FastAPI backend (spec: [`Docs/REACT_UI_MIGRATION_AND_DESIGN.md`](../Docs/REACT_UI_MIGRATION_AND_DESIGN.md)).
The backend stays authoritative for every permission, workflow rule, approval and financial safeguard; the UI only displays server results.
This is the project's only UI. The previous Streamlit UI was removed once the React UI reached feature parity (2026-10-21).

## Run locally

```powershell
# Terminal 1 — API (repo root)
python -m uvicorn app.main:app --reload --port 8000

# Terminal 2 — React UI
cd frontend
copy .env.example .env.local   # optional; defaults to http://localhost:8000
npm install
npm run dev                    # http://localhost:5173
```

- `VITE_API_BASE_URL` is the only frontend setting. Never put secrets in `VITE_*` variables — they are shipped to the browser.
- The API allows the UI origin through `CORS_ALLOWED_ORIGINS` (backend env, comma-separated, default `http://localhost:5173,http://127.0.0.1:5173`; `*` is ignored; no credentials mode — the UI sends a bearer token).
- Sign-in uses the existing `/api/auth/login` (bearer token kept in `sessionStorage`, cleared when the tab closes). Account creation uses `/api/auth/signup` (requester accounts only).

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server on port 5173 |
| `npm run build` | Production build to `dist/` |
| `npm run preview` | Serve the production build |
| `npm test` | Vitest + Testing Library (jsdom, no network — every request is mocked) |

## Structure

```text
src/
  api/          client.js (fetch wrapper, safe errors, bearer token), auth, requests, approvals, policies, vendorSourcing, payments
  components/   AppLayout (sidebar + top bar), PageHeader, DataTable, StatusBadge, States (loading/empty/error/notice),
                ConfirmDialog, Tabs, WorkflowResult
  lib/          auth context, useApi/useAction hooks, formatting, health (feature flags)
  pages/        one file per screen (+ __tests__/)
  styles/       tokens.css (design tokens), global.css, components.css, pages/*.css
  routes.js     role-based navigation (presentation only)
```

## Pages by role

| Role | Pages |
|---|---|
| Requester | My requests, New request, Request details (edit & resubmit when allowed, notes) |
| Admin | Overview, All requests, Request details (run/re-run workflow, readable result steps, approval panel, close, invoice match, audit), Vendor sourcing (directory, candidates), RFQs & quotes (create, quotes, PDF upload, verify, compare, decision), Policy library, Policy rules, Approvals (view only), Payments (Simulation) — shown only when `/api/health` reports `payments_simulation_enabled` |
| Approver | Pending approvals, Approval history (approve / reject / request changes with confirmation), Policy review (rules + rate cards) |

## Design rules (see the spec)

White surfaces, neutral `#E5E7EB` borders, `#172033` text, one blue accent (`#2563EB`); muted status badges that always carry text;
one primary action per screen; consequential actions confirm (with a reason when the API requires one) and show the result only after
the server confirms; evidence, citations, audit and raw data sit behind disclosures; plain-language labels.
Safety copy is shown where relevant: draft PO is not an order, approval doesn't send a PO or move money, sourcing is decision support
only, payments are simulation only.
