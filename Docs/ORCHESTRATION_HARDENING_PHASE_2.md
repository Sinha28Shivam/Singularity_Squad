# Orchestration Hardening — Phase 2 validation notes

Plan: [WorkplaceOps_AI_Phase_2_Implementation_Plan.md](WorkplaceOps_AI_Phase_2_Implementation_Plan.md).

- **Branch:** `feat/orchestration-hardening-phase-2`.
- **Base:** `b24ae52` (`master`, which already contains the Phase 1 commits `5bafefb` plus the hardened `analyze()` and the LangGraph spike).
- **Date:** 2026-10-21.
- **Environment:** Windows 10 (build 26300), Python 3.11.9, 8 CPUs, offline (`WORKPLACEOPS_OFFLINE=1`) unless stated.

## Scope

| Workstream | Delivered |
|---|---|
| A. Branch and baseline | Branch created from `b24ae52`. Baseline: `python -m pytest -q` on a clean worktree of `b24ae52` gave **839 passed** (330 s). |
| B. Input isolation | Each branch gets its own `model_copy(deep=True)` of `req` and `rules` (both are Pydantic models, so they are safe to deep-copy). `policy.evaluate(req, today)` reloads rules internally, so only the vendor branch receives a rules copy; no unused copy is made. `today` is an immutable `date`. |
| C. Failure handling | Both futures always finish. Errors are raised after the join as `AnalysisBranchError(branch, cause)` (`raise … from cause`), checking policy before vendor. There is one redacted WARNING log line: branch, exception type, mode and failed branches, with no message and no request text. There is a durable `analysis_branch_failed` audit event on both `/api/admin/tickets/{id}/run` and the legacy `/api/workflow`. Responses are a 500 with the established `{"detail": …}` shape and a generic message; there is no global handler. |
| D. Tests | 79 tests in `tests/test_parallel_orchestration.py`, 19 in `tests/test_workflow_api_failures.py`, and 6 in `tests/test_benchmark_script.py` (see the checklist below). |
| E. Timing and benchmark | `result.extra["orchestration"] = {engine, mode, analysis_ms{policy,vendor,total}, workflow_ms}`. It is internal metadata on the existing `extra` field, not a new API schema, and is not in the approval snapshot hash. A DEBUG timing log is written. `scripts/benchmark_orchestration.py` runs a warm-up, then N reps, and reports median, p95, min, max and mean, errors counted separately, the per-stage breakdown, environment and config, and a parity check. |
| F. Documentation | This file, `Docs/flow.md` §15, the README, and `Docs/PARALLEL_ORCHESTRATION_LANGGRAPH_EVALUATION.md`. |
| G. Readiness | Checks below. |

## Plan checklist evidence (§5 D)

| Requirement | Test(s) |
|---|---|
| Policy, vendor, or both workers raise | `test_branch_failure_raises_after_both_finish[policy/vendor]`, `test_both_branches_fail_policy_reported_first` |
| No PO, reconciliation, Critic or payment after failure | `test_branch_failure_runs_no_downstream_work[policy/vendor/both]` (spies on `build_draft_po`, `reconcile` and `critic.review`, with `invoice_id`); `test_workflow_api_failures.py` asserts 0 POs, 0 approval requests and 0 `payment_simulations`, and that payment eligibility is `false` |
| Safe API error with no leaks | `test_workflow_api_failures.py`: the body is the exact `{"detail": …}` and never contains the injected `sk-secret…` text, `Traceback`, `RuntimeError` or request text |
| Failure observable, no sensitive data | `test_failure_log_is_redacted`, `test_both_failures_logged_once`, and the audit-event assertions in the portal and API tests |
| Branches overlap | `test_branches_overlap_in_parallel_mode` (`threading.Barrier`, 3 s timeout) |
| Sequential mode runs no thread pool | `test_sequential_mode_creates_no_executor` (the executor is replaced by a stub that raises), `test_disabled_mode_runs_sequentially_in_caller_thread` |
| `0/false/no/off` case-insensitive, and the default | `test_parallel_analysis_enabled_env` (including `FALSE`, `Off`, ` False `), `test_default_env_unset_selects_parallel` |
| Input isolation | `test_branch_mutation_is_isolated`, `test_analyze_does_not_mutate_caller_inputs` |
| Business parity matrix | happy path, missing fields, policy block (`too_soon`), over budget, no eligible vendor, clean, mismatched and duplicate invoice, all in both modes plus `run_both` parity, and `test_portal_parity` (ticket status and approval reason codes) |
| Audit order independent of completion order | `test_audit_order_independent_of_completion_order[policy/vendor]` (one branch is forced to finish first using `Event`) |
| Repeated runs stable / no leaked threads | `test_repeated_runs_stable`, `test_no_leaked_threads` |

