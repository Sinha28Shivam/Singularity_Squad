# MongoDB Phase 1 — Fixes and Improvements Checklist

**Project:** WorkplaceOps AI — Request-to-Payment Copilot  
**Repository:** `Sinha28Shivam/Singularity_Squad`  
**Scope:** Remediation of the existing MongoDB persistence implementation  
**Goal:** Make Phase 1 consistent, secure against accidental secret commits, and verifiably persistent across API restarts.

> This document is an implementation checklist, not evidence that fixes have already been applied. The repository code should be updated and the checks below run before Phase 1 is marked complete.

---

## 1. Priorities at a glance

| Priority | Item | Why it matters | Completion evidence |
|---|---|---|---|
| P0 | Fix the persisted-flag mismatch | A stored workflow result can say `persisted: false` even when the API response says it was saved | Test stored result and API response agree |
| P0 | Correct `.gitignore` encoding and verify `.env` is untracked | Prevent accidental commits of MongoDB credentials | `git check-ignore -v .env` succeeds and `git ls-files .env` prints nothing |
| P1 | Correct `.env.example` and README encodings | Avoid a BOM interfering with the first environment variable and repair corrupted symbols | Files are UTF-8 without BOM; symbols render correctly |
| P1 | Verify persistence across a real API restart | `mongomock` tests alone do not prove durable MongoDB behavior | Real-Mongo integration test or documented manual test passes |
| P1 | Test partial-write failures | Ticket, PO, and audit writes are separate operations | Failure tests show the ticket is marked appropriately and partial state is diagnosable |
| P2 | Minimize request text in list responses | Avoid returning more user-provided text than the UI needs | Recent-ticket endpoint returns only necessary fields |
| P2 | Document and verify deployment configuration | Local and Atlas setups should fail clearly and safely | Startup/health and configuration checks documented |

---

## 2. Fix the persisted-result mismatch

### Problem

The workflow result is serialized before its persisted state is updated. The API response may report `persisted: true`, while the `workflow_result` document saved in MongoDB contains `persisted: false`.

The Streamlit loader uses `setdefault("persisted", True)`. `setdefault` does not replace an existing `False` value, so the incorrect value can remain visible after loading a saved ticket.

### Recommended fix

In `app/services/ticket_service.py`, make the saved and returned representations consistent.

Preferred approach:
1. Run the workflow.
2. Persist the ticket, draft PO, and audit entries according to the existing error-handling flow.
3. Only after the required persistence operations succeed, set `result.persisted = True`.
4. Save the final serialized result to MongoDB, or update the stored result once persistence succeeds.
5. Return the same final result to the API caller.

Be careful about the order: setting `persisted = True` before database writes succeed would incorrectly claim success if a later write fails.

If the current multi-collection write sequence makes that ordering awkward, keep the ticket's top-level `status` authoritative and explicitly normalize the flag in the stored workflow snapshot after all required writes succeed.

In `ui/streamlit_app.py`, when a result has just been retrieved successfully from the database, the UI may normalize the display copy to `persisted = True`; do not use that UI adjustment as a substitute for fixing the saved document.

### Tests to add or update

- [ ] A successful workflow response contains `persisted: true`.
- [ ] `GET /api/tickets/{ticket_id}` returns a stored `workflow_result.persisted` value of `true`.
- [ ] Loading that ticket in Streamlit displays it as persisted.
- [ ] If a required write fails, the API does not return a successful persisted result.
- [ ] Existing workflow outcome and invoice reconciliation tests continue to pass.

---

## 3. Fix `.gitignore` and verify secrets are not tracked

### Problem

The committed `.gitignore` appears to contain a UTF-8 byte-order mark (BOM) before its first `.env` pattern. Depending on the exact bytes and Git behavior, the pattern may not match `.env` as intended.

### Recommended fix

1. Open `.gitignore` in VS Code.
2. Use **Save with Encoding → UTF-8** (not UTF-8 with BOM).
3. Ensure the file begins with the literal pattern `.env` on the first line.
4. Keep `.env.example` explicitly allowed.
5. Check whether a real `.env` file has ever been tracked. Ignore rules do not untrack files that are already in the index.

