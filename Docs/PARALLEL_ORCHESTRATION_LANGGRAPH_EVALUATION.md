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
