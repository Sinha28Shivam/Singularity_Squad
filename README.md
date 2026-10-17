# WorkplaceOps AI — Request-to-Payment Copilot (hackathon MVP)

Multi-agent workflow: **Intake → Policy Advisor (local BM25 RAG + citations) → Vendor Matcher (re-plans on conflict) → Procurement (deterministic costing, draft PO) → Billing Guardian (3-way invoice match, duplicate check) → Critic → Human approval gate.**
All data is synthetic. Draft PO only: the system makes no purchase and approves or pays nothing. See [the guide](Docs/WorkplaceOps_AI_Hackathon_Guide.md).

Execution flow, function call sequence and file dependencies: see [Docs/flow.md](Docs/flow.md). Phase specs live in [Docs/](Docs/), including [Phase 2 Approval Safety](Docs/PHASE_2_APPROVAL_SAFETY.md).

**Three roles (one Streamlit app):**
- **Users** sign up, fill in an event request form (category, catering service, date, attendees, budget, location, notes) and track their own tickets: status, outcome, approval panel, and edit & resubmit when more info or changes are requested.
- **Admins** log in with the account from `.env` and get a queue of all tickets. They run or re-run the multi-agent workflow (with a demo invoice) and see every result tab. They can reject hard-blocked tickets, but they **cannot approve**.
- **Approvers** (server-configured via `APPROVER_ACCOUNTS`) decide approval requests on the **Approvals** page: approve, reject or request changes, with a confirmation step. Nobody can approve their own request.
- **Policy library (RAG):** admins upload multiple policy, vendor and rate-card PDFs. Each ticket run retrieves cited evidence (file, page, section), checks quotes against verified rate cards with Decimal arithmetic, and adds review reasons to the approval request. Approvers verify extracted rate rows. The 3 PDFs in `data/policies` are ingested automatically on first start.
- Every draft PO needs an approver's sign-off. Approval records an internal authorization only: the PO stays a draft, and no order or payment is executed.

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
- `app/approvals/`: Phase 2 approval policy (deterministic), versioned approval requests and decisions. `app/routers/approvals.py` holds `/api/approvals*`.
- `app/repositories/`, `app/db/`: the MongoDB data-access layer and shared client.
- `ui/app.py`: Streamlit entry point (login, User portal, Admin console in `ui/pages_portal/`). `ui/streamlit_app.py` is a shim that runs it.
- `data/`: synthetic vendors, policies, invoices and deliveries.
- `tests/`: test suite.
