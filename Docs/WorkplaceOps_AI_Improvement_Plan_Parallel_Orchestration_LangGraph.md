# WorkplaceOps AI — Improvement Plan
## Parallel Agent Orchestration, LangGraph Evaluation, and Safe Iterative Delivery

**Repository:** `Sinha28Shivam/Singularity_Squad`  
**Development target:** Local development first  
**Approach:** Small, reviewable stages with tests at every stage  
**Safety boundary:** Vendor outreach, purchasing, PO dispatch, and payments remain simulated until explicitly authorized.

---

## 1. Executive summary

WorkplaceOps AI coordinates a workplace procurement workflow:

**Request intake → Policy evaluation → Vendor matching → Draft purchase order → Invoice reconciliation → Critic review → Human approval.**

The repository already contains specialist modules, Pydantic contracts, FastAPI routes, MongoDB persistence, role-based access, approval controls, audit records, policy RAG, a versioned policy engine, and a payment simulator.

The improvement strategy is to preserve this architecture wherever practical. First optimize the current orchestrator by running independent, read-only analyses concurrently. Then test and benchmark the change against the sequential path. Only after that should we build an isolated LangGraph proof of concept and decide whether explicit graph state, conditional routing, and pause/resume justify the extra dependency and migration cost.

The LLM may summarize or recommend. It must not replace deterministic policy checks, authorization, approval, idempotency, or payment eligibility controls.

## 2. Goals and non-goals

### Goals

- Reduce avoidable workflow latency by executing independent analyses concurrently.
- Preserve deterministic business outcomes and stable audit ordering.
- Make concurrency optional for debugging and comparison.
- Add regression tests for concurrency, fallback behavior, and existing workflow cases.
- Evaluate LangGraph against measurable criteria rather than adopting it by default.
- Keep local development and test execution straightforward.
- Preserve existing API contracts, persistence, RBAC, approvals, and synthetic-data boundaries.

### Non-goals

- No real vendor outreach, purchase placement, PO dispatch, or money transfer.
- No automatic approval by an agent or graph node.
- No rewrite of the entire application.
- No immediate migration of all agents to LangGraph.
- No replacement of backend-enforced authorization with prompts or graph routing.
- No claim that tests pass until they have actually been executed.

## 3. Existing architecture and baseline

The main workflow is implemented in `app/orchestrator.py`. Specialist logic lives in `app/agents/`:

- `intake.py` — extracts structured request data and identifies missing mandatory fields.
- `policy.py` — evaluates policy checks and retrieves relevant policy clauses.
- `vendor_matcher.py` — filters and ranks eligible vendors.
- `procurement.py` — performs deterministic costing and builds a draft PO.
- `billing.py` — reconciles invoice, PO, and delivery evidence.
- `critic.py` — reviews the assembled result and reports unresolved issues.

Shared data contracts are in `app/schemas.py`. The LLM wrapper is in `app/llm.py`. API routes, repositories, MongoDB persistence, authentication, approvals, and payment simulation remain separate from workflow orchestration.

The existing orchestrator performs policy evaluation and vendor matching sequentially even though both consume the validated request and policy rules and can run independently. PO construction depends on the vendor result and policy outcome, so it must wait for both.

## 4. Stage 1 — Parallelize independent analyses

### Proposed execution flow

```text
Request intake
     |
Validate mandatory fields
     |
     +----------------------+----------------------+
     |                                             |
     v                                             v
Policy evaluation                            Vendor matching
     |                                             |
     +------------------- Join --------------------+
                         |
                         v
              Enforce policy gate
                         |
                         v
                Draft PO (draft only)
                         |
                         v
          Optional invoice reconciliation
                         |
                         v
                  Read-only Critic
                         |
                         v
             Existing human approval flow
```

### Implementation

Use a bounded `ThreadPoolExecutor(max_workers=2)` to run `policy.evaluate(req, today)` and `vendor_matcher.match_vendors(req, rules)` concurrently. Keep intake and clarification before the concurrent branches. Wait for both results before procurement begins.

Use `WORKPLACEOPS_PARALLEL_ANALYSIS=0` to force sequential execution. Parallel execution is enabled by default in the proposed implementation, but the fallback must remain available for debugging and parity testing.

### Reliability requirements

- Both branch results must be available before the workflow advances to procurement.
- A branch exception must not be converted into a successful or approval-ready result.
- Do not mutate shared request or rules objects inside the branches.
- Append audit entries after joining, in a fixed order, so thread scheduling does not change audit serialization.
- Keep procurement, invoice reconciliation, Critic review, approval decisions, and payment simulation downstream and sequential.
- Do not claim a performance improvement until measured against the sequential baseline.

