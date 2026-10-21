# Parallel orchestration and LangGraph evaluation

## Current implementation

The workflow is a synchronous Python orchestrator in `app/orchestrator.py`. Intake and clarification happen first. Once the request is complete, policy evaluation and vendor matching are independent read-only analyses and can run concurrently. The orchestrator collects both results and writes audit entries in a fixed order, then continues with draft PO construction, optional invoice reconciliation, Critic review, and the existing human approval workflow.

No LangGraph dependency is required for the first optimization. The parallel branch uses a bounded `ThreadPoolExecutor(max_workers=2)`, which is appropriate here because the work is mostly I/O-bound when an LLM is configured and deterministic local computation otherwise.

## Safety and determinism

- Intake and mandatory-field clarification happen before parallel analysis.
- The two concurrent branches receive the same request and policy-rule inputs; neither branch writes procurement state.
- Both futures must complete successfully before draft PO generation continues. Exceptions are not converted into a successful result.
- Audit entries are appended only after the join, in a fixed order, so output ordering does not depend on thread scheduling.
- Procurement, invoice reconciliation, Critic review, approval decisions, and payment simulation are not parallelized.
- The workflow still creates draft POs only. Purchases, vendor outreach, and payments remain simulated.
- Set `WORKPLACEOPS_PARALLEL_ANALYSIS=0` to use the sequential fallback while debugging or comparing behavior.

## Test plan for this stage

Run locally from the repository root:

```powershell
python -m pytest tests/test_workflow.py -q
python -m pytest -q
```

The new concurrency test uses a two-party thread barrier inside both analysis functions. If the orchestrator accidentally invokes them sequentially, the test fails by timing out instead of silently passing. A second test verifies that the sequential fallback remains available. The tests have been added to the repository but must be run locally/CI before this change is considered verified.

## LangGraph: adopt only if it improves the project

LangGraph is a candidate for the next stage, not a prerequisite for parallelism. The existing workflow already has business logic, schemas, persistence, audit records, role controls, and approval handling. A migration should preserve those boundaries rather than moving business rules or authorization into an LLM graph.

### Proposed proof of concept

Build an isolated adapter/prototype with explicit typed state and nodes for:

1. `intake` — validate request and stop for clarification when required.
2. `policy_analysis` and `vendor_analysis` — fan out concurrently after intake.
3. `join_and_gate` — require both outputs and enforce deterministic policy-block rules.
4. `draft_po` — call the existing deterministic procurement module.
5. `invoice_reconciliation` — optional, after a PO exists.
6. `critic` — read-only review.
7. `approval_pending` — emit a pending approval result; never approve automatically.

Compare the prototype with the current orchestrator using the same synthetic requests. Measure end-to-end latency, behavior parity, trace/audit clarity, retry behavior, test complexity, and dependency overhead. Include happy path, missing fields, policy block, no eligible vendor, over-budget request, invoice mismatch, duplicate invoice, branch exception, and disabled-parallelism cases.

### Decision criteria

Adopt LangGraph only if the prototype demonstrates a meaningful benefit from explicit graph state, conditional routing, resumability, or future human-in-the-loop pause/resume. Keep the current orchestrator if the graph merely wraps the same straight-line code without reducing complexity.

Do not move approval enforcement, separation-of-duties checks, payment eligibility, idempotency, or durable audit writes into graph-only logic. These remain backend-enforced controls. Avoid adding `langgraph` or `langchain` to the main requirements until the proof of concept has been benchmarked and its dependency cost is justified.

## Verified results (2026-10-21)

All numbers below were **measured** in this repository (Windows, Python 3.11.9, 8 CPUs). Reproduce them with `scripts\benchmark_orchestration.py`.

### Tests

