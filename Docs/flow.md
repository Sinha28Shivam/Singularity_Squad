# WorkplaceOps AI — Execution Flow

This document covers:

- How to run the project.
- How folders and files depend on each other.
- How functions call each other during a workflow run.
- How data moves between agents.
- How each run is saved to MongoDB (Phase 1).
- The role-based portal: users submit tickets and the admin runs the workflow (section 10).
- Phase 2 approval safety: approvers decide versioned approval requests, with separation of duties (section 11).
- Phase 2 policy RAG: a multi-PDF policy library, cited evidence, rate cards and evidence snapshots that feed approvals (section 12).

All data is synthetic. The system creates a **draft PO only**. It never makes a purchase or payment. Saving a draft PO to the database does **not** approve or issue it.

---

## 1. How to run

```powershell
# 1. Install dependencies (use "python -m pip", or the venv's python)
python -m pip install -r requirements.txt

# 2. Configure .env (copy from .env.example; never commit .env)
#    API_KEY=sk-...                          LLM key (optional; without it the deterministic fallback runs)
#    MONGODB_URI=mongodb://localhost:27017/  local MongoDB or an Atlas SRV string
#    MONGODB_DATABASE=workplaceops
#    WORKPLACEOPS_PERSISTENCE=required       required = DB writes must succeed (else 503); disabled = no DB
#    WORKPLACEOPS_OFFLINE=1                  (optional) force no-LLM mode
#    ADMIN_USERNAME=admin                    admin account, created/updated at API startup
#    ADMIN_PASSWORD=<strong password>        (no admin account exists if unset; there is no default password)
#    SESSION_TTL_HOURS=8                     login session lifetime
#    APPROVER_ACCOUNTS=approver:<password>   Phase 2 approver accounts (comma-separated user:pass; demo auth only)

# 3. Make sure MongoDB is running (Windows service "MongoDB", or Atlas)
mongosh --eval "db.adminCommand('ping')"

# 4. Terminal 1: start the backend API (indexes are created at startup)
python -m uvicorn app.main:app --port 8000 --reload
#    Swagger: http://127.0.0.1:8000/docs  ·  Health: http://127.0.0.1:8000/api/health

# 5. Terminal 2: start the UI (ui\app.py also works; streamlit_app.py is a shim that runs it)
python -m streamlit run ui\streamlit_app.py
#    UI: http://localhost:8501   (set WORKPLACEOPS_API if the API is not on 127.0.0.1:8000)
#    Log in as the admin from .env, or click "Create account" to register as a user.

# 6. Run tests. Most use in-memory mongomock. test_mongo_integration.py and
#    test_persistence_restart.py use local MongoDB in throwaway databases (dropped afterwards)
#    and skip themselves if MongoDB is not running.
python -m pytest -q

# 7. Pre-commit checks
git check-ignore -v .env      # must print the .env rule
git ls-files .env             # must print nothing
git diff --check
```

**Demo (two browser windows, or log out and back in):**

