# Phase 1 Implementation Plan — MongoDB Persistence

**Project:** WorkplaceOps AI — Request-to-Payment Copilot  
**Repository:** `Sinha28Shivam/Singularity_Squad`  
**Stack:** Python, FastAPI, Pydantic v2, Streamlit, PyMongo, MongoDB

## 1. Objective

Persist each workflow run so the same ticket, draft purchase order (PO), and audit history can be retrieved after refreshing the UI or restarting FastAPI.

### Deliverables
- MongoDB connection configuration through environment variables.
- One shared MongoDB client.
- A repository/data-access layer separate from HTTP routes and agents.
- Persistent tickets, draft POs, and sequenced agent audit events.
- `GET /api/tickets/{ticket_id}` and `GET /api/tickets/{ticket_id}/audit`.
- Unique ticket IDs.
- Tests for persistence, API behavior, and existing workflow behavior.

### Out of scope
- Persisted human approval/rejection (Phase 2).
- PO issuance, purchases, or payments.
- Replacing JSON seed files for vendors, invoices, delivery records, or policy rules.
- Real integrations with procurement, calendars, vendors, or finance systems.

**Safety:** this remains a synthetic-data, draft-only prototype. Saving a draft PO does not approve or issue it.

## 2. Current state

The repository already has `app/main.py`, `app/orchestrator.py`, `app/schemas.py`, `app/data_loader.py`, the agent modules, Streamlit UI, synthetic JSON data, and tests.

Currently, workflow results and audit entries are assembled in memory per request. Reference data is read from JSON. Phase 1 should add persistence around the current workflow, not rewrite the agents.

## 3. Architecture

```text
Streamlit UI
   | HTTP/JSON
   v
FastAPI (app/main.py)
   |
   v
Orchestrator + existing agents
   |
   v
Repository layer (app/repositories/)
   |
   v
MongoDB: tickets | purchase_orders | agent_logs
```

- **Routes:** validate requests, call services/repositories, return HTTP responses.
- **Orchestrator:** runs existing agents and creates `WorkflowResult`.
- **Repositories:** encapsulate MongoDB reads/writes.
- **MongoDB:** stores ticket snapshots, draft POs, and audit events.
- **JSON files:** remain the synthetic reference-data source for this phase.

Do not place MongoDB queries inside agent modules.

## 4. Environment and dependency

Add to `requirements.txt`:

```text
pymongo>=4.6
```

Create `.env.example`:

```dotenv
MONGODB_URI=mongodb://localhost:27017/
MONGODB_DATABASE=workplaceops
WORKPLACEOPS_PERSISTENCE=required
```

Keep actual `.env` credentials out of Git. Add `.env`, `.env.*`, and `!.env.example` to `.gitignore` as appropriate.

Use either local MongoDB or Atlas. For Atlas, use the connection string from Atlas, URL-encode special characters in credentials, and restrict access to the application database/user.

## 5. Connection module

Create:

```text
app/db/__init__.py
app/db/mongo.py
app/db/indexes.py
```

Suggested starting point for `app/db/mongo.py`:

```python
import os
from functools import lru_cache

from pymongo import MongoClient
from pymongo.database import Database

@lru_cache(maxsize=1)
def get_client() -> MongoClient:
    return MongoClient(
        os.environ["MONGODB_URI"],
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
    )

def get_database() -> Database:
    return get_client()[os.getenv("MONGODB_DATABASE", "workplaceops")]

def ping_database() -> bool:
    get_client().admin.command("ping")
    return True
```

This is a starting point: wire a ping into FastAPI startup/health handling, close the shared client on application shutdown, and catch connection exceptions. Do not ping at module import time or log the URI/password. Do not create a new `MongoClient` for every request.

**Failure policy:** with `WORKPLACEOPS_PERSISTENCE=required`, do not return success if persistence fails. Return a safe error and log details server-side. Do not silently fall back to memory in the demo.

## 6. Collections and indexes

### `tickets`
One document per workflow ticket.

```json
{
  "ticket_id": "REQ-<unique-id>",
  "created_at": "2026-10-17T10:00:00Z",
  "updated_at": "2026-10-17T10:00:02Z",
  "status": "pass",
  "raw_text": "Organize lunch for 100 employees...",
  "workflow_result": {
    "final_status": "pass",
    "human_approval_required": true
  },
  "schema_version": 1
}
```