Suggested `.gitignore` content:

```gitignore
.env
.env.*
!.env.example
__pycache__/
*.pyc
.pytest_cache/
.venv/
venv/
```

### Verify in PowerShell

Run these from the repository root:

```powershell
git check-ignore -v .env
git ls-files .env
```

Expected:
- `git check-ignore -v .env` identifies the `.env` ignore rule.
- `git ls-files .env` prints no tracked file.

If `.env` is already tracked, remove it from the Git index without deleting your local file:

```powershell
git rm --cached .env
```

Then commit the index change. If real credentials were already pushed, rotate/revoke them; removing the file in a later commit does not erase the secret from Git history.

**Never put real MongoDB credentials in `.env.example`.**

---

## 4. Repair `.env.example` and README encoding

### Problem

The latest commit appears to introduce encoding artifacts in `README.md` (for example, corrupted arrow and rupee symbols). `.env.example` also appears to begin with a BOM. A BOM before the first variable can cause a parser or shell workflow to treat its name incorrectly.

### Recommended fix

- [ ] Save `.env.example` as UTF-8 without BOM.
- [ ] Save `README.md` as UTF-8 without BOM.
- [ ] Restore corrupted characters, including arrows and `₹`, where intended.
- [ ] Confirm `.env.example` starts exactly with a comment or `API_KEY=`, with no hidden characters.
- [ ] Copy `.env.example` to `.env` and verify the first variable is read correctly by the application's environment-loading path.

Do not rewrite unrelated documentation while fixing encoding; keep the diff focused.

---

## 5. Prove persistence survives a real API restart

### Current limitation

The existing repository/API tests use `mongomock` for fast, offline tests. These are valuable unit tests, but they do not establish that a full API workflow survives an actual FastAPI process restart against a real MongoDB server.

### Required acceptance test

Use a disposable test database, never production data.

1. Start MongoDB (local or a dedicated test Atlas database).
2. Configure `MONGODB_URI` and `MONGODB_DATABASE` for the test environment.
3. Start FastAPI.
4. Submit a workflow request to `POST /api/workflow`.
5. Record the returned `ticket_id`.
6. Verify `GET /api/tickets/{ticket_id}` returns the ticket and saved workflow result.
7. Verify `GET /api/tickets/{ticket_id}/audit` returns the audit events in sequence order.
8. If the workflow created a draft PO, verify it is stored once and has status `draft`.
9. Stop and restart FastAPI without dropping the test database.
10. Call both GET endpoints again with the same ticket ID.
11. Confirm the ticket, workflow result, draft PO, and audit history remain available.
12. Clean up the disposable test database.

### Test implementation guidance

Add an opt-in integration test, for example `tests/test_persistence_restart.py`, or document the manual acceptance test if process orchestration would make CI too complex. It should:
- Skip with a clear reason if the real test MongoDB is unavailable.
- Use a unique database name per run.
- Never drop or mutate the default `workplaceops` database.
- Clean up its own database in a `finally`/fixture teardown.
- Avoid hardcoding credentials or an Atlas URI in source code.
- Be clearly marked as a real-MongoDB integration test.

A repository-only integration test is useful, but the acceptance test should exercise the API workflow and retrieval endpoints too.

---

## 6. Test and document partial-write failures

### Current behavior

The ticket result, draft PO, and audit events are written to separate MongoDB collections. These writes are not atomic as a group. If one succeeds and a later one fails, the database can temporarily contain partial workflow state.

This is an acceptable MVP trade-off only if it is visible, tested, and documented.

### Phase 1 improvement

Keep the current non-transactional design for now, but make the failure state clear:

- [ ] Test failure while creating the initial ticket.
- [ ] Test failure while saving the workflow result.
- [ ] Test failure while saving the draft PO.
- [ ] Test failure while saving audit events.
- [ ] Confirm the API returns a safe error (normally HTTP 503 for required persistence failures).
- [ ] Confirm the ticket is marked `persistence_error` where possible.
- [ ] Confirm logs do not expose the MongoDB URI, username, password, or other credentials.
- [ ] Confirm retrying does not duplicate a ticket, PO, or audit sequence.

