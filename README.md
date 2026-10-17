# WorkplaceOps AI — Request-to-Payment Copilot (hackathon MVP)

Multi-agent workflow: **Intake → Policy Advisor (local BM25 RAG + citations) → Vendor Matcher (re-plans on conflict) → Procurement (deterministic costing, draft PO) → Billing Guardian (3-way invoice match, duplicate check) → Critic → Human approval gate.**
All data is synthetic. Draft PO only: the system makes no purchase and approves or pays nothing. See [the guide](Docs/WorkplaceOps_AI_Hackathon_Guide.md).

Execution flow, function call sequence and file dependencies: see [Docs/flow.md](Docs/flow.md). Phase specs live in [Docs/](Docs/), including [Phase 2 Approval Safety](Docs/PHASE_2_APPROVAL_SAFETY.md).

**Three roles (one Streamlit app):**
- **Users** sign up, fill in an event request form (category, catering service, date, attendees, budget, location, notes) and track their own tickets: status, outcome, approval panel, and edit & resubmit when more info or changes are requested.
- **Admins** log in with the account from `.env` and get a queue of all tickets. They run or re-run the multi-agent workflow (with a demo invoice) and see every result tab. They can reject hard-blocked tickets, but they **cannot approve**.
- **Approvers** (server-configured via `APPROVER_ACCOUNTS`) decide approval requests on the **Approvals** page: approve, reject or request changes, with a confirmation step. Nobody can approve their own request.
- **Policy library (RAG):** admins upload multiple policy, vendor and rate-card PDFs. Each ticket run retrieves cited evidence (file, page, section), checks quotes against verified rate cards with Decimal arithmetic, and adds review reasons to the approval request. Approvers verify extracted rate rows. The 3 PDFs in `data/policies` are ingested automatically on first start.
- **Dynamic policy engine (Phase 3):** business rules are reviewed, versioned data rather than code. Admins extract candidate rules from ready PDFs (LLM with deterministic fallback, cited evidence, prompt-injection flags). Approvers validate and approve or reject them; nothing activates automatically. Every ticket run evaluates the active policy-set version deterministically, and approvals are bound to that version, so a policy change makes them stale.
- Every draft PO needs an approver's sign-off. Approval records an internal authorization only: the PO stays a draft, and no order or payment is executed.
- **Payments (Simulation) — no money is ever transferred:** for an approved ticket with a matched invoice, an admin can create one **simulated** payment instruction on the "Payments (Simulation)" page. The backend re-checks eligibility on every request: approval current and bound to the active policy set, invoice matched, no duplicate or policy block, canonical Decimal amount, allow-listed currency. It is idempotent (same key → same payment; changed payload → 409) and fully audited. A local deterministic simulator returns an "Accepted by simulator" receipt with a fake `PAY-SIM-…` reference. There is no bank or gateway SDK and no network call, and every response says `mode: "SIMULATION"`, `money_transferred: false`. Disabled unless `PAYMENT_MODE=simulation` and `PAYMENT_SIMULATION_ENABLED=true`. Approvers can view payments read-only; users can't see them.

## Run
```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000          # API (docs at /docs)
python -m streamlit run ui\streamlit_app.py         # UI at http://localhost:8501 (log in as admin, or Create account)
python -m pytest -q                                 # tests (mongomock; real-MongoDB integration + restart tests run if MongoDB is up)
```

**Port already in use (`[Errno 10048]`)?** Another server is already on that port. Either stop it or use a different port:
```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | Select OwningProcess   # find the PID
Stop-Process -Id <PID>
# or: python -m uvicorn app.main:app --port 8001  and set $env:WORKPLACEOPS_API="http://127.0.0.1:8001" before starting Streamlit
```

**Before committing:** `git check-ignore -v .env` must print the `.env` rule, and `git ls-files .env` must print nothing. Keep config and docs saved as UTF-8 without a BOM.