1. **User:** click **Create account**, then in **My requests → New request** fill in e.g. "Quarterly team lunch", Team lunch, Lunch, 2026-10-23, 100 attendees, ₹1,00,000, Bengaluru. Submit; the ticket shows as `submitted`.
2. **Admin:** log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`. The ticket appears in the **Admin console** queue.
3. Open the ticket, keep the demo date `2026-10-17`, pick an invoice (INV-5001 match, INV-5002 mismatch, INV-4987 duplicate), and click **Run workflow**. The six result tabs appear. The ticket becomes `pending_approval`, with an approval request (reasons `PO_REQUIRES_HUMAN_APPROVAL` and `AMOUNT_THRESHOLD`).
4. **Approver:** log in with an `APPROVER_ACCOUNTS` user, open **Approvals**, pick the request, choose Approve / Reject / Request changes, then tick **Confirm**. Admins cannot approve, and nobody can approve their own request.
5. **User:** refresh **My requests**. The ticket shows "Approved — draft PO … for ₹76,125 recorded. No purchase has been made yet." plus the approval panel and its history.

To call the API directly, log in first; every ticket endpoint needs a bearer token:

```powershell
$login = @{ username = "admin"; password = "<ADMIN_PASSWORD>" } | ConvertTo-Json
$H = @{ Authorization = "Bearer " + (Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/auth/login -Body $login -ContentType application/json).token }
Invoke-RestMethod http://127.0.0.1:8000/api/admin/tickets -Headers $H
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/admin/tickets/<TICKET_ID>/run" -Headers $H -Body (@{ invoice_id = "INV-5001"; today = "2026-10-17" } | ConvertTo-Json) -ContentType application/json
```

**`Errno 10048` (port in use)** means another server is already running on that port. Find its PID with `Get-NetTCPConnection -LocalPort 8000 -State Listen` and run `Stop-Process -Id <PID>`, or use `--port 8001`.

---

## 2. Folder & file map

```text
AI_Hackathon_2.0/
├── .env / .env.example          # Secrets (git-ignored) / template (committed)
├── requirements.txt             # fastapi, pydantic, openai, streamlit, pymongo, mongomock (tests) ...
├── pytest.ini                   # registers the "real_mongo" marker
├── README.md / flow.md          # Docs
├── PHASE_1_MONGODB_PERSISTENCE.md  # Phase 1 spec
├── app/
│   ├── main.py                  # App wiring: lifespan (DB ping, indexes, admin seed), error handlers,
│   │                            #   public + admin-only legacy routes, includes the routers below
│   ├── routers/
│   │   ├── auth.py              # /api/auth/signup|login|logout|me
│   │   ├── my_tickets.py        # /api/my/tickets... (requester; own tickets only)
│   │   └── admin_tickets.py     # /api/admin/tickets... (admin: queue, run, decision, notes, audit)
│   ├── auth/
│   │   ├── passwords.py         # PBKDF2-SHA256 hashing (stdlib), constant-time verify
│   │   ├── service.py           # signup, login, logout, user_for_token, seed_admin_from_env
│   │   ├── deps.py              # FastAPI deps: current_user, require_user, require_admin
│   │   └── errors.py            # AuthError -> 401/403/409
│   ├── services/
│   │   ├── ticket_service.py    # submit/run/decide/notes/resubmit + views; run_and_persist (legacy)
│   │   └── errors.py            # TicketError -> 404/409/422
│   ├── portal_schemas.py        # Users, sessions, TicketSubmission, lifecycle statuses, user/admin views
│   ├── orchestrator.py          # Manager agent: runs the pipeline, builds WorkflowResult + audit trail (no DB)
│   ├── repositories/            # Data-access layer: the ONLY code that queries MongoDB
│   │   ├── ticket_repository.py         # tickets collection (lifecycle, owner, notes, decision)
│   │   ├── purchase_order_repository.py # purchase_orders collection (draft only; approval_status)
│   │   ├── audit_repository.py          # agent_logs collection
│   │   ├── user_repository.py           # users collection
│   │   └── session_repository.py        # sessions collection (hashed tokens, TTL)
│   ├── db/
│   │   ├── mongo.py             # one shared MongoClient, ping, db_status, persistence_mode
│   │   ├── indexes.py           # ensure_indexes() (idempotent)
│   │   └── errors.py            # PersistenceError (-> HTTP 503), AuditConflictError
│   ├── schemas.py               # Pydantic contracts (WorkflowResult now has ticket_id, persisted)
│   ├── llm.py                   # OpenAI wrapper; None on failure/offline -> fallback
│   ├── data_loader.py           # load_json() over data/ (cached)
│   └── agents/                  # Specialist agents: pure business logic, no DB access
│       ├── intake.py            # text -> EventRequest
│       ├── policy.py            # BM25 RAG over policies + rule checks
│       ├── vendor_matcher.py    # filter/rank vendors, re-plan
│       ├── procurement.py       # cost engine + draft PO (po_id derived from ticket id)
│       ├── billing.py           # 3-way invoice match + duplicate check
│       └── critic.py            # read-only final review
├── data/                        # Synthetic reference data (still JSON in Phase 1)
│   ├── policies/*.md, policy_rules.json, vendors.json
│   ├── invoices.json, deliveries.json, invoice_history.json
│   └── sample_requests.json
├── ui/                          # Frontend: HTTP only, never imports app/
│   ├── app.py                   # Entry point: health sidebar, login state, role-based st.navigation
│   ├── streamlit_app.py         # Shim, so "streamlit run ui\streamlit_app.py" still works
│   ├── lib/api.py               # HTTP helpers; adds the bearer token; a 401 logs out
│   ├── lib/render.py            # Shared renderers: pipeline, 6 result tabs, audit, INR formatting
│   └── pages_portal/
│       ├── login.py             # Log in / Create account
│       ├── user_portal.py       # New request form, My requests, outcome, notes, edit & resubmit
│       ├── admin_console.py     # Queue + counts, ticket detail, run/re-run, reject (reviewed only), notes, reconcile
│       └── approvals.py         # Approver queue + detail, 2-step confirm, idempotent decisions (Phase 2)
└── tests/
    ├── conftest.py              # sys.path, offline LLM, fresh mongomock client per test
    ├── test_intake.py, test_policy.py, test_vendor_procurement.py, test_billing.py, test_workflow.py
    ├── test_repositories.py     # repository behaviour on mongomock
    ├── test_api_persistence.py  # ticket endpoints, 404/422/503, disabled mode, invoice outcomes
    ├── test_persistence_failures.py # failure at each write stage, retry idempotency, no secret leaks
    ├── test_ui_tickets.py       # AppTest: admin console shows the stored persisted flag, 404/503
    ├── test_auth.py             # signup/login/logout, hashing, token expiry, admin seeding, 401/403
    ├── test_ticket_lifecycle.py # submit -> run -> pending_approval, re-run, needs_info, ownership, views
    ├── test_approval_policy.py, test_approval_service.py, test_approval_concurrency.py  # Phase 2 core
    ├── test_approval_api.py     # Phase 2 authz matrix + HTTP lifecycle
    ├── test_approval_acceptance.py, test_approval_persistence.py  # real MongoDB + uvicorn (spec §18)
    ├── test_ui_approvals.py     # AppTest: approver page, confirmation, 409/503 handling
    ├── test_ui_portal.py        # AppTest: login, user submit/edit, admin run, no admin approve, 401 handling
    ├── test_mongo_integration.py# real local MongoDB: indexes + duplicate-key behaviour
    └── test_persistence_restart.py # real MongoDB + 2 real uvicorn processes: survives API restart
```

Also at the repo root: `.gitattributes` (LF line endings). All text files are saved as UTF-8 **without a BOM**, so `.gitignore` and `.env.example` parse correctly.

### How files depend on each other (imports and I/O)

```mermaid
flowchart LR
    subgraph UI["ui/"]
        ST[app.py + pages_portal/*<br/>lib/api.py, lib/render.py]
    end
    subgraph APP["app/"]
        MAIN[main.py]
        RT[routers/<br/>auth, my_tickets, admin_tickets]
        AUTH[auth/<br/>service, deps, passwords]
        SVC[services/ticket_service.py]
        ORCH[orchestrator.py]
        SCH[schemas.py]
        LLM[llm.py]
        DL[data_loader.py]
        subgraph AG["agents/"]
            IN[intake.py]
            PO[policy.py]
            VM[vendor_matcher.py]
            PR[procurement.py]
            BI[billing.py]
            CR[critic.py]
        end
        subgraph REPO["repositories/"]
            TR[ticket_repository.py]
            POR[purchase_order_repository.py]
            AR[audit_repository.py]
            UR[user_repository.py<br/>session_repository.py]
        end
        subgraph DBL["db/"]
            MG[mongo.py]
            IX[indexes.py]
            ER[errors.py]
        end
    end
    subgraph DATA["data/ (JSON reference data)"]
        JSON[(policies, vendors,<br/>invoices, deliveries,<br/>history, samples)]
    end
    subgraph MONGO["MongoDB: workplaceops"]
        TK[(tickets)]
        PC[(purchase_orders)]
        AL[(agent_logs)]
        US[(users)]
        SE[(sessions)]
    end
    ENV[(.env)]
    OPENAI{{OpenAI API}}

    ST -- "HTTP JSON + Bearer token" --> MAIN
    MAIN --> RT
    RT --> AUTH
    RT --> SVC
    MAIN --> AUTH
    AUTH --> UR
    UR --> MG
    MG -. PyMongo .-> US & SE
    MAIN --> SVC
    MAIN --> TR & AR
    MAIN --> MG & IX
    MAIN --> BI & PO
    SVC --> ORCH
    SVC --> TR & POR & AR
    SVC --> IX
    ORCH --> IN & PO & VM & PR & BI & CR
    VM --> PR
    IN & PO & CR --> LLM
    PO & VM & BI --> DL
    DL -. reads .-> JSON
    TR & POR & AR --> MG
    TR & POR & AR & MG & IX --> ER
    MG -. PyMongo .-> TK & PC & AL
    MG -. reads .-> ENV
    LLM -. reads .-> ENV
    LLM -. HTTPS .-> OPENAI
    SCH -. types used by all modules .- SVC
```

**Layering rules:**

- **UI → API over HTTP only.** The UI never imports `app/`. Streamlit `session_state` holds only the login token and cached display data; MongoDB is the source of truth.
- **Every ticket endpoint is authenticated.** `deps.py` resolves the bearer token. `require_admin` guards `/api/admin/*` and the legacy endpoints; `/api/my/*` is scoped to the owner.
- **Routes (`main.py`, `routers/`) validate input and map errors.** A missing or foreign ticket gives `404`, a malformed ID `422`, an illegal transition `409`, a missing or expired token `401`, the wrong role `403`, and a database failure `503`.
- **`services/ticket_service.py` owns the order of persistence steps.** It calls the orchestrator, then the repositories.
- **The orchestrator and agents have no database code**, so their business logic is unchanged.
- **Only `repositories/` queries MongoDB**, through the single shared client in `db/mongo.py`.
- **Reference data (vendors, policies, invoices) still comes from `data/*.json`.** Only run outputs go to MongoDB.

---

## 3. Execution flow: one workflow run

In the portal, a run starts when the admin clicks **Run workflow**, which calls `POST /api/admin/tickets/{id}/run` → `ticket_service.run_ticket()`. Section 10 covers that wrapper: lifecycle status, re-run audit offset and approval. The persistence core below (agents, then PO → audit → ticket commit point) is the same code path, `persist_result()`. The diagram shows the legacy admin-only `POST /api/workflow`, which creates the ticket and runs it in one call.

```mermaid
flowchart TD
    A([User submits request in Streamlit]) --> B["main.workflow()<br/>validate body (400 if empty)"]
    B --> S0{"persistence_mode()"}
    S0 -->|disabled| S1["new_ticket_id()<br/>run_workflow(..., request_id=ticket_id)<br/>persisted = False"]
    S1 --> R
    S0 -->|required| S2["ticket_service.run_and_persist()<br/>1. new_ticket_id() e.g. REQ-20261017-A72640C9<br/>2. ensure indexes once per client"]
    S2 --> S3["3. create_ticket(status='received', raw_text, timestamps)"]
    S3 -->|DB error| E503
    S3 --> C["4. orchestrator.run_workflow(text, today, invoice_id, request_id=ticket_id)"]

    subgraph WF["Multi-agent workflow (in memory, unchanged logic)"]
        C --> D["intake.extract_request()"]
        D -->|missing fields| K
        D --> E["policy.evaluate()"]
        E --> F["vendor_matcher.match_vendors()<br/>(re-plans if first choice infeasible)"]
        F --> G["procurement.build_draft_po()<br/>po_id = DRAFT-PO-&lt;ticket suffix&gt;"]
        G --> H{invoice_id and PO?}
        H -->|yes| I["billing.bind_demo_documents()<br/>billing.reconcile()"]
        H -->|no| K
        I --> K["critic.review() -> final_status<br/>human approval gate"]
    end

    C -->|workflow exception| EF["mark_ticket_status('failed')<br/>re-raise"]
    K --> P2{"5. persist_result()<br/>draft PO exists?"}
    P2 -->|yes| P3["6. upsert_draft_po() keyed by po_id"]
    P2 -->|no| P4
    P3 --> P4["7. append_audit_entries() (sequence = step)"]
    P4 --> P5["8. result.persisted = True<br/>update_ticket_result(snapshot, final_status)<br/><b>commit point</b>"]
    P3 & P4 & P5 -->|DB error| PE["persisted = False<br/>mark_ticket_status('persistence_error')<br/>raise PersistenceError"]
    PE --> E503["HTTP 503 {detail: safe message}<br/>(no URI / credentials / trace)"]
    P5 --> R([9. Return the same WorkflowResult that was stored])
```

### Persistence steps (spec §9)

| # | Function | Collection | Notes |
|---|---|---|---|
| 1 | `ticket_service.new_ticket_id()` | – | `REQ-<yyyymmdd>-<8 hex>`, unique per submission |
| 2 | `_ensure_indexes_once()` → `ensure_indexes()` | all | idempotent; covers the case where startup ran while MongoDB was down |
| 3 | `create_ticket()` | `tickets` | `status="received"`, `workflow_result=None` |
| 4 | `run_workflow(..., request_id=ticket_id)` | – | agents run in memory exactly as before |
| 5 | `persist_result(ticket_id, result)` | – | runs steps 6–8; safe to retry for the same ticket |
| 6 | `upsert_draft_po()` | `purchase_orders` | only if a PO exists; upsert by `po_id`; rejects non-`draft` status |
| 7 | `append_audit_entries()` | `agent_logs` | `AuditEntry.step` → `sequence`. A retry with identical content is ignored; different content raises `AuditConflictError`. Existing events are never overwritten. |
| 8 | `update_ticket_result()` | `tickets` | **Commit point.** Saves the full JSON snapshot with `persisted=True` and `status=final_status`, only after steps 6–7 succeeded. |
| 9 | return | – | the same object that was stored: response and saved snapshot agree |

**Why the ticket update comes last:**

- The ticket snapshot is written after the PO and audit writes. So `workflow_result.persisted == true` in MongoDB always means every required write succeeded.
- The API response is the same object, so the two never disagree.
- The UI shows the stored flag as-is and never forces it to `true`.

**Failure policy (`WORKPLACEOPS_PERSISTENCE=required`):**

- A failed database write never returns success. The API returns `503` with a safe message, and the response has no `persisted` field.
- The server logs only the ticket ID and the error class: never the request text, the URI or credentials.
- Writes are **not atomic** across collections:

| Fails at | API | Ticket left as | Partial data that may exist |
|---|---|---|---|
| `create_ticket` | 503 | (no ticket) | nothing |
| workflow exception | error | `failed`, `workflow_result=null` | nothing else |
| `upsert_draft_po` | 503 | `persistence_error`, `workflow_result=null` | nothing else |
| `append_audit_entries` | 503 | `persistence_error`, `workflow_result=null` | draft PO (and possibly some audit events) |
| `update_ticket_result` | 503 | `persistence_error`, `workflow_result=null` | draft PO + all audit events |

- Partial data can be found by `ticket_id`.
- Calling `persist_result()` again for the same ticket completes it without duplicating the PO, the audit sequence or the ticket. This is covered by `test_retry_does_not_duplicate`.
- A transaction or outbox pattern is planned for a later reliability phase. **Nothing claims atomicity today.**

### Demo invoice binding

The synthetic invoices in `data/invoices.json` reference the placeholder `DRAFT-PO-1001`. Every ticket now gets a unique PO ID, so `billing.bind_demo_documents()` points the demo invoice and its delivery record at the current draft PO before reconciliation.

- It changes **only the PO number**. Vendor, rate, quantity, delivery and duplicate checks still apply.
- It works on copies; the seed data is never mutated.
- It records a `bind_demo_invoice` audit event.

---

## 4. Function call sequence

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant UI as streamlit_app.py
    participant API as main.py
    participant SVC as ticket_service.py
    participant O as orchestrator.py
    participant AG as agents/*
    participant RP as repositories/*
    participant DB as MongoDB

    U->>UI: enter request, click Run
    UI->>API: POST /api/workflow {text, today, invoice_id}
    API->>SVC: run_and_persist(text, today, invoice_id)
    SVC->>SVC: new_ticket_id()
    SVC->>RP: ensure_indexes() (once per client)
    SVC->>RP: create_ticket({status: received})
    RP->>DB: tickets.insert_one
    SVC->>O: run_workflow(..., request_id=ticket_id)
    O->>AG: intake → policy → vendor_matcher → procurement → billing → critic
    AG-->>O: EventRequest, PolicyReport, VendorMatchResult, ProcurementResult, ReconciliationReport, CriticReport
    O-->>SVC: WorkflowResult (+ audit_trail)
    Note over SVC: persist_result()
    opt draft PO created
        SVC->>RP: upsert_draft_po(ticket_id, po)
        RP->>DB: purchase_orders.update_one(upsert, key po_id)
    end
    SVC->>RP: append_audit_entries(ticket_id, audit_trail)
    RP->>DB: agent_logs.insert_many(ordered=False)
    SVC->>SVC: result.persisted = True
    SVC->>RP: update_ticket_result(snapshot, final_status)  (commit point)
    RP->>DB: tickets.update_one
    SVC-->>API: same WorkflowResult (ticket_id, persisted=True)
    API-->>UI: 200 JSON (or 503 if any write failed)
    UI-->>U: ticket ID + pipeline + tabs

    Note over U,DB: Later, even after an API restart
    U->>UI: Load ticket / Load audit (ticket ID)
    UI->>API: GET /api/tickets/{id}
    API->>RP: get_ticket(id)
    RP->>DB: tickets.find_one (no _id)
    API-->>UI: ticket + workflow_result (404 if unknown)
    UI->>API: GET /api/tickets/{id}/audit
    API->>RP: get_ticket(id), get_audit_entries(id, limit<=500)
    RP->>DB: agent_logs.find().sort(sequence)
    API-->>UI: {ticket_id, events[]} in sequence order
```

### Inside the workflow (agents, unchanged)

| # | Agent / function | Input | Output | LLM? | Reads |
|---|---|---|---|---|---|
| 1 | `intake.extract_request()` | text, today, request_id | `EventRequest`, missing fields, questions | extraction (regex wins on numbers) | – |
| 2 | `policy.evaluate()` → `retrieve()`, `get_clause()` | `EventRequest`, today | `PolicyReport` | summary only | `policies/*.md`, `policy_rules.json` |
| 3 | `vendor_matcher.match_vendors()` → `procurement.calculate_cost()` | request, rules | `VendorMatchResult` | no | `vendors.json` |
| 4 | `procurement.build_draft_po()` → `_po_id(request_id)` | request, vendor, rules | `ProcurementResult` | no | – |
| 5 | `billing.bind_demo_documents()`, `billing.reconcile()` | invoice, PO, delivery, history | `ReconciliationReport` | no | `invoices.json`, `deliveries.json`, `invoice_history.json` |
| 6 | `critic.review()` | `WorkflowResult` (read-only) | `CriticReport` | summary only | – |

---

## 5. Data contracts and MongoDB documents

```mermaid
flowchart LR
    T[raw text] --> ER[EventRequest]
    ER --> PRP[PolicyReport]
    ER --> VMR[VendorMatchResult]
    PRP -- PolicyRules --> VMR
    VMR -- Vendor --> PRR[ProcurementResult / DraftPO]
    PRR -- DraftPO --> RR[ReconciliationReport]
    ER & PRP & VMR & PRR & RR --> WR[WorkflowResult<br/>ticket_id, persisted]
    WR --> CRR[CriticReport] --> WR
    WR --> AE[audit_trail: AuditEntry list]

    WR == "model_dump(mode='json')" ==> TKD[(tickets.workflow_result)]
    PRR == DraftPO fields + ticket_id ==> POD[(purchase_orders)]
    AE == "step → sequence" ==> ALD[(agent_logs)]
```

| Collection | One document per | Key fields | Indexes |
|---|---|---|---|
| `tickets` | ticket | `ticket_id`, `status` (portal lifecycle: submitted / processing / reviewed / needs_info / approved / rejected / failed / persistence_error; legacy runs store the final status), `owner_username`, `owner_name`, `owner_email`, `submission`, `raw_text`, `workflow_result`, `notes[]`, `decision`, `run_count`, `last_run_by`, `last_run_at`, `error`, `created_at`, `updated_at`, `schema_version` | unique `ticket_id`; `updated_at` desc; `(owner_username, updated_at)`; `(status, updated_at)` |
| `purchase_orders` | draft PO | all `DraftPO` fields + `ticket_id`, `created_at`, `updated_at`, `schema_version`. `status` is always `draft`; `approval_status` (approved / rejected), `approved_by` and `approved_at` record the approver's decision (Phase 2). | unique `po_id`; `ticket_id` |
| `agent_logs` | audit event | `ticket_id`, `sequence`, `agent` (agents, plus `User` / `Admin` for portal actions), `action`, `status`, `detail`, `evidence[]`, `llm_used`, `timestamp`, `schema_version` | unique `(ticket_id, sequence)`; `(ticket_id, timestamp)` |
| `users` | account | `username` (lowercase), `email` (lowercase), `name`, `role` (user / admin), `password_hash`, `salt`, `created_at`, `updated_at`, `schema_version` | unique `username`; unique `email` |
| `sessions` | login session | `token_hash` (SHA-256 of the token; the token itself is never stored), `username`, `role`, `created_at`, `expires_at` | unique `token_hash`; TTL on `expires_at` (expiry is also checked in code) |

- All timestamps are timezone-aware UTC.
- `_id` is never returned by the API.
- Only structured evidence is stored, never model chain-of-thought.

---

## 6. API endpoints

Auth: send `Authorization: Bearer <token>` (from login or signup).

**Public**

| Method | Path | Calls | Returns |
|---|---|---|---|
| GET | `/api/health` | `llm.llm_enabled()`, `db_status()` | `{status, llm_enabled, model, database: connected / unavailable / disabled, persistence}` |
| GET | `/api/samples` | `load_json()` | demo prompts |
| GET | `/api/invoices` | `billing.load_invoices()` | invoice summaries |
| POST | `/api/auth/signup` | `auth.service.signup_and_login()` | `201 {token, expires_at, user}`. Role is always `user`. `409` for a duplicate username or email; `422` for invalid input. |
| POST | `/api/auth/login` | `auth.service.login()` | `{token, expires_at, user}`; `401 "Invalid username or password."` (same message for an unknown user or wrong password) |

**Any logged-in user** (`routers/auth.py`, `routers/my_tickets.py`)

| Method | Path | Calls | Returns |
|---|---|---|---|
| POST | `/api/auth/logout` | `auth.service.logout()` | `204`; the token stops working |
| GET | `/api/auth/me` | `current_user` | `{username, name, email, role}` |
| POST | `/api/my/tickets` | `ticket_service.submit_ticket()` | `201 UserTicketView` with status `submitted`; no workflow is run |
| GET | `/api/my/tickets?limit=50` | `list_tickets_for_owner()` | `{tickets: [TicketListItem]}`, own tickets only |
| GET | `/api/my/tickets/{id}` | `to_user_view()` | `UserTicketView`: status, outcome text, submission, PO summary, clarification questions, notes, decision. **No agent internals.** `404` if not the owner. |
| PUT | `/api/my/tickets/{id}` | `user_update_ticket()` | edit and resubmit (status back to `submitted`); `409` unless status is `submitted` or `needs_info` |
| POST | `/api/my/tickets/{id}/notes` | `add_note()` | `UserTicketView` |

**Admin only** (`routers/admin_tickets.py`, plus the legacy routes in `main.py`). No token gives `401`; a user token gives `403`.

| Method | Path | Calls | Returns |
|---|---|---|---|
| GET | `/api/admin/tickets?status=&limit=100` | `list_tickets()`, `count_tickets_by_status()` | `{tickets: [TicketListItem], counts: {status: n}}` |
| GET | `/api/admin/tickets/{id}` | `to_admin_view()` | `AdminTicketView`: requester, submission, raw text, full `workflow_result`, notes, decision, run info |
| POST | `/api/admin/tickets/{id}/run` | `ticket_service.run_ticket()` | `AdminTicketView`; `409` if approved, rejected or already processing; `503` on DB failure |
| POST | `/api/admin/tickets/{id}/decision` | `ticket_service.decide_ticket()` | **Reject only**, for `reviewed` tickets (blocked or no PO). `approve` gives `403` (approvals go through `/api/approvals`). Rejecting a `pending_approval` ticket gives `409`. |
| POST | `/api/admin/tickets/{id}/notes` | `add_note()` | `AdminTicketView` |
| GET | `/api/admin/tickets/{id}/audit?limit=500` | `get_audit_entries()` | `{ticket_id, events}` sorted by `sequence` |
| POST | `/api/workflow` | `ticket_service.run_and_persist()` | Legacy one-shot create and run: `WorkflowResult` with `ticket_id`, `persisted` |
| GET | `/api/tickets?limit=20` | `list_recent_tickets()` (limit 1–100) | `{tickets: [{ticket_id, created_at, updated_at, status, final_status}]}`, with no request text |
| GET | `/api/tickets/{ticket_id}` | `get_ticket()` | raw ticket document; `404` / `422` / `503` |
| GET | `/api/tickets/{ticket_id}/audit?limit=500` | `get_ticket()`, `get_audit_entries()` | `{ticket_id, events}`; `404` / `422` / `503` |
| POST | `/api/reconcile` | `bind_demo_documents()`, `billing.reconcile()` | `ReconciliationReport` (`404` if invoice unknown) |

Responses never contain `password_hash`, `salt`, `token_hash`, connection URIs or tracebacks.

**Startup and shutdown (`lifespan`):**

- On startup, `ping_database()`, `ensure_indexes()` and `seed_admin_from_env()` run.
  - `seed_admin_from_env()` creates or updates the admin from `ADMIN_USERNAME` / `ADMIN_PASSWORD`, re-hashing the password on each start, so changing `.env` takes effect after a restart.
  - It never promotes an existing normal user to admin.
- If MongoDB is down, the API still starts: `/api/health` reports `unavailable` and write requests return `503`.
- On shutdown, `close_client()` runs.

A startup failure is visible in two places:

- The logs show `MongoDB ping failed: ServerSelectionTimeoutError` and `MongoDB not reachable at startup ... will return 503`.
- `/api/health` shows `"database": "unavailable"`.

Neither includes the URI or credentials; this was verified with a URI containing a username and password.

**Configuration checklist:**

- Keep `WORKPLACEOPS_PERSISTENCE=required` for a persistence demo.
- Use `disabled` only for intentional non-persistent development; the UI then labels results "Not persisted".
- Give the database user permissions only on the application database.
- On Atlas, restrict network access and URL-encode special characters in the username and password.
- Never commit `.env`, database dumps or credentials. `.gitignore` covers `.env` and `.env.*`, with an exception for `!.env.example`.

---

## 7. Streamlit UI flow (role-based)

```mermaid
flowchart LR
    START([ui/app.py]) --> AUTHQ{logged in?}
    AUTHQ -->|no| LOGIN[Login page<br/>Log in / Create account]
    LOGIN -->|POST /api/auth/login or signup| AUTHQ
    AUTHQ -->|role = user| UP[My requests]
    AUTHQ -->|role = admin| AC[Admin console]

    subgraph USER["User portal (pages_portal/user_portal.py)"]
        UP --> NEW[New request form<br/>POST /api/my/tickets]
        UP --> LIST[My requests list<br/>GET /api/my/tickets]
        LIST --> DET[Request detail: outcome, PO summary,<br/>questions, notes, decision<br/>GET /api/my/tickets/id]
        DET -->|status submitted or needs_info| EDIT[Edit and resubmit<br/>PUT /api/my/tickets/id]
        DET --> UNOTE[Add note<br/>POST .../notes]
    end

    subgraph ADMIN["Admin console (pages_portal/admin_console.py)"]
        AC --> Q[Queue + status counts + filter<br/>GET /api/admin/tickets]
        Q --> AD[Ticket detail<br/>GET /api/admin/tickets/id]
        AD --> RUN[Run / Re-run: demo date + invoice<br/>POST .../run]
        RUN --> TABS[Pipeline + 6 result tabs<br/>lib/render.py]
        AD --> DEC[Reject reviewed ticket + note<br/>POST .../decision]
        AD --> ANOTE[Add note<br/>POST .../notes]
        TABS --> AUD[Load saved audit<br/>GET .../audit]
    end
```

**UI behaviour:**

- The sidebar shows API, LLM and DB health, "Signed in as <name> (<role>)", and **Logout**. A disclaimer banner appears on every page.
- The admin console has **no Approve button** (Phase 2): approvals belong to approvers. **Reject** appears only for `reviewed` tickets. **Run** is disabled when the ticket is approved, rejected or processing. The backend enforces the same rules (`403` / `409`).
- The admin ticket banner shows the stored `workflow_result.persisted` flag as saved, plus the run count and who ran it last.
- Error messages:
  - `401` → "Session expired — please log in again." The user is logged out.
  - `404` → "Ticket not found."
  - `409` → the backend's message.
  - `503` → "Database unavailable…"
  - Backend down → a hint with the start command.
  - Tracebacks are never shown.
- The user portal never displays vendor evaluations, invoice checks, policy checks or the audit trail; the API doesn't send them.

---

## 8. Decision outcomes (demo date 2026-10-17)

| Scenario | What triggers it | Final status | Saved |
|---|---|---|---|
| happy_path + INV-5001 | VEN-001 unavailable → re-planned to VEN-002, ₹76,125, invoice matches | `pass` | ticket + draft PO + audit |
| happy_path + INV-5002 | Unit price 720 ≠ 700 (FIN-5.3); 100 billed vs 95 delivered (FIN-5.2) | `blocked` | ticket + draft PO + audit |
| happy_path + INV-4987 | Invoice already paid (FIN-5.4) | `blocked` | ticket + draft PO + audit |
| over_budget | ₹90,825 > ₹60,000 (EVT-3.3) | `blocked` | ticket + audit (**no PO**) |
| too_soon | < 5 business days (EVT-2.2); PO withheld | `blocked` | ticket + audit (no PO) |
| missing_info | date / attendees / budget missing | `needs_clarification` | ticket + audit (no PO) |
| MongoDB down | write fails | HTTP `503` | nothing claimed as saved |

---

## 9. Tests (`tests/`)

| File | Covers |
|---|---|
| `test_intake.py`, `test_policy.py` | Extraction, BM25 retrieval, policy checks |
| `test_vendor_procurement.py`, `test_billing.py` | Vendor eligibility, costing, approver tiers, reconciliation |
| `test_workflow.py` | Orchestrator end-to-end + original API endpoints |
| `test_repositories.py` | Serialization, `None` for unknown tickets, audit order, duplicate and conflict events, PO upsert idempotency, draft-only rule, error mapping, no `_id` |
| `test_api_persistence.py` | Unique ticket IDs, retrieval, audit order, 404/422/503, `persistence_error` marking, disabled mode, no PO when over budget, invoice outcomes unchanged with unique IDs |
| `test_persistence_failures.py` | Failure at create / PO / audit / ticket-update stages returns a safe 503 and leaves the ticket as `persistence_error` with no snapshot. Also: partial state can be diagnosed, a retry doesn't duplicate data, logs leak no credentials or request text, and the success snapshot equals the response. |
| `test_ui_tickets.py` | Streamlit AppTest (admin console): a loaded ticket shows the stored `persisted` value (true or false); friendly 404 and 503 messages; requests without the admin token get `401` |
| `test_auth.py` | Signup and auto-login, duplicate username/email `409` (case-insensitive), validation `422`, identical `401` for an unknown user or wrong password, missing/garbage/expired token `401`, logout revokes the token, admin seeding and re-seeding, no escalation of existing users, `role` in the signup body ignored, salted hashes, no secrets in responses, `require_admin` `403` |
| `test_ticket_lifecycle.py` | Submit (no run, no PO); ownership isolation (`404`); admin queue, counts and filter; run → reviewed with persisted snapshot and draft PO; re-run keeps one PO and audit sequences contiguous; approve (PO still `draft`) and reject; run after a decision `409`; approve when blocked `409`; needs_info → edit and resubmit → re-run; notes from both sides; the user view has no internals; concurrent-run guard; form → request text mapping (numbers in notes can't override the form) |
| `test_ui_portal.py` | Streamlit AppTest with a fake backend: login and signup per role, user submit payload, list and detail with no internals, needs_info edit (`PUT`), admin queue, run, tabs, approve/reject enablement, notes, `409` messages, `401` logs out, shim entry point |
| `test_mongo_integration.py` | Real local MongoDB: index names and uniqueness, duplicate-key behaviour (skipped if MongoDB is down) |
| `test_persistence_restart.py` | **Acceptance test.** Real MongoDB in a unique throwaway database, against real uvicorn process #1, then process #2. Submits a ticket, restarts, and checks the ticket, snapshot, draft PO and audit history are identical. Also checks that an over-budget ticket stores no PO. Skips if MongoDB is unavailable. |

- `conftest.py` gives each test a fresh in-memory `mongomock` client and forces offline LLM mode.
- Tests marked `real_mongo` use a real local MongoDB instead, in a uniquely named database that is dropped afterwards. They never touch the default `workplaceops` database.

### Phase 1 verification record (2026-10-17)

- `python -m pytest -q`: **86 passed**, including the real-MongoDB restart acceptance test.
- Manual live run with the LLM enabled, port 8010, throwaway database: all 6 scenarios returned the expected status. Each response and its stored snapshot both had `persisted=true`.
- After an API restart, all 6 tickets and their audit histories (sequences in order) reloaded. There were 3 draft POs, all `draft`, with no duplicate POs or audit sequences.
- In Streamlit against the restarted API, Load ticket and Load audit showed the saved result and 10 events, and an unknown ticket showed "Ticket not found."
- With a database URI containing a username and password, pointed at a dead port: health showed `unavailable`, workflow returned `503`, and neither the responses nor the logs contained the credentials.
- `.env` is ignored and untracked, there are no BOMs or garbled characters, and `git diff --check` is clean.

---

## 10. Role-based portal: users submit, the admin runs the workflow

> Phase 2 changed who approves: the admin no longer approves. A pass or warning run now creates an approval request that an **approver** decides; see section 11. Sections 10.1 and 10.3 below show the original Phase 1.5 flow; 11.2 has the current ticket lifecycle.

### 10.1 Ticket lifecycle (`tickets.status`)

```mermaid
stateDiagram-v2
    [*] --> submitted: User submits form<br/>POST /api/my/tickets
    submitted --> submitted: User edits<br/>PUT /api/my/tickets/id
    submitted --> processing: Admin runs<br/>POST .../run
    processing --> reviewed: final_status pass / warning / blocked
    processing --> needs_info: final_status needs_clarification
    processing --> failed: workflow raised
    processing --> persistence_error: DB write failed
    needs_info --> submitted: User edits and resubmits
    reviewed --> processing: Admin re-runs (before a decision)
    needs_info --> processing: Admin re-runs
    failed --> processing: Admin retries
    persistence_error --> processing: Admin retries
    reviewed --> approved: Admin approves<br/>(draft PO + pass/warning)
    reviewed --> rejected: Admin rejects
    approved --> [*]
    rejected --> [*]
```

**Lifecycle rules:**

- **`status` vs `final_status`:** `status` is the portal lifecycle that both roles see. The workflow's own verdict, `workflow_result.final_status`, is unchanged.
- **One run at a time:** `run_ticket()` claims the ticket with a conditional update to `processing`, and only if it is still runnable. A second concurrent run gets `409`.
- **Re-runs keep history:** each run's audit events continue after the last sequence, using an offset applied to copies of the audit entries. The unique `(ticket_id, sequence)` index holds and the full history is kept. The PO is upserted by `po_id`, so there is never more than one.
- **Approval is a recorded decision only:** the PO keeps `status: "draft"` and gets `approval_status: "approved"`. Nothing is issued, purchased or paid.
- **Every portal action is audited:** submit, resubmit, run, approve and reject are written to `agent_logs` with agent `User` or `Admin`.

### 10.2 Auth flow

```mermaid
sequenceDiagram
    autonumber
    actor P as Person
    participant UI as ui/app.py + login.py
    participant API as routers/auth.py
    participant S as auth/service.py
    participant DB as MongoDB (users, sessions)

    P->>UI: Create account (username, name, email, password)
    UI->>API: POST /api/auth/signup
    API->>S: signup_and_login()
    S->>S: hash_password() — PBKDF2-SHA256, 200k iterations, random salt
    S->>DB: users.insert_one(role = "user")
    S->>S: token = token_urlsafe(32)
    S->>DB: sessions.insert_one(token_hash = sha256(token), expires_at)
    API-->>UI: 201 {token, expires_at, user}
    UI->>UI: st.session_state["auth"] = {...}
    Note over UI,API: Every later call sends Authorization: Bearer token
    UI->>API: any /api/my or /api/admin request
    API->>S: deps.current_user → user_for_token()
    S->>DB: sessions.find_one(sha256(token)), check expires_at
    alt valid
        API-->>UI: 200 (admin routes also need role = admin, else 403)
    else missing / expired / logged out
        API-->>UI: 401 → UI clears auth and shows the login page
    end
```

The admin account is never created through the UI. It is seeded at startup from `ADMIN_USERNAME` and `ADMIN_PASSWORD` in `.env`.

### 10.3 End-to-end: user → admin → user

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    actor A as Admin
    participant UP as user_portal.py
    participant AC as admin_console.py
    participant API as routers (my_tickets / admin_tickets)
    participant TS as ticket_service.py
    participant WF as orchestrator + agents
    participant DB as MongoDB

    U->>UP: Fill in the New request form
    UP->>API: POST /api/my/tickets (TicketSubmission)
    API->>TS: submit_ticket(user, submission)
    TS->>TS: build_request_text(submission)
    TS->>DB: tickets.insert(status=submitted, owner, submission, raw_text)
    TS->>DB: agent_logs seq 1 (User / submit_ticket)
    API-->>UP: 201 {ticket_id, status: submitted, outcome}

    A->>AC: Open the queue
    AC->>API: GET /api/admin/tickets
    A->>AC: Run workflow (demo date, invoice)
    AC->>API: POST /api/admin/tickets/{id}/run
    API->>TS: run_ticket()
    TS->>DB: set_status(processing, only if runnable)
    TS->>WF: run_workflow(raw_text, today, invoice_id, request_id=ticket_id)
    WF-->>TS: WorkflowResult
    TS->>TS: shift audit steps after the last sequence
    TS->>DB: upsert draft PO → append audit → save_run_result(snapshot, reviewed / needs_info) (commit point)
    API-->>AC: AdminTicketView (full result, 6 tabs)

    A->>AC: Approve + note
    AC->>API: POST /api/admin/tickets/{id}/decision
    API->>TS: decide_ticket(approve)
    TS->>DB: set_decision(status=approved, only if reviewed)
    TS->>DB: PO approval_status=approved (status stays draft)
    TS->>DB: agent_logs (Admin / approve) + push note

    U->>UP: Refresh My requests
    UP->>API: GET /api/my/tickets/{id}
    API->>TS: to_user_view()
    API-->>UP: status approved · outcome · PO summary · admin note (no internals)
```

### 10.4 Turning the form into workflow input

The agents are unchanged. `build_request_text()` turns the structured form into the request text the intake agent already understands.

- **Structured values come first.** The parser takes the first match, so free text that comes later can't override them. Example:
  `Organize lunch for 100 employees on 2026-10-23. Keep the total under Rs 1,00,000. Use an approved vendor and follow the workplace event policy. Venue: at the Bengaluru office. Event: Team lunch — Quarterly team lunch. Additional notes: …`
- **Free text can't change the catering service.** In the title, location and notes, a meal word that the parser would rank above the chosen service is reworded. For example, "breakfast" becomes "morning meal" when the user picked dinner. The "Team lunch" category label becomes "Team meal" when another service is chosen.
- **The structured submission is stored on the ticket too**, so the admin can compare what the user entered (`submission`) with what the agent extracted (`workflow_result.request`).
- **Event categories are labels.** Costing still uses the catering service, because vendor data only covers lunch, breakfast, dinner and snacks. A request no vendor can serve is flagged by the existing checks (no eligible vendor → blocked) rather than faked.

### 10.5 Portal verification record (2026-10-17)

- `python -m pytest -q`: **149 passed**, including the auth, lifecycle, portal UI and real-MongoDB restart tests.
- **Live API run** (LLM enabled, throwaway database):
  - Legacy `/api/workflow` with no token: `401`. Admin login: `200`. Two user signups: `201`. A user calling a legacy route: `403`.
  - A user submitted a ticket: `submitted`. The other user got `404` on it and saw an empty list.
  - The admin ran it with INV-5001: `reviewed`, final status `pass`, reconciliation `match`, `persisted=true`. The form values were extracted exactly: lunch, 100 attendees, ₹1,00,000, 2026-10-23, Bengaluru.
  - A re-run gave run count 2. Approve gave `approved`, and a run after that `409`.
  - The user saw: "Approved — draft PO … for ₹76,125 recorded. No purchase has been made yet.", the PO summary (status `draft`, approval `approved`) and the admin note. The view had only the 10 allowed fields.
  - The audit had 22 events with contiguous sequences, from `submit_ticket` to `approve`.
  - A dinner request for 120 people on ₹60,000 was parsed as dinner and came back `blocked`. Approve gave `409`, reject gave `rejected`, and the user saw "Rejected by admin. Note: Over budget, please revise."
  - Editing a ticket while `reviewed` gave `409`; editing a `submitted` one gave `200`. After logout, the old token got `401`.
- **Real Streamlit pages against the live API:** a new user signed up and submitted through the form; the admin logged in, saw the status counts, loaded and ran the ticket (6 tabs) and approved it, after which Run was disabled. The user then saw the decision and note with no internals, and no page raised an exception.
- `streamlit run ui\streamlit_app.py` (the shim) serves the app; the health check returned `ok`.

---

## 11. Phase 2: approval safety (spec `Docs/PHASE_2_APPROVAL_SAFETY.md`)

**Boundary:** approval records an internal human authorization only. No code path places an order, sends a PO, approves an invoice for payment or moves money. The PO `status` is always `draft`; only `approval_status` changes. The approval code never imports billing or procurement.

### 11.1 Roles and who may decide

| Role | How the account exists | Can decide approvals? |
|---|---|---|
| `user` (requester) | Self sign-up | No |
| `approver` | Server-configured via `APPROVER_ACCOUNTS` in `.env`; cannot be self-registered or escalated | **Yes**, except on their own requests |
| `admin` | `ADMIN_USERNAME` / `ADMIN_PASSWORD` | **No.** Runs workflows, sees the queue read-only, rejects `reviewed` tickets, reconciles |

- Roles come from the user record behind the session token, never from the request body or headers.
- `DecisionIn` uses `extra="forbid"`, so a body carrying `actor_id`, `role` or `status` gets `422`.
- This is demo-grade authentication, not production identity.

### 11.2 Ticket lifecycle with approvals

```mermaid
stateDiagram-v2
    [*] --> submitted: User submits
    submitted --> processing: Admin runs
    processing --> needs_info: needs_clarification
    processing --> reviewed: blocked / no draft PO (hard block - no approval request)
    processing --> pending_approval: draft PO + pass/warning<br/>approval request vN created
    pending_approval --> approved: Approver approves vN
    pending_approval --> rejected: Approver rejects vN (comment required)
    pending_approval --> changes_requested: Approver requests changes (comment required)
    pending_approval --> approval_expired: expires_at passed (server clock, never auto-approve)
    pending_approval --> processing: Admin re-runs (vN superseded)
    changes_requested --> submitted: User edits and resubmits
    approval_expired --> processing: Admin re-runs (new version)
    needs_info --> submitted: User edits and resubmits
    reviewed --> rejected: Admin rejects (blocked ticket)
    approved --> [*]
    rejected --> [*]
```

**Lifecycle rules:**

- **Approval states:** `pending` → `approved` / `rejected` / `changes_requested` / `expired` / `superseded`. Every non-pending state is terminal for that version.
- **New versions, never edits:** a new run or a resubmission creates a new version. Old versions stay in history.
- **`final_status` is never touched:** the workflow's verdict stays as computed.
- **No approval after a hard block:** `on_workflow_completed` creates no request, and a decision on a stored snapshot whose final status isn't pass or warning gives `409`.

### 11.3 Deterministic approval policy (`app/approvals/policy.py`, `data/approval_policy.json`)

`evaluate_approval_requirement(workflow_result)` uses only structured fields: `final_status`, the PO total, check statuses, attendee checks and critic finding statuses. It never reads LLM summaries.

| Condition | Result |
|---|---|
| `needs_clarification` | No request; the ticket goes to `needs_info` |
| `blocked`, or no draft PO | No request; hard block, ticket `reviewed` |
| Draft PO (pass/warning) | **Required**: `PO_REQUIRES_HUMAN_APPROVAL` (PROC-4.1: every system PO needs sign-off) |
| PO total > ₹50,000 | + `AMOUNT_THRESHOLD` (risk medium) |
| PO total > ₹1,50,000 | + `HIGH_VALUE` (risk high) |
| `final_status == warning` | + `POLICY_WARNING` (risk medium) |
| Attendee-limit check warns | + `LARGE_EVENT` (risk medium) |

- `policy_version: approval-policy-v1` and `required_role: approver`; requests expire after `expiry_hours: 72`.
- If evaluation raises, the run fails closed: `503`, no approval request is created, and the ticket is never shown as approved.

**Snapshot hash:** `review_snapshot()` takes the canonical review fields (ticket id, run count, submission, extracted request, PO lines and totals, final status, reconciliation status). `snapshot_hash()` is the SHA-256 of their sorted-key JSON. A decision must send the same `request_version` and `workflow_snapshot_hash`, so reviewers approve exactly what they saw. The hash detects change; it is not authorization.

### 11.4 Deciding: atomic, idempotent, separated

```mermaid
sequenceDiagram
    autonumber
    actor P as Approver
    participant UI as pages_portal/approvals.py
    participant API as routers/approvals.py
    participant S as approvals/service.decide()
    participant R as approval_repository
    participant DB as MongoDB

    P->>UI: pick action + comment, "Continue to confirmation", tick Confirm
    UI->>UI: idempotency_key generated once and reused on retry
    UI->>API: POST /api/approvals/{id}/decision {action, comment, idempotency_key, request_version, workflow_snapshot_hash}
    API->>S: decide(approval_id, actor from token, body)
    S->>R: get_approval (404 if missing or not visible)
    S->>S: lazy expiry (expired gives 409)
    S->>S: role == approver? (403) / actor != requester? (403) / comment rule (422)
    S->>R: find_decision_by_key: same payload gives a replay (200, replayed=true), different payload 409
    S->>R: decide_atomic: find_one_and_update {status: pending, request_version, hash, expires_at > now} + $push decision
    alt matched (the single winner)
        R->>DB: approval doc becomes approved / rejected / changes_requested
        S->>DB: ticket status (expected pending_approval), PO approval_status, ticket note, audit
        Note over S,DB: If a secondary write fails, reconciliation_required=true and the response is still the truthful 200. Admin can POST /api/admin/approvals/{id}/reconcile.
        API-->>UI: 200 {approval, replayed:false, reconciliation_required, message}
    else no match
        S->>S: classify: terminal / stale version or hash / expired
        API-->>UI: 409 safe message (UI warns and reloads)
    end
```

- **One winner under concurrency:** two approvers racing on one request produce exactly one decision; the other gets `409`. This was tested with threads on mongomock and real MongoDB, and with 6 parallel HTTP requests against real uvicorn.
- **Denied attempts are audited:** `approval_denied` records a safe category only (self_approval, wrong_role, stale, terminal, expired). Logging is best-effort and can never turn a denial into an allow.
- **Audit events:** `approval_requested`, `approval_superseded`, `approval_approved`, `approval_rejected`, `approval_changes_requested`, `approval_expired`, `approval_denied` and `approval_reconciliation_required`. Appends retry on a sequence collision, so the audit stays contiguous.

### 11.5 Phase 2 API

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/api/approvals?status=&limit=` | approver, admin (user `403`) | `{approvals: [ApprovalListItem], counts}`; a short summary, never the request text |
| GET | `/api/approvals/{approval_id}` | approver/admin any; requester own only (else `404`) | `ApprovalView` with `can_decide` / `cannot_decide_reason` and the safety notice |
| POST | `/api/approvals/{approval_id}/decision` | authenticated; the service enforces the role | `{approval, replayed, reconciliation_required, message}`; errors `401` / `403` / `404` / `409` / `422` / `503` |
| GET | `/api/tickets/{ticket_id}/approval` | owner, approver, admin | `TicketApprovalView` (current + history). The requester copy has no snapshot, hash or idempotency keys. |
| POST | `/api/admin/approvals/{approval_id}/reconcile` | admin | re-applies secondary updates idempotently |

**Collection `approval_requests`**

- Fields: `approval_id`, `ticket_id`, `request_version`, `ticket_version`, `status`, `requester_id`, `required_role`, `reason_codes`, `reasons`, `risk_level`, `amount`, `currency`, `policy_version`, `workflow_snapshot_hash`, `review_snapshot`, `created_at`, `expires_at`, `decided_at`, `decisions[]` (append-only `DecisionRecord`), `reconciliation_required`, `schema_version`.
- Indexes: `approval_id_unique`, `ticket_version_unique`, `status_created`, `requester_created`, and `one_pending_per_ticket` (partial unique on `ticket_id` where status is pending).

### 11.6 What changed for existing clients

1. **Admins can no longer approve.** `POST /api/admin/tickets/{id}/decision` with `approve` returns `403`. Admin reject works only on `reviewed` tickets, and returns `409` on `pending_approval` ones.
2. **Successful runs end in `pending_approval`**, not `reviewed`. `reviewed` now means blocked or no PO.
3. **New roles, statuses and fields:** role `approver`; statuses `pending_approval`, `changes_requested` and `approval_expired`; an `approval` field in both ticket views; new outcome texts such as "Waiting for approval by an approver." and "Rejected by an approver. Note: …".
4. **Re-runs:** a `pending_approval` ticket can be re-run (the pending version is superseded). Approved or rejected tickets still give `409`.
5. **UI:** the admin console's Approve button is gone. Approvers get a new **Approvals** page; admins get the same page read-only.
6. **Unchanged:** the agents, workflow, persistence order, legacy admin endpoints, user ticket endpoints and auth endpoints.

### 11.7 Phase 2 verification record (2026-10-17)

- `python -m pytest -q`: **257 passed**. That covers 39 policy, service and concurrency tests, 21 API tests, 8 real-MongoDB acceptance and persistence tests with real uvicorn, 20 approval UI tests, and every earlier test.
- **Live run** (LLM enabled, `.env` admin and approver accounts, throwaway database):
  - A happy-path run ended in `pending_approval` with v1 (`PO_REQUIRES_HUMAN_APPROVAL`, `AMOUNT_THRESHOLD`, medium, ₹76,125, approval-policy-v1).
  - Requester decision `403`; admin decision `403`; legacy admin approve `403`; smuggled `actor_id` `422`; stale hash `409`; reject without a comment `422`.
  - The approver's approve returned `200` with the message "…no order or payment was executed". A replay with the same key returned `replayed=true`; the same key with another payload `409`; a new key `409`.
  - The user saw "Approved…" with the PO `draft` and approval `approved`. No ordered or paid claims and no hash or keys appeared in the user view, and the audit was contiguous.
- **After an API restart:** the approval was still `approved` with one decision by `approver` (approver), and the ticket was `approved`.
- **Other paths:**
  - Reject: the user saw "Rejected by an approver. Note: …".
  - Request changes, then user edit and admin re-run: v2 pending and v1 changes_requested; approving the stale v1 gave `409` and v2 was approved.
  - Re-run while pending: v1 superseded, and approving the old version gave `409`.
  - Hard block (120 attendees on ₹60,000): no approval request, admin reject worked.
  - A ₹29,400 ticket: still requires sign-off, risk low.
  - Forced expiry: approve gave `409 "Approval request has expired."`, the ticket became `approval_expired`, and an admin re-run produced v2 pending.
- **Database:** every PO `status` is `draft`; all 5 approval indexes exist.
- **Real Streamlit pages against the live API:**
  - Approver: the queue showed counts by status, the detail showed the safety notice and the "Draft — no order placed, nothing paid" label, and history read "Approved by `approver` (approver) at … — 'Within policy.'".
  - "Continue to confirmation" sent nothing; Confirm stayed disabled until the checkbox was ticked; after confirming, the server status was `approved`.
  - The admin console had no Approve button and the user portal had no decision controls, with no exceptions on any page.

---

## 12. Phase 2: policy RAG, multi-PDF library and rate cards (spec `Docs/PHASE_2_POLICY_RAG_AND_APPROVAL_IMPLEMENTATION.md`)

**Design rule:**

1. RAG finds the evidence.
2. Structured extraction supplies candidate facts.
3. Deterministic code (`Decimal`) validates amounts and enforces rules.
4. A human approver decides.

Nothing places an order or a payment. The existing Markdown policy agent (`app/agents/policy.py`) is unchanged. The PDF library adds a second, cited evidence layer next to it.

### 12.1 Modules

```text
app/policy_rag/
  schemas.py      PolicyDocument, PolicyChunk, PolicyEvidence, PolicyFinding, RateCardEntry, RateQuote,
                  RateValidationResult, PolicyEvaluationResult, EvidenceSnapshot (+ enums)
  storage.py      PDF bytes in POLICY_STORE_DIR (default data/policy_store, git-ignored); paths never exposed
  pdf_extract.py  pypdf page-aware extraction, low-text page detection, letter-spacing normalization, typed errors
  chunking.py     page-bounded chunks (~450 tokens, 60 overlap) with detected section headings
  embeddings.py   OpenAIEmbedder (text-embedding-3-small via API_KEY) | LocalHashEmbedder (offline, 512-d)
  ingestion.py    ingest_bytes / ingest_batch / seed_from_directory / reprocess / archive; versioning + supersede
  retrieval.py    access + applicability filters -> BM25 + cosine -> reciprocal-rank fusion -> cited evidence
  rate_cards.py   extract_rate_entries (menu tables -> Decimal rows, all needs_review), verify_entry, validate_quote
  evaluation.py   evaluate_ticket -> findings with evidence_ids, outcome, approval_reason_codes, evaluation_hash
  events.py       append-only policy_events (no document text)
  errors.py       PolicyRagError(status_code, detail) -> safe JSON
app/routers/policy_documents.py   /api/policy-documents...
app/routers/policy_search.py      /api/policy/search, /api/policy/validate-rate, /api/requests/{id}/..., /api/rate-cards...
app/repositories/policy_document_repository.py, policy_chunk_repository.py, rate_card_repository.py,
                 evidence_snapshot_repository.py
data/policy_rag_config.json        rule_set_version, limits, chunking, embedding model, tolerance, vendor key maps, review topics
data/policies/manifest.json        document types / logical keys / versions of the seeded PDFs
ui/pages_portal/policy_library.py  Upload · Inventory · Search · Rate cards
ui/lib/policy_render.py            evidence panel (outcome badges, findings, citations, rate table, hash)
```

### 12.2 Ingestion pipeline (per file, independent within a batch)

```mermaid
flowchart LR
    U[Admin uploads N PDFs<br/>POST /api/policy-documents/upload<br/>files + metadata JSON list] --> V{validate<br/>.pdf + %PDF- magic<br/>size, pages, encryption}
    V -->|fail| F[DocumentUploadResult ok=false<br/>error_code: not_pdf / corrupt / encrypted / too_large / too_many_pages]
    V --> H[sha256 content hash]
    H -->|same hash, not archived| D[ok=true, duplicate_of=doc_id<br/>nothing re-ingested]
    H --> S[store bytes: doc_id.pdf]
    S --> X[extract pages pypdf<br/>low-text pages flagged]
    X --> C[page-bounded chunks + sections]
    C --> E[embed chunks<br/>OpenAI or local hash]
    E --> I[policy_chunks indexed]
    I --> R{document_type == rate_card?}
    R -->|yes| RC[extract rate rows<br/>Decimal, needs_review]
    R -->|no| ST
    RC --> ST{status}
    ST -->|ok| READY[ready]
    ST -->|"low text > 30% / no chunks"| NR[needs_review]
    ST -->|image-only| OCR[needs_ocr]
    READY --> VS[previous version of same logical_key -> superseded<br/>never overwritten]
```

**Pipeline rules:**

- **Server-set fields:** `uploaded_by`, `status`, the hash and `document_id` are set by the server. Metadata uses `extra="forbid"`, so a smuggled `uploaded_by` gets `422`, and so does an effective range that ends before it starts.
- **Untrusted text:** PDF text is treated as data. LLM prompts wrap excerpts as quoted data, and LLM output is advisory only (`source: "llm"`); it never changes an outcome.
- **Events:** `document_uploaded`, `document_validation_failed`, `document_extraction_completed`, `document_indexing_completed`, `document_marked_needs_review`, `document_version_superseded`, `document_archived`, `rate_card_extraction_created`, `rate_card_verified`, `policy_evaluation_completed` and `policy_conflict_detected`.
- **Seeding:** at startup (`POLICY_RAG_SEED`, default on), PDFs in `data/policies` are ingested by manifest, matched by content hash so a restart never re-ingests them.

### 12.3 Retrieval

1. **Filters first:**
   - Status `ready` only. Archived, superseded, `needs_review` and `needs_ocr` documents are excluded unless `include_historical` is set.
   - The `as_of` date must fall within the effective dates.
   - Vendor applicability: a chunk is used if its vendor matches the query vendor, or if it has no vendor (company-wide). Another vendor's documents are never used.
2. **BM25 and cosine:** BM25 runs over the filtered chunks, and cosine similarity runs on the vectors (only when the query and chunk embedding models match).
3. **Fusion:** reciprocal-rank fusion, then deduplication.
4. **Results:** the top k come back with **real** scores and excerpts taken from the actual chunk text.

`retrieval_mode` is `hybrid`, or `keyword_only_degraded` with a warning when no compatible vectors exist. If the best match is weak, or nothing matches, the result is `insufficient_evidence: true` and no citations are invented.

### 12.4 Rate cards (structured, deterministic)

- **Extraction:**
  - Menu pages are split into vendor records: `Record: VEN-00x` maps to `vendor_key` `evt-ven-00x`.
  - Event vendors are deliberately namespaced apart from the catering vendors (`cat-ven-00x`), and vendors are never matched by fuzzy name.
  - Each package or add-on row becomes a `RateCardEntry`: `unit_rate` as a Decimal string (`₹1,45,000` → `145000.00`), the unit (event / person / serving / item), `tax_treatment` (`excluded` only when the page says so) and the source page.
  - **Every row starts as `needs_review`.**
- **Verification:** `POST /api/rate-cards/{id}/verify` (approver or admin) with `verify` or `reject`. A second decision gives `409`, and each decision is audited.
- **Validation** (`POST /api/policy/validate-rate`):
  - The vendor key and item code must match exactly, the quote date must fall in the validity window, and the currency and unit must match.
  - The expected total is `rate × qty` in `Decimal`, compared within a ₹1.00 tolerance.
  - Results:
    - An unverified row gives `needs_review`.
    - Several plausible verified rows give `conflict`.
    - No row gives `unknown`.
    - A wrong currency gives `needs_review`.
  - Tax and surcharges are never invented.

### 12.5 Ticket evaluation and approval binding

```mermaid
sequenceDiagram
    autonumber
    participant A as Admin (Run workflow)
    participant TS as ticket_service.run_ticket
    participant WF as orchestrator + agents
    participant EV as policy_rag.evaluation
    participant RT as retrieval / rate_cards
    participant SN as evidence_snapshot_repository
    participant AP as approvals.policy + service

    A->>TS: POST /api/admin/tickets/{id}/run
    TS->>WF: run_workflow (unchanged)
    WF-->>TS: WorkflowResult (persisted, ticket stays processing)
    TS->>EV: evaluate_ticket(ticket, workflow_result)   [POLICY_RAG_ENABLED]
    EV->>RT: queries from category/service/attendees/location/vendor
    RT-->>EV: cited evidence (+ rate validation for evt- vendors)
    EV-->>TS: PolicyEvaluationResult (findings, outcome, approval_reason_codes, evaluation_hash)
    TS->>SN: create_snapshot (append-only, unique ticket_id+run_count)
    Note over TS,SN: any exception here -> fail closed: existing 503, no approval request
    TS->>AP: evaluate_approval_requirement(workflow_result, policy_evaluation=...)
    AP-->>TS: requirement (policy codes ADDED, risk >= medium, blocks never removed)
    TS->>AP: on_workflow_completed -> approval vN with review_snapshot.policy_evaluation {evaluation_id, evaluation_hash, rule_set_version, documents_used}
```

**Findings** (`PolicyFinding`): `category`, `severity` (info / warning / review_required / hard_block), `status` (pass / fail / unknown / conflict / not_applicable), `summary`, `reason`, `evidence_ids` (a subset of the returned evidence, checked in code), `rule_id` and `source`.

**Initial rules (`policy-rag-rules-v1`):**

| Rule | When | Result |
|---|---|---|
| Policy located | Applicable company/vendor evidence found | info · pass (cited) |
| Vendor verification | A vendor is selected | **review_required** · unknown: facilities, licences and insurance must be verified (cited from the Event Vendor & Venue Policy) |
| Safety review | Attendees ≥ 150 | review_required (cited) |
| Privacy review | The request mentions personal data or photos | review_required (cited) |
| Conflict | Overlapping verified rate rows, or conflicting thresholds without configured precedence (`precedence: []`) | conflict → review |
| Rate card | Catering vendor (`cat-`) | `not_applicable` (catering has no rate card; never blocks) |
| No evidence | Library empty or no relevant match | `INSUFFICIENT_POLICY_EVIDENCE` → review (never a pass) |

**How the outcome feeds the approval:**

- **Outcome:** `hard_block` if any finding is a hard block (none configured by default), `review_required` if any finding needs review or conflicts, `unknown` if there is no evidence, otherwise `pass`.
- **Codes added to the approval request:** `POLICY_REVIEW_REQUIRED`, `RATE_CARD_MISMATCH`, `RATE_CARD_UNVERIFIED`, `POLICY_CONFLICT`, `INSUFFICIENT_POLICY_EVIDENCE` and `POLICY_RETRIEVAL_DEGRADED`.
- **Limits:** RAG can never remove a hard block or a review requirement, and never approves.
- **Version binding:** the approval's `review_snapshot` includes the evaluation hash. Changing the evidence changes `workflow_snapshot_hash`, so a stale decision gets `409`.
- **Re-checking:** `POST /api/requests/{id}/evaluate-policy` (admin) re-evaluates without re-running the workflow and reports `stale_against_approval`. It never supersedes anything on its own.

### 12.6 Policy RAG API

| Method | Path | Who | Returns |
|---|---|---|---|
| POST | `/api/policy-documents/upload` | admin | `{results: [DocumentUploadResult]}`. Always `200` with per-file results; `413` if the batch is too large; `422` for bad metadata. |
| GET | `/api/policy-documents?document_type=&vendor_key=&status=&logical_key=` | approver, admin | `{documents, counts}`: metadata only, never text or paths |
| GET | `/api/policy-documents/{document_id}` | approver, admin | metadata + safe event history |
| POST | `/api/policy-documents/{document_id}/reprocess` | admin | `{result, document}` |
| POST | `/api/policy-documents/{document_id}/archive` | admin | `{document}` (excluded from search; audit kept) |
| POST | `/api/policy/search` | approver, admin | `PolicySearchResult {query_id, evidence[], retrieval_mode, insufficient_evidence, warnings}` |
| POST | `/api/policy/validate-rate` | approver, admin | `RateValidationResult` (Decimal strings) |
| GET | `/api/rate-cards?vendor_key=&verification_status=` | approver, admin | `{rate_cards, count}` |
| POST | `/api/rate-cards/{rate_id}/verify` | approver, admin | `{"decision": "verify" or "reject"}`; `409` if already decided |
| POST | `/api/requests/{ticket_id}/evaluate-policy` | admin | `{evaluation, snapshot_created, stale_against_approval}` |
| GET | `/api/requests/{ticket_id}/evidence` | admin/approver: full snapshot; owner: **redacted**; others `404` | The owner's view has finding summaries and citation labels (file, version, page, section), with no excerpts, hashes or scores. |

Users (requesters) get `403` on the library, search and rate cards.

**New collections and indexes:**

- `policy_documents`: `document_id_unique`, `content_hash`, `type_status`, `vendor_type_status`, `effective_range`, `logical_key_version_unique`
- `policy_chunks`: `chunk_id_unique`, `doc_page`, `chunk_status_type`
- `rate_card_entries`: `rate_id_unique`, `rate_vendor_item_validity`, `rate_document`, `rate_verification`
- `policy_evidence_snapshots`: `snapshot_ticket_run_unique`
- `policy_events`: `policy_event_time`, `policy_event_document`

### 12.7 UI

**Policy library** (admin; approver read-only plus rate verification):

- **Upload:** multiple PDFs with metadata per file, and results per file.
- **Inventory:** status, version, effective dates and supersede/duplicate markers. Reprocess and archive need a confirmation tick.
- **Search:** evidence cards (file · version · page · section · excerpt · score), the retrieval mode, a degraded warning and an insufficient-evidence message.
- **Rate cards:** verify or reject each row, showing its source page.

**Evidence panel** (admin ticket detail and approvals detail):

- Outcome badges that distinguish pass, review required, hard block and unknown, plus each finding's status and severity.
- Citations, the rate table, missing evidence, conflicts, and the hash prefix with the rule set version.
- On approve, it shows exactly: "Approved internally — purchase order remains a draft; no order or payment has been executed."

The user portal shows the redacted "Policy check" only.

### 12.8 What changed for existing behaviour

1. **Policy check on every portal run** (`POLICY_RAG_ENABLED`, default **on**):
   - Approval requests now also carry `POLICY_REVIEW_REQUIRED`, or `INSUFFICIENT_POLICY_EVIDENCE` if the library is empty, and their risk is at least `medium`.
   - Snapshot hashes include the evaluation binding, so they differ from Phase 2 approval-only hashes.
2. **Failures:** if the policy check or saving its snapshot fails, the run takes the existing fail-closed `503` path and no approval request is created.
3. **Ticket views gain two fields:** `policy_evaluation` (admin) and `policy_evidence` (requester, redacted).
4. **Startup seeding:** the first start ingests the 3 PDFs in `data/policies`. With OpenAI embeddings this takes about 15 seconds; later starts skip them.
5. **Tests:** `tests/conftest.py` defaults to `POLICY_RAG_ENABLED=0` and `POLICY_RAG_SEED=0` with a temporary `POLICY_STORE_DIR`, so the earlier 257 tests are unchanged. The new RAG tests opt in. The three real-uvicorn tests now also pass `POLICY_RAG_SEED=0` and a temporary store, so test runs never write into `data/policy_store`.
6. **Unchanged:** the agents, catering costing, vendors.json, the legacy `/api/workflow`, auth, and every Phase 2 approval rule.

### 12.9 Policy RAG verification record (2026-10-17)

- `python -m pytest -q`: **350 passed** (257 earlier + 93 new). The new tests cover ingestion (23), retrieval (12), rate cards (9), evaluation (18), authorization (5), no external execution (2), UI (14), and real-MongoDB plus uvicorn acceptance (3, spec §18.6 and the real PDFs).
- **Live run** (LLM and OpenAI embeddings on, throwaway database, seeding from your 3 real PDFs):
  - **Seeding:** Company Policy 16 pages / 77 chunks, Vander Policy 10 / 26, Vander Manu 6 / 12 with **52 rate rows** across `evt-ven-001` to `evt-ven-006`. All three were `ready`, with 1536-dimension `text-embedding-3-small` vectors and no storage paths exposed.
  - **Access:** users got `403` on the library and search.
  - **Search:**
    - "vendor insurance licenses verification" (hybrid): Vander Policy.pdf p. 3 § 5, p. 4 § 9, p. 5 § 9.1 and p. 10 § 10.
    - "risk assessment first aid supervision": Company Policy p. 4 § 4.1, p. 11 § 11.4 / 11.6 and Vander Policy p. 4 § 6.3.
    - A nonsense query gave `insufficient_evidence`.
  - **Catering ticket with INV-5001:**
    - The ticket became `pending_approval` and the evaluation came back `review_required` (hybrid). Vendor verification was cited from 3 real chunks, every evidence ID was valid, and catering was `not_applicable` for rate cards.
    - The approval codes were `PO_REQUIRES_HUMAN_APPROVAL`, `AMOUNT_THRESHOLD` and `POLICY_REVIEW_REQUIRED` (medium). The bound hash matched the snapshot, and the requirement panel matched the stored request (a gap fixed during integration).
    - The requester's evidence was redacted with no excerpts or hashes, and another user got `404`.
    - The approver approved with the draft-only message, and a re-evaluation reported `stale_against_approval=false`.
  - **Rate validation:**
    - Before verification: `needs_review`. Verification: user `403`, approver `200`.
    - Quote 42000 → `pass` (diff 0.00, source page 1). Quote 45500.50 → `fail` (diff 3500.50, 8.33%). USD currency → `needs_review`.
  - **Mixed upload batch:** privacy_policy.pdf ok (`needs_review`, 1 of 2 pages low-text), broken.pdf → `corrupt`, notes.txt → `not_pdf`, and the real Vander Policy → `duplicate_of`. A reversed date range and a smuggled `uploaded_by` both gave `422`.
  - **After an API restart:** still 3 documents (no re-seed), the ticket and approval `approved`, the snapshot hash unchanged, the verified rate row kept, and every PO `draft`.
- **Real Streamlit pages against the live API:** the Policy library showed the Upload / Inventory / Search / Rate cards tabs with the real documents, and search returned 8 citations with file, version, page, section and excerpt. The admin ticket detail showed the evidence panel with the rule set version, and there were no exceptions.