### Follow-up beyond minimum Phase 1

A future reliability phase can evaluate:
- MongoDB transactions, if the deployment topology supports them; or
- a durable workflow state/recovery strategy that resumes or repairs incomplete persistence.

Do not claim the multi-collection operation is atomic unless a transaction or equivalent guarantee is actually implemented and tested.

---

## 7. Minimize data in recent-ticket responses

### Concern

The recent-ticket repository returns a truncated piece of `raw_text`. Even though it is limited to 120 characters, request text can contain employee, vendor, or purchasing details in a future real-data deployment.

### Recommended change

- [ ] Review whether the UI genuinely needs request text in the recent-ticket dropdown.
- [ ] Prefer returning `ticket_id`, timestamps, status, and final status only.
- [ ] If a preview is necessary, keep it short and explicitly treat it as potentially sensitive user content.
- [ ] Avoid logging full request bodies in normal application logs.
- [ ] Keep the full request text available only through the ticket endpoint for authorized callers.

This is a hardening improvement, not a blocker for the synthetic-data demo.

---

## 8. Configuration and operational checks

- [ ] Keep `WORKPLACEOPS_PERSISTENCE=required` as the default for a persistence demo so failed writes cannot silently appear successful.
- [ ] Use `WORKPLACEOPS_PERSISTENCE=disabled` only for intentional non-persistent development/testing, and ensure the UI clearly labels results as not persisted.
- [ ] Verify `/api/health` reports database status without revealing the connection URI.
- [ ] Verify MongoDB startup/index initialization failures are visible in logs and health status.
- [ ] Ensure the database user has only the permissions required for the application database.
- [ ] For Atlas, use a restricted network access configuration and URL-encode special characters in the connection-string username/password.
- [ ] Do not commit `.env`, database dumps, or credentials.

---

## 9. Suggested execution order

### Step A — Quick correctness/security fixes

- [ ] Fix the stored `persisted` flag.
- [ ] Fix `.gitignore` BOM and verify `.env` is not tracked.
- [ ] Fix `.env.example` and README encoding.
- [ ] Add regression tests for these behaviors.

### Step B — Reliability verification

- [ ] Run the full existing test suite.
- [ ] Add/execute real MongoDB API restart acceptance test.
- [ ] Test failures at each persistence-write stage.
- [ ] Confirm duplicate handling and audit ordering remain correct.

### Step C — Hardening and closeout

- [ ] Review recent-ticket response fields.
- [ ] Review health and error responses for secret leakage.
- [ ] Update README with verified setup/test instructions.
- [ ] Review `git diff` for unrelated changes and encoding-only damage.
- [ ] Mark Phase 1 complete only when the acceptance checks pass.

---

## 10. Commands to run before completion

From PowerShell at the repository root:

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
git check-ignore -v .env
git ls-files .env
git diff --check
git status --short
```

Then perform the real-MongoDB restart acceptance test from Section 5.

### Expected completion criteria

Phase 1 can be marked complete when:

- [ ] API response and persisted workflow snapshot agree on persistence status.
- [ ] `.env` is ignored and no real credentials are tracked.
- [ ] Configuration/documentation files have clean UTF-8 encoding.
- [ ] Existing tests pass.
- [ ] A real MongoDB-backed workflow survives an API restart.
- [ ] Ticket, draft PO, and audit history can be reloaded.
- [ ] Failure paths do not falsely report a successful save.
- [ ] No unintended approvals, PO issuance, or payments have been introduced.

---

## 11. Reference files in the repository

Review these files while implementing the fixes:

- `app/services/ticket_service.py`
- `app/db/mongo.py`
- `app/db/indexes.py`
- `app/repositories/ticket_repository.py`
- `app/repositories/purchase_order_repository.py`
- `app/repositories/audit_repository.py`
- `app/main.py`
- `app/schemas.py`
- `ui/streamlit_app.py`
- `tests/test_api_persistence.py`
- `tests/test_repositories.py`
- `tests/test_mongo_integration.py`
- `.gitignore`
- `.env.example`
- `README.md`

**Final note:** This checklist describes recommended fixes based on source review. It does not claim that the repository has been changed or that tests have passed.
