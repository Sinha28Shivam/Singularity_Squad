# AI Guardrails Runbook

Operational runbooks for the AI/LLM and policy-RAG components (AI plan §22 D3, §23 D4). Companion to
[`AI_Guardrails_Tokenization_RAG_Enhancement_Plan.md`](AI_Guardrails_Tokenization_RAG_Enhancement_Plan.md).

## 0. What exists today vs operator responsibility

| Capability | Status |
|---|---|
| Redacted AI telemetry events (`app/ai_security/telemetry.py`) | **Implemented** — bounded in-process ring buffer (2000 events), **ephemeral** (lost on restart, per process/worker). |
| Structured log line per event: logger `workplaceops.ai_telemetry`, message prefix `ai_event {json}` | **Implemented** — the only durable copy, *if* your log pipeline ships it. |
| Admin endpoints `GET /api/admin/ai/telemetry/summary`, `GET /api/admin/ai/telemetry/events?limit=1..200`, `GET /api/admin/ai/tasks` | **Implemented** (admin only; approvers/users get 403). |
| Retrieval mode + degradation reason on every search result / policy evaluation | **Implemented** (`retrieval_mode`, `degradation_reason`, `retrieval_config_version`). |
| Offline RAG benchmark + measured baseline (`scripts/run_rag_benchmark.py`) | **Implemented** (dataset is DRAFT, domain-owner review pending). |
| Release gate (`scripts/ai_release_gate.py`, `.github/workflows/ai-release-gate.yml`) | **Implemented**. |
| Alert evaluation, paging, dashboards, durable metric storage, scheduled benchmark runs in production | **NOT implemented — operator responsibility.** Build alerts on the `ai_event` log stream in your log/metrics platform. |
| On-call rotation, owner assignment | **Operator responsibility.** Role names below are suggestions. |

Telemetry never contains prompts, document text, excerpts or secrets (keys such as `prompt`, `text`, `content`,
`excerpt` are replaced with `[REDACTED]`; API keys, bearer tokens, connection URIs and JWTs are pattern-redacted).
Alerts built on it must keep that property — never attach document excerpts or raw prompts to an alert.

Event kinds read by `summary()`: `llm_call` (task, outcome, error category, tokens, latency), `retrieval`
(`retrieval_mode`) and `injection_signal`. Whether a given code path emits a given kind depends on the current
integration; check `GET /api/admin/ai/telemetry/events` before relying on a signal for alerting.

## 1. Alert catalogue

Thresholds marked *baseline* must be set from observed production traffic (do not invent targets; plan §23).

| ID | Alert | Severity | Owner role | Trigger (sustained, aggregated) | Runbook |
|---|---|---|---|---|---|
| A1 | LLM provider errors/timeouts | P2 | Platform on-call | `llm_call` outcome ∉ {ok} rate above *baseline* for 10 min, or `error_category` = `rate_limit`/`timeout` spike | §2 |
| A2 | Retrieval degraded | P2 | Search/RAG owner | share of `keyword_only_degraded` (or `hybrid_local_hash` when OpenAI embeddings are expected) above *baseline* for 15 min | §3 |
| A3 | Index inconsistency / mixed embedding models | P2 | Search/RAG owner | warning "Some chunks have no comparable embedding", documents stuck `indexing`/`failed`, re-index failures | §4 |
| A4 | Token/cost spike | P3 | FinOps + platform | estimated tokens or cost per hour > *baseline* × N, or `evidence_reduced` rate jump | §5 |
| A5 | Prompt-injection signal spike | P3 (P2 if correlated with a single uploader) | Security owner | `injection_signal` count over 1 h above *baseline*; **aggregate, never page per phrase** | §6 |
| A6 | Citation/evidence validation failures | P1 | Security owner + RAG owner | any rejected/fabricated citation in production, or benchmark `citation_validity_rate` < 1.0 | §7 |
| A7 | Release gate failure | Blocker | Change author | `scripts/ai_release_gate.py` exits non-zero | §8 |

## 2. Provider outage (A1)

Safe fallback (implemented): every AI task has a contract fallback (`GET /api/admin/ai/tasks` → `fallback`).
Deterministic parsing, rule evaluation and approvals continue; AI summaries are omitted or templated. An LLM
failure is never reported as a successful AI result and can never approve anything.

1. Confirm via `GET /api/admin/ai/telemetry/summary` → `llm_tasks.*.outcomes` / `error_categories`.
2. Check provider status page and the API key/quota (do not paste keys into tickets or chat).
3. If prolonged, set `WORKPLACEOPS_OFFLINE=1` (or remove the key) and restart to stop retries; the app runs on
   deterministic fallbacks and local hashed embeddings.