## Validation results (actually executed)

| Check | Result |
|---|---|
| `python -m pytest tests/test_workflow.py -q` (default, parallel) | **10 passed** |
| `$env:WORKPLACEOPS_PARALLEL_ANALYSIS="0"; python -m pytest tests/test_workflow.py -q` | **10 passed** |
| `python -m pytest -q` (full suite) | **886 passed** in 333 s (baseline 839) |
| `python -m pyflakes app ui tests scripts experiments` | clean |
| `git diff --check` | clean (only CRLF→LF notices) |
| Type checker / CI | **Not run**: the repository has no type-checker configuration and no CI workflow (`.github/workflows` absent). |
| PR mergeability | **Not checked through GitHub**: the API was not accessible from this environment. Locally the branch is `b24ae52` plus the working-tree changes, with no conflicts. The old draft PR for `feat/parallel-analysis-evaluation` is superseded because its commits are already on `master`. |

## Benchmark (`scripts/benchmark_orchestration.py`, 7 scenarios, warm-up + N reps, 0 errors, parity true)

| Setup | Mode | Workflow median | p95 | Analysis-stage median (policy / vendor / total) |
|---|---|---:|---:|---|
| Offline, 30 reps | sequential | 1.58 ms | 2.70 ms | 0.76 / 0.24 / 1.17 ms |
| Offline, 30 reps | parallel | 2.15 ms | 3.43 ms | 0.69 / 0.24 / 1.57 ms |
| Simulated 150 ms I/O per branch, 20 reps | sequential | 302.59 ms | 303.90 ms | 151.18 / 150.71 / 302.03 ms |
| Simulated 150 ms I/O per branch, 20 reps | parallel | 152.53 ms | 154.66 ms | 151.17 / 151.06 / 151.97 ms |
| Live LLM gpt-4o-mini, 5 reps (earlier run, same code paths) | sequential / parallel | 4510.6 / 4605.9 ms | — | policy ≈ 1.9 s, vendor ≈ 0.2 ms |

**What the numbers show:**
- **Offline:** parallel mode is about 0.5 ms slower because thread start-up costs more than branches that finish in under 1 ms.
- **When both branches are I/O-bound:** parallel mode is **1.98×** faster.
- **With the live LLM:** there is **no gain today**. Only the policy branch calls the LLM, and the vendor branch takes microseconds.

Parallel mode stays the default because it is cheap and outputs are identical. It is a rollback switch, not a claimed speed-up. Timings are not asserted in CI.

## Rollback

1. Set `WORKPLACEOPS_PARALLEL_ANALYSIS=0`. Sequential mode is covered by the same tests (see the validation results above).
2. If needed, revert only the Phase 2 commit(s); business logic is unchanged.
3. Never disable policy checks, human approval, audit, RBAC or the payment simulation guards as a workaround.

## Limitations and notes

- **No cancellation:** a running Python thread can't be cancelled, so when one branch fails the other is allowed to finish before the error is raised. Nothing downstream runs.
- **Variable results:** measurements depend on the host and, in live mode, on provider latency and rate limits.
- **Unchanged contracts:** no API, Pydantic contract, database schema, policy-rule or vendor-ranking change. No new dependency was added to `requirements.txt`.
- **Simulated side effects:** vendor outreach, PO dispatch and payments remain simulated. POs stay drafts, and payments are `SIMULATION` with `money_transferred: false`.
- **LangGraph deferred:** the isolated spike (`experiments/langgraph_poc/`) measured as a no-go. No LangGraph change was made in Phase 2.

## PR summary (for the reviewer)

- **Scope:** hardening of the parallel policy/vendor analysis (logging, failure audit, safe API errors), plus tests and benchmark evidence.
- **Risks:** low. The change is additive, keeps the same signatures and outputs, and can be rolled back with an environment variable.
- **Validation:** 886 tests passed, the sequential and parallel workflow tests passed, pyflakes is clean, and the benchmark JSON is attached.
- **Deferred:** a CI workflow, type checking, and a LangGraph adoption decision if new branching or resumability requirements appear.
- **Approval:** reviewer approval is still required before merge, and the PR stays a draft until then.
