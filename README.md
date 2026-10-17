# WorkplaceOps AI — Request-to-Payment Copilot (hackathon MVP)

Multi-agent workflow: **Intake → Policy Advisor (local BM25 RAG + citations) → Vendor Matcher (re-plans on conflict) → Procurement (deterministic costing, draft PO) → Billing Guardian (3-way invoice match, duplicate check) → Critic → Human approval gate.**
All data is synthetic. Draft PO only: the system makes no purchase and approves or pays nothing. See [the guide](WorkplaceOps_AI_Hackathon_Guide.md).

Execution flow, function call sequence and file dependencies: see [flow.md](flow.md).

**Two roles (one Streamlit app):**
- **Users** sign up, fill in an event request form (category, catering service, date, attendees, budget, location, notes) and track their own tickets: status, outcome, admin notes, and edit & resubmit when more info is needed.
- **Admins** log in with the account from `.env` and get a queue of all tickets. They run or re-run the multi-agent workflow (with a demo invoice) and see every result tab. They approve or reject the draft PO with a note.
- Approving only records the decision; the PO stays a draft.

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
- `app/repositories/`, `app/db/`: the MongoDB data-access layer and shared client.
- `ui/app.py`: Streamlit entry point (login, User portal, Admin console in `ui/pages_portal/`). `ui/streamlit_app.py` is a shim that runs it.
- `data/`: synthetic vendors, policies, invoices and deliveries.
- `tests/`: test suite.