`.env` settings:
- `API_KEY` or `OPENAI_API_KEY` (required for LLM mode).
- `OPENAI_MODEL` (optional, default `gpt-4o-mini`).
- `OPENAI_BASE_URL` (optional, for Azure/OpenAI-compatible endpoints).
- `WORKPLACEOPS_OFFLINE=1` forces deterministic mode.
- `MONGODB_URI` (default `mongodb://localhost:27017/`), `MONGODB_DATABASE` (default `workplaceops`).
- `ADMIN_USERNAME`, `ADMIN_PASSWORD`: the admin account is created or updated at API startup. If unset there is no admin; there is no default password.
- `POLICY_RAG_ENABLED` (default on), `POLICY_RAG_SEED` (default on), `POLICY_STORE_DIR` (default `data/policy_store`, git-ignored). Thresholds live in `data/policy_rag_config.json`. Embeddings use the OpenAI key (`text-embedding-3-small`); offline, the system falls back to local vectors.
- `POLICY_RULES_ENABLED` (default on): evaluate the active rule set on each run. `POLICY_RULES_BOOTSTRAP` (default on): load `data/policy_rules_seed.json` at startup.
- `PAYMENT_MODE` (only `simulation` is valid; unset or anything else keeps the simulator off), `PAYMENT_SIMULATION_ENABLED` (default `false`), `PAYMENT_ALLOWED_CURRENCIES` (default `INR`), `PAYMENT_SIMULATOR_PROFILE` (`normal` | `processing` | `transient_failure`), and optionally `PAYMENT_INITIATOR_ROLES` (default `admin`). There is no real-payment mode.
- `APPROVER_ACCOUNTS`: approver accounts as `user:password[,user2:password2]`, created at API startup. Demo auth only; approvers can't self-register, and an existing user or admin is never escalated.
- `SESSION_TTL_HOURS` (default 8): login session lifetime. Optional: `ADMIN_NAME`, `ADMIN_EMAIL`.
- `WORKPLACEOPS_PERSISTENCE`: `required` (default; DB write failures return 503) or `disabled` (no database).

See [.env.example](.env.example). Every workflow run is saved to MongoDB as a ticket (`tickets`, `purchase_orders`, `agent_logs`). You can reload it after a restart with `GET /api/tickets/{ticket_id}` and `/audit`, or from the UI's **Saved tickets** panel.

The LLM handles only language work: extraction and summaries. All arithmetic and pass/fail checks run as deterministic code.

## Demo scenarios (demo date 2026-10-17)
| Sample | Invoice | Expected |
|---|---|---|
| happy_path | INV-5001 | pass. Preferred vendor unavailable → re-planned to VEN-002, ₹76,125 |
| happy_path | INV-5002 | blocked. Rate 720≠700, 95 delivered vs 100 billed |
| happy_path | INV-4987 | blocked. Duplicate invoice (FIN-5.4) |
| over_budget | — | blocked. ₹90,825 > ₹60,000, no PO |
| too_soon | — | blocked. Lead time < 5 business days (EVT-2.2) |
| missing_info | — | needs_clarification |

## Layout
- `app/agents/`: one module per agent.
- `app/orchestrator.py`: workflow state and audit trail.
- `app/main.py`: FastAPI app.
- `app/routers/`: auth, user (`/api/my/*`) and admin (`/api/admin/*`) endpoints.
- `app/auth/`: password hashing, sessions, `current_user` / `require_admin`.
- `app/services/`: ticket service (submit, run, decide, notes, resubmit; persistence).
- `app/policy_rag/`: PDF ingestion, chunking, embeddings, hybrid retrieval, rate cards and policy evaluation. Routes: `app/routers/policy_documents.py`, `app/routers/policy_search.py`.
- `app/policy_rules/`: rule schema and restricted language, fact registry, evaluator, registry (lifecycle and active policy set), extraction and engine. Routes: `app/routers/policy_rules.py`.
- `app/payments/`: simulation-only payment initiator: config (fail closed), eligibility gate, deterministic local simulator, service (idempotency, state machine, audit outbox), metrics and the narrow `create_simulated_payment` tool. Routes: `app/routers/payments.py` (`/api/payments/simulation*`). Persistence: `app/repositories/payment_repository.py`. UI: `ui/pages_portal/payments.py`.
- `app/approvals/`: Phase 2 approval policy (deterministic), versioned approval requests and decisions. `app/routers/approvals.py` holds `/api/approvals*`.
- `app/repositories/`, `app/db/`: the MongoDB data-access layer and shared client.
- `ui/app.py`: Streamlit entry point (login, User portal, Admin console in `ui/pages_portal/`). `ui/streamlit_app.py` is a shim that runs it.
- `data/`: synthetic vendors, policies, invoices and deliveries.
- `tests/`: test suite.