### Files changed in the current implementation branch

- `app/orchestrator.py` — concurrent policy and vendor analyses plus sequential fallback.
- `tests/test_workflow.py` — concurrency and fallback tests.
- `README.md` — documents `WORKPLACEOPS_PARALLEL_ANALYSIS`.
- `Docs/PARALLEL_ORCHESTRATION_LANGGRAPH_EVALUATION.md` — records the evaluation approach.

The changes were placed on branch `feat/parallel-analysis-evaluation` and a draft pull request was opened:  
https://github.com/Sinha28Shivam/Singularity_Squad/pull/1

**Validation status:** tests were added but were not executed in the development environment when this plan was written. Treat the implementation as pending verification until local tests and CI complete successfully.

## 5. Stage 2 — Test and benchmark the parallel implementation

### Required commands

Run from the repository root:

```powershell
python -m pytest tests/test_workflow.py -q
python -m pytest -q
git diff --check
```

If using a remote feature branch:

```powershell
git fetch origin
git switch --track origin/feat/parallel-analysis-evaluation
```

If the local branch already exists:

```powershell
git switch feat/parallel-analysis-evaluation
```

### Test matrix

| Scenario | Expected behavior |
|---|---|
| Happy-path request | Same vendor, cost, PO draft, and final status as baseline |
| Missing mandatory fields | Stop for clarification before parallel branches |
| Policy hard failure | Draft PO withheld |
| No eligible vendor | No PO generated |
| Over-budget request | Existing budget controls remain effective |
| Clean invoice | Reconciliation remains a match |
| Invoice mismatch | Workflow remains blocked |
| Duplicate invoice | Existing duplicate detection remains effective |
| Parallel branch raises | No approval-ready success result |
| Parallel mode disabled | Sequential path returns the expected result |
| Repeated runs | Business outputs and audit action order remain stable |

The concurrency test should prove actual overlap, not just that both functions were called. A barrier-based test is suitable, provided it has a short timeout and fails clearly if the functions are invoked sequentially.

### Benchmark method

Use a fixed set of synthetic requests and run each scenario in both modes:

- `WORKPLACEOPS_PARALLEL_ANALYSIS=0`
- `WORKPLACEOPS_PARALLEL_ANALYSIS=1`

Measure total workflow duration over multiple repetitions. Report median and p95 latency, not just the fastest run. Keep inputs, LLM mode, policy configuration, and machine conditions consistent. When the LLM is disabled, policy and vendor logic may be fast enough that thread overhead offsets the benefit; report that result honestly.

Do not benchmark real supplier systems or financial services.

## 6. Stage 3 — Build an isolated LangGraph proof of concept

LangGraph should be evaluated as a workflow-state and control-flow tool, not as a replacement for existing business logic. Do not add LangGraph or LangChain to the main dependency list until the proof of concept shows a concrete benefit.

### Candidate graph

1. `intake` — validate and structure the request.
2. `clarification_gate` — end the run when mandatory fields are missing.
3. `policy_analysis` and `vendor_analysis` — independent branches that can run in parallel.
4. `join_and_gate` — verify both outputs and apply deterministic policy-block rules.
5. `draft_po` — call the existing deterministic procurement module.
6. `invoice_reconciliation` — run only when an invoice is provided and a draft PO exists.
7. `critic` — read-only review.
8. `approval_pending` — return a result requiring a human decision; never approve automatically.

### Prototype requirements

- Use a typed state contract with explicit input and output fields.
- Keep existing Pydantic schemas and specialist functions where possible.
- Keep database writes and durable audit operations behind existing repositories/services.
- Make branch failures explicit and testable.
- Keep conditional transitions readable and deterministic.
- Use synthetic data and local execution first.
- Do not add real external procurement, vendor messaging, bank, or payment integrations.
- Keep the prototype isolated so the existing application remains runnable if the experiment is abandoned.

### Evaluation criteria

| Criterion | What to check |
|---|---|
| Correctness | Same decisions, costs, vendor selection, and statuses as the current orchestrator |
| Parallelism | Independent branches execute concurrently where supported |
| State clarity | Typed state makes dependencies and transitions easier to understand |
| Error handling | Branch failure stops unsafe downstream actions |
| Auditability | Significant transitions are explainable and traceable |
| Resumability | A paused workflow can safely resume without duplicating side effects |
| Human-in-the-loop | Approval pauses for an authorized human and cannot be bypassed |
| Testing | Node and end-to-end tests remain practical |
| Performance | Latency improves or remains acceptable |
| Maintenance | Dependency and conceptual complexity are justified |

### Adoption decision