Store the full JSON-compatible `WorkflowResult` snapshot for the MVP. Use `model_dump(mode="json")` to serialize Pydantic models. Keep `_id` out of public responses or convert it to a string.

Indexes:
- Unique: `ticket_id`
- Optional: `updated_at`

### `purchase_orders`
One document per draft PO.

```json
{
  "po_id": "DRAFT-PO-<unique-id>",
  "ticket_id": "REQ-<unique-id>",
  "status": "draft",
  "vendor_id": "VEN-002",
  "total": 76125.0,
  "currency": "INR",
  "requires_human_approval": true,
  "created_at": "2026-10-17T10:00:01Z",
  "schema_version": 1
}
```

Persist all fields from the actual `DraftPO`, plus `ticket_id` and timestamps. Do not hard-code the example values. Do not store a PO if `result.procurement.po` is `None`.

Indexes:
- Unique: `po_id`
- Non-unique: `ticket_id`

### `agent_logs`
One document per existing `AuditEntry`.

```json
{
  "ticket_id": "REQ-<unique-id>",
  "sequence": 4,
  "agent": "Planner & Vendor Matcher",
  "action": "match_vendors",
  "status": "warn",
  "detail": "Preferred vendor unavailable; selected eligible alternative.",
  "evidence": [],
  "llm_used": false,
  "timestamp": "2026-10-17T10:00:01Z",
  "schema_version": 1
}
```

Indexes:
- Compound unique: `(ticket_id, sequence)`
- Query index: `(ticket_id, timestamp)`

Use structured rationale/evidence, not private model chain-of-thought. Persist the existing audit trail instead of creating a separate logging system.

## 7. Suggested file changes

Add:

```text
app/
  db/
    __init__.py
    mongo.py
    indexes.py
  repositories/
    __init__.py
    ticket_repository.py
    purchase_order_repository.py
    audit_repository.py
```

Modify `requirements.txt`, `.env.example`, `.gitignore`, `app/main.py`, `app/orchestrator.py`, and tests. Change `app/schemas.py` only if a small response model is needed. Keep agent business logic unchanged where possible.

## 8. Repository interfaces

Implement a small, testable interface.

### `ticket_repository.py`
- `create_ticket(document)`
- `get_ticket(ticket_id)`
- `update_ticket_result(ticket_id, result, status, updated_at)`
- Optional: `list_recent_tickets(limit=20)`

### `purchase_order_repository.py`
- `upsert_draft_po(ticket_id, draft_po, created_at)`

Use an upsert keyed by `po_id` so retrying the same write does not create duplicate draft POs.

### `audit_repository.py`
- `append_audit_entries(ticket_id, entries)`
- `get_audit_entries(ticket_id, limit=500)`

Sort by ascending `sequence`. Use the compound unique index to avoid duplicate event sequence numbers. Define whether a retry is ignored or treated as a conflict; do not silently overwrite an existing event with different content.

Create indexes once through an idempotent initialization function, not before every insert.

## 9. Ticket IDs and workflow persistence

The current orchestrator defaults to `REQ-1001`, which is useful for deterministic tests but unsafe for multiple real/demo submissions. Generate a unique ticket ID in the API layer before calling the workflow, then pass it as `request_id`.

For `POST /api/workflow`:

1. Validate the request.
2. Generate a unique ticket ID.
3. Insert a ticket with status `received`, request text, and timestamps.
4. Run the existing `run_workflow(..., request_id=ticket_id)`.
5. Serialize the final `WorkflowResult`.
6. Update the ticket with result, status, and `updated_at`.
7. Persist a draft PO if present.
8. Persist audit entries in sequence order.
9. Return the result and ticket ID.

If a write fails, do not return a success response implying the workflow was persisted. A ticket may remain with `failed` or `persistence_error` status if a later write fails; document this. Multi-document atomicity/outbox processing can be considered in a later phase.

## 10. API endpoints

Keep existing endpoints intact.

### `GET /api/tickets/{ticket_id}`
- `200`: ticket and saved workflow result.
- `404`: ticket does not exist.
- `503`: MongoDB unavailable.

### `GET /api/tickets/{ticket_id}/audit`
- `200`: audit events ordered by ascending sequence.
- `404`: ticket does not exist.
- `503`: MongoDB unavailable.

Example:

```json
{
  "ticket_id": "REQ-...",
  "events": [
    {
      "sequence": 1,
      "agent": "Orchestrator",
      "action": "start",
      "status": "info",
      "detail": "Received request"
    }
  ]
}
```

Optional: `GET /api/tickets?limit=20` for recent-ticket history, with a strict maximum limit.

Extend `/api/health` with a database status while preserving existing fields (`status`, `llm_enabled`, `model`). Never return credentials or connection strings.

## 11. Streamlit updates

1. Display the ticket ID returned from `POST /api/workflow`.
2. Add a ticket-ID field and “Load ticket” action calling `GET /api/tickets/{ticket_id}`.
3. Add “Load audit history” calling `GET /api/tickets/{ticket_id}/audit`.
4. Render the saved events in the existing audit table/expanders.
5. Show a clear error when the backend/database is unavailable.

Streamlit `session_state` may cache display data but is not the source of truth.

## 12. Reliability and security rules

- Do not expose URIs, credentials, exception traces, or confidential payloads in API responses.
- Distinguish missing tickets (`404`) from database failure (`503`).
- Never claim persistence succeeded when a write failed.
- Use one shared client and timezone-aware UTC timestamps.
- Bound audit retrieval with a maximum limit.
- Use unique indexes and idempotent upserts where appropriate.
- Avoid logging raw request text if it could contain confidential information.
- Persist `llm_used` and evidence, not private chain-of-thought.
- Keep POs in `draft` state; do not implement issue/purchase/payment actions.

## 13. Testing plan

Tests should not require a developer's personal Atlas cluster. Inject a test repository or use a test-only MongoDB mock. If using `mongomock`, add it as a test dependency and verify real MongoDB index/connection behavior in a local or isolated test database too.

### Repository tests
- Pydantic dates/nested models serialize correctly.
- Unknown ticket returns `None`.
- Audit events are sorted by sequence.
- Duplicate `(ticket_id, sequence)` events are not duplicated.
- Draft PO is saved only when present.
- Repeated PO upsert does not duplicate the document.
- DB errors are mapped to safe API errors.

### API tests
- `POST /api/workflow` returns a unique ticket ID.
- Workflow result is persisted and can be retrieved.
- Audit endpoint returns saved events in order.
- Unknown ticket returns `404`.
- DB failure returns a non-success response.
- Existing health, samples, invoices, workflow, and reconcile tests still pass.

### Integration acceptance test
1. Start MongoDB and FastAPI.
2. Submit a happy-path request.
3. Copy the ticket ID from the response.
4. Verify the ticket, draft PO, and audit entries exist in MongoDB.
5. Restart FastAPI.
6. Retrieve the same ticket and audit events through the API.
7. Confirm the PO remains `draft`; no purchase or payment occurred.
8. Run the full existing test suite.

Commands:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
python -m pytest -q
```

Use `http://127.0.0.1:8000/docs` to test the endpoints.

## 14. Definition of done

- [ ] MongoDB configuration is environment-driven; no secrets are committed.
- [ ] Shared client and DB health check work.
- [ ] Collections have the required indexes.
- [ ] Every new workflow receives a unique ticket ID.
- [ ] Workflow result is saved to `tickets`.
- [ ] Draft PO is saved when one is created.
- [ ] Audit entries are saved with stable sequence numbers.
- [ ] Ticket retrieval works after API restart.
- [ ] Audit retrieval returns events in order.
- [ ] Streamlit can reload a ticket and its audit history.
- [ ] Database failure cannot masquerade as successful persistence.
- [ ] Existing and new tests pass.
- [ ] No automatic purchase, PO issuance, approval, or payment is possible.

## 15. Recommended work order

1. Add PyMongo, environment variables, connection module, and DB health.
2. Create indexes and repository functions; test them in isolation.
3. Generate unique ticket IDs and persist ticket results.
4. Persist draft POs and audit entries.
5. Add ticket and audit retrieval endpoints.
6. Add Streamlit ticket/history controls.
7. Verify restart persistence and run the full test suite.

For a four-person team, split work into: (1) DB connection/indexes, (2) repositories/tests, (3) API/orchestrator integration, and (4) UI retrieval/end-to-end verification. Agree on collection names and function signatures before parallel implementation.

## References

- PyMongo connection: https://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/
- PyMongo CRUD: https://www.mongodb.com/docs/languages/python/pymongo-driver/current/crud/
- PyMongo connection pools: https://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/connection-options/connection-pools/