- `python -m pytest -q`: **839 passed** (767 before this work).
  - `tests/test_parallel_orchestration.py`: 47 tests. They cover the full §5 matrix in both modes with output parity, barrier-proven overlap, input isolation (deep copies), stable repeated runs, no leaked threads, and branch failure raising `AnalysisBranchError`. They also check the portal path: a failing branch marks the ticket `failed`, creates no approval request, and both modes give the same ticket and approval results.
  - `tests/test_langgraph_poc.py`: 14 tests (parity, fail-closed branches, overlap, pause/resume that never approves, isolation from the main app).
  - `tests/test_benchmark_script.py`: 3 tests.
- pyflakes and `git diff --check` are clean.

### Review follow-up (code review of `feat/parallel-analysis-evaluation`)

| Review point | Status |
|---|---|
| Shared inputs: defensive isolation | Done. Each branch gets a `model_copy(deep=True)` of `req` and `rules`. A test mutates the branch copy and asserts the caller's objects are unchanged. |
| Error context | Done. `AnalysisBranchError(branch, cause)` is raised only after both branches finish, checking policy before vendor. |
| Timing | Done. `result.extra["orchestration"]["analysis_ms"]` records policy, vendor and total. |
| Dedicated failure audit record | Done. The portal writes a durable `analysis_branch_failed` audit event (branch plus exception type, no message). |
| Explicit failure tests (no draft PO, API behaviour) | Done for policy and vendor, in both modes. The API returns 500 with a generic body, the ticket is `failed`, and there is no PO, approval or leaked detail. |
| Parity tests | Done. The §5 matrix compares business outputs and audit order across modes; the timing metadata is excluded. |
| Real latency measurements | Done: offline, simulated and live LLM (see the table below). |
| Tests executed | `tests/test_workflow.py`: 10 passed. Full suite: 839 passed. `git diff --check`: clean. |
| PR mergeability / CI | The branch's commits were already merged into `master` (`5bafefb`, `b24ae52`), so the draft PR is superseded and can be closed. There is no CI workflow in the repo; verification is local. |

### Latency: sequential vs parallel (`app/orchestrator.py`)

| Setup | Sequential median | Parallel median | Speedup | Parity |
|---|---:|---:|---:|---|
| Offline (deterministic), 7 scenarios × 30 reps | 1.11 ms | 1.66 ms | 0.67× (thread overhead ≈ 0.5 ms) | true |
| Simulated 150 ms I/O in **each** branch, 20 reps | 302.4 ms | 152.4 ms | **1.99×** | true |
| **Live LLM** (gpt-4o-mini), 7 scenarios × 5 reps | 4510.6 ms | 4605.9 ms | **0.98×** (no gain) | true |

**Why the live LLM run shows no gain:** with the live LLM, the policy branch takes about 1,900 ms (its LLM summary call) while vendor matching takes about 0.2 ms. Parallel time is bounded by the slower branch, so total time can't drop below about 1,900 ms. The rest of the roughly 4.5 s is the intake and Critic LLM calls, which are sequential by design.

**Conclusion:** keep parallel mode on by default. It is cheap (≈ 0.5 ms), safe (outputs are identical) and future-proof. It halves latency once both branches do I/O, for example when vendor matching gains retrieval or LLM ranking. With today's code paths, don't claim a real-world speed-up.

### LangGraph decision: **NO-GO** for now

Evidence is in `experiments/langgraph_poc/README.md`.

- **Latency:** the graph adds about 18 ms per run (graph build plus checkpointing). Offline the median is 19 ms versus about 1.6 ms. With 150 ms branches it is 169 ms versus 152 ms.
- **Cost:** it adds 18 transitive packages and about 2× the code.
- **Benefit:** it does not reduce complexity for this linear pipeline. Durable, access-controlled human approval is already handled by the MongoDB-backed approvals service, and an in-memory `interrupt()` does not replace that.

The PoC stays isolated (`experiments/`, optional `requirements-langgraph.txt`) as a reference.

**Revisit LangGraph if** the workflow gains real branching or loops (for example multi-round vendor negotiation), long-running resumable steps with a durable checkpointer, or several independent I/O-bound agents.