Adopt LangGraph only if the prototype materially improves explicit state handling, conditional routing, recovery, or human pause/resume. If it only wraps the same functions without reducing complexity, retain the current orchestrator.

## 7. Safety and security invariants

These invariants apply in every stage and every orchestration implementation.

1. **Draft-only procurement:** generating a PO record does not issue an order or contact a supplier.
2. **Simulated payments only:** no bank/gateway SDK, provider call, or real transfer is permitted.
3. **Human approval:** agents and graph nodes cannot approve their own recommendations.
4. **Backend authorization:** RBAC, separation of duties, approval freshness, and eligibility checks remain enforced by backend services.
5. **Deterministic controls:** arithmetic, budget validation, invoice matching, duplicate checks, and policy gates must not depend on LLM prose.
6. **Fail closed:** missing or conflicting evidence, branch failure, unavailable policy state, or persistence failure must not become a successful authorization.
7. **Idempotency:** retries must not create duplicate logical payment instructions or duplicate business side effects.
8. **Audit durability:** important state changes and security-relevant denials should use existing durable audit mechanisms.
9. **Synthetic data:** local tests and demonstrations use synthetic/anonymized requests, vendors, invoices, and delivery evidence.
10. **No implicit authorization:** “simulated” mode is not permission to enable real external effects later.

## 8. Local development setup

Use the project instructions in `README.md` and `Docs/flow.md`. Typical commands:

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000 --reload
python -m streamlit run ui\streamlit_app.py
```

Run tests:

```powershell
python -m pytest tests/test_workflow.py -q
python -m pytest -q
```

Use a local MongoDB instance if required by the test or app configuration. Keep `.env` out of version control and use synthetic credentials for local demos. For deterministic development, use the existing `WORKPLACEOPS_OFFLINE=1` mode.

Do not introduce new dependencies until the relevant stage requires them and their value has been demonstrated.

## 9. Suggested delivery sequence

### Milestone A — Parallel orchestration
- Review the feature branch diff.
- Run targeted and full test suites.
- Fix concurrency, typing, or regression issues.
- Verify the sequential fallback.
- Benchmark sequential versus parallel modes.
- Merge only after review and passing validation.

### Milestone B — LangGraph spike
- Create an isolated proof of concept.
- Reuse existing specialist functions and schemas.
- Implement conditional flow and explicit branch joins.
- Add parity tests for the scenario matrix.
- Measure latency, complexity, and recovery behavior.
- Write a short go/no-go recommendation.

### Milestone C — Hardening
- Test branch failures and partial results.
- Verify stable audit ordering and persistence behavior.
- Test repeated runs, retries, and policy-version changes.
- Ensure no route or workflow can bypass human approval.
- Review payment simulation settings and keep real execution impossible.
- Update the docs with observed results, not assumptions.

### Milestone D — Local demo
- Run the FastAPI backend and Streamlit UI locally.
- Demonstrate a clean request, missing-information case, policy block, alternative-vendor re-plan, invoice mismatch, and approval flow.
- Demonstrate that the PO remains a draft and payment responses explicitly state simulation-only behavior.
- Capture latency measurements and test results for the project report.

## 10. Definition of done

A stage is complete only when:

- Changes are scoped and reviewable.
- Tests for new behavior exist and pass locally or in CI.
- Existing workflow behavior remains compatible, or any intentional change is documented.
- Concurrency is demonstrated rather than assumed.
- Failure paths are tested.
- Documentation matches the implemented code.
- No real vendor outreach, purchase, PO dispatch, or payment is possible.
- Performance claims are backed by reproducible measurements.
- The LangGraph adoption decision is based on evidence.

## 11. Current status and immediate next action

**Current status:** a feature branch and draft PR contain the first parallel-analysis change, its tests, and evaluation documentation. Test execution and benchmarking remain outstanding. LangGraph is an evaluation proposal, not yet a production dependency or completed migration.

**Immediate next action:** check out `feat/parallel-analysis-evaluation`, run `python -m pytest tests/test_workflow.py -q`, then run `python -m pytest -q`. Fix failures before benchmarking or merging.

---

## Reference links

- Repository: https://github.com/Sinha28Shivam/Singularity_Squad
- Draft pull request: https://github.com/Sinha28Shivam/Singularity_Squad/pull/1
- Current orchestration implementation: https://github.com/Sinha28Shivam/Singularity_Squad/blob/master/app/orchestrator.py
- Workflow tests: https://github.com/Sinha28Shivam/Singularity_Squad/blob/master/tests/test_workflow.py
- Existing workflow documentation: https://github.com/Sinha28Shivam/Singularity_Squad/blob/master/Docs/flow.md