4. Note: new documents indexed while offline get `local-hash-512` vectors — see §4 for re-indexing afterwards.
5. Close: error rate back to baseline for 30 min; record the incident window.

## 3. Degraded retrieval (A2)

Modes: `hybrid` (keyword + semantic embeddings), `hybrid_local_hash` (keyword + local hashed vectors, **not
semantic**), `keyword_only_degraded` (no comparable vectors). Anything but `hybrid` is degraded and is surfaced
as a warning plus `POLICY_RETRIEVAL_DEGRADED`; absence of evidence is never treated as compliance.

1. Look at `degradation_reason` on recent search results: `embedding_provider_unavailable` → §2;
   `no_stored_embeddings` / `local_hash_embeddings_not_semantic` → §4.
2. Run the benchmark locally to quantify impact: `& .\venv\Scripts\python.exe scripts\run_rag_benchmark.py`.
3. Do not lower `min_relevance` or silence warnings to "fix" degraded mode.

## 4. Bad or mixed index — re-index procedure (A3)

Vectors are only comparable within one embedding model; retrieval uses the majority model and ranks other
chunks by keywords only (warning emitted). An embedding-model change is an index migration (plan §17).

1. Inventory: list documents (`GET /api/policy-documents`) and their `index_status` / `chunk_count`; in Mongo,
   group `policy_chunks` by `embedding_model`.
2. Fix the cause first (provider available, correct `embedding_model` in `data/policy_rag_config.json`).
3. Re-index each affected document: `POST /api/policy-documents/{id}/reprocess` (admin). Reprocessing keeps
   versions, supersession state and verified rate-card decisions.
4. Verify: chunks share a single `embedding_model`; searches report the expected `retrieval_mode`; run the
   benchmark against the baseline (`--baseline data\eval\baseline_rag_bench_v1.json`).
5. If a document cannot be fixed, archive it rather than leaving a stale/mixed index in place.

## 5. Cost spike (A4)

1. `GET /api/admin/ai/telemetry/summary` → per-task `estimated_input_tokens`, `actual_input_tokens`,
   `actual_output_tokens`, `calls`. Identify the task and whether volume or size per call grew.
2. Check for loops/retries in workflow runs and for unusually large evidence sets (`evidence_reduced`).
3. Mitigate: disable the optional advisory task (contracts in `app/ai_security/contracts.py`) or go offline (§2);
   deterministic decisions are unaffected.
4. Cost estimates need configured rates in `data/ai_guardrails_config.json` → `cost_rates` (currently
   `rates_version: "unset"`, i.e. **no cost figures are produced** until an operator configures them).

## 6. Prompt-injection signal spike (A5)

Detection signals are not the defense: retrieved text is always data, AI output is advisory and validated, and
deterministic rules/approvals are authoritative. A spike is an investigation trigger, not an outage.

1. From `GET /api/admin/ai/telemetry/events`, group `injection_signal` events by document id / uploader
   (event metadata only — do not export document text into the alert).
2. Review the flagged documents in the admin UI; archive malicious uploads and review the uploader's access.
3. Check that no affected ticket was auto-approved (it cannot be by design — verify the audit trail anyway).
4. If a new obfuscation bypassed detection, add a regression test under `tests/ai_security/` and extend
   `app/ai_security/injection_signals.py`.

## 7. Citation validation failures (A6)

A fabricated or non-resolving citation must never be accepted; this is a zero-tolerance release blocker.

1. Identify the evaluation/request id and the cited `chunk_id` / `document_id` / `content_hash`.
2. Check whether the document was reprocessed or superseded mid-request (hash mismatch) vs a genuine defect.
3. Treat the affected decision as `review_required`; do not rely on the AI explanation.
4. Add a failing regression test, fix, and run the release gate before redeploying.

## 8. Release gate (A7)

```powershell
& .\venv\Scripts\python.exe scripts\ai_release_gate.py          # writes data/eval/reports/release_report.json
```

Gates: required security/invariant test suites (a missing suite fails unless `--allow-missing` is explicitly
approved), RAG benchmark vs `data/eval/baseline_rag_bench_v1.json` (zero tolerance for scope violations,
forbidden evidence, authorization failures and citation validity < 1.0; critical cases may not regress), and a
secret scan of tracked files (reports file:line only, never the value). CI: `.github/workflows/ai-release-gate.yml`.

Re-baselining (only when a retrieval change is an intended improvement, or the dataset version changes): run
`scripts\run_rag_benchmark.py --out data\eval\baseline_rag_bench_v1.json`, and commit the new baseline with a
reviewed explanation of every metric change.
