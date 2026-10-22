# WorkplaceOps AI — AI Guardrails, Tokenization, RAG Quality & Evaluation Enhancement Plan

**Repository:** `Sinha28Shivam/Singularity_Squad`  
**Product:** WorkplaceOps AI — Request-to-Payment Copilot  
**Document type:** Implementation requirements and engineering plan  
**Status:** Proposed implementation specification; not a claim that the work has been completed  
**Primary objective:** Strengthen the existing AI and policy-RAG system without replacing its deterministic policy engine or weakening human approval controls.

---

## 1. Executive summary

WorkplaceOps AI already contains a substantial policy-RAG and dynamic-policy foundation. The next phase should improve its reliability, security, observability, and measurable quality rather than rebuild it or add agents without a demonstrated need.

This plan covers four workstreams:

1. **AI security guardrails** — prompt-injection resistance, evidence integrity, structured output validation, access-control checks, sensitive-data handling, and adversarial testing.
2. **Model-aware tokenization and context budgeting** — accurate token counts, safe input limits, deterministic chunking, context budgeting, and usage/cost telemetry.
3. **RAG quality improvements** — retrieval evaluation, ranking/reranking experiments, document extraction quality, citation validation, version consistency, and explicit degraded-mode behavior.
4. **Evaluation and monitoring** — a versioned benchmark dataset, automated regression tests, quality metrics, operational alerts, and release gates.

### 1.1 Binding principles

- **Backend code remains authoritative.** AI must not make final approval, eligibility, financial, or workflow-state decisions.
- **RAG supplies evidence, not authority.** Retrieved document text is untrusted data, never instructions to the model.
- **No evidence means no unsupported pass.** Missing, conflicting, stale, or low-quality evidence must remain unknown/review-required as appropriate.
- **Candidate policy rules are not active rules.** Extracted rules require validation and authorized human approval before activation.
- **Citations must be verifiable.** Every citation must map to an actual stored document version, content hash, chunk, and page/section when available.
- **Authorization is enforced server-side.** UI filtering is not a security boundary.
- **Fail safely and report degradation.** Provider failure must not silently appear to be successful semantic retrieval.
- **Measure before adding complexity.** Reranking or a new model is adopted only if benchmark results justify its latency and cost.
- **Keep external execution disabled.** No vendor outreach, PO dispatch, award, or real payment is enabled by these enhancements.

---

## 2. Existing baseline and known limitations

The repository currently has relevant modules in `app/policy_rag/`, `app/policy_rules/`, `app/llm.py`, and related repositories and tests.

| Area | Existing baseline | Enhancement required |
|---|---|---|
| Policy retrieval | Keyword/BM25 plus vector similarity and rank fusion | Measure recall/precision; benchmark query types; improve ranking only when evidence supports it |
| Embeddings | OpenAI-compatible embeddings; local hashed-vector fallback; keyword-only degraded mode | Validate model/version compatibility, report fallback clearly, monitor quality and cost |
| Chunking | Page-aware and section-aware chunks; overlap; word-based token estimate | Use model-aware token counts and retain page/section traceability |
| Evidence | Stored chunk/document metadata, content hashes, page/section references | Centralized citation validation, immutable source/version checks, stronger evidence access tests |
| Rule extraction | Typed candidate rules, suspicious-instruction pattern detection, evidence checks, human review lifecycle | Expand adversarial tests; treat pattern detection as one layer, not a complete defense |
| Policy decisions | Deterministic evaluation and approval integration; LLM summaries are advisory | Test invariants end-to-end so LLM outputs cannot alter outcomes |
| Monitoring | Retrieval mode/configuration metadata and logs exist in parts of the implementation | Add structured telemetry, dashboards, alerts, retention and redaction rules |
| Evaluation | Unit tests exist for retrieval and rule extraction | Build a versioned gold dataset and release-blocking quality/security regression suite |

**Important:** The presence of these modules does not by itself prove production readiness. Current branch tests, deployment configuration, authorization paths, provider settings, and operational alerting must be verified during implementation.

### 2.1 Relevant existing code to inspect before changing anything

- `app/llm.py`
- `app/policy_rag/chunking.py`
- `app/policy_rag/embeddings.py`
- `app/policy_rag/retrieval.py`
- `app/policy_rag/evaluation.py`
- `app/policy_rag/schemas.py`
- `app/policy_rules/extraction.py`
- Policy document/chunk/rule repositories and API routers
- Existing tests for policy retrieval, evaluation, rule extraction, authorization, and workflow approvals
- `data/policy_rag_config.json`

Before implementation, inspect the current default branch and recent commits. Reuse current conventions and extend existing services instead of creating duplicate policy, retrieval, or approval layers.

---

## 3. Goals and non-goals

### 3.1 Goals

- Prevent retrieved document content from controlling model instructions or application behavior.
- Reject invalid, unsupported, or fabricated AI outputs.
- Enforce actual model token limits before sending requests.
- Improve citation correctness and retrieval quality using measurable tests.
- Make fallback/degraded behavior explicit to API clients and operators.
- Track model, prompt, tokenizer, embedding, chunking, and retrieval configuration versions.
- Detect regressions before deployment.
- Provide useful telemetry without logging secrets or full sensitive document contents.
- Preserve deterministic evaluation and approval behavior.

### 3.2 Non-goals

- Replacing the existing RAG system.
- Replacing deterministic rules with an LLM judge.
- Giving an LLM permission to approve requests, activate rules, change policy versions, or execute purchases/payments.
- Automatically approving extracted policy rules.
- Introducing LangGraph or additional agents unless a later, separately justified workflow requires stateful branching, retries, and human-in-the-loop execution.
- Treating model confidence scores as calibrated probabilities without empirical calibration.
- Guaranteeing that prompt injection can be eliminated solely through prompting or regex detection.

---

## 4. Target architecture

```text
User / Workflow Request
        |
        v
FastAPI Authentication + Authorization + Input Validation
        |
        v
Deterministic Request/Policy Context Builder
        |
        +------------------------------+
        |                              |
        v                              v
Policy Document Access Filters     Token/Size/Cost Guard
        |                              |
        +---------------+--------------+
                        |
                        v
               RAG Retrieval Layer
       (keyword + compatible vector search)
                        |
                        v
             Citation/Evidence Validator
                        |
                        v
       Untrusted Evidence Pack + Clear Boundaries
                        |
                        v
        LLM Task (extract / summarize / explain)
                        |
                        v
      Schema + Evidence + Safety Validation
                        |
             +----------+----------+
             |                     |
             v                     v
      Valid advisory output   Invalid/unsafe output
             |                     |
             v                     v
 Deterministic policy engine  Safe fallback / review
             |
             v
 Persisted evaluation + audit + telemetry
             |
             v
 React/portal displays source-linked result and status
```

### 4.1 Trust boundaries

- Uploaded PDFs, OCR text, vendor quotations, retrieved chunks, user-supplied text, and model output are untrusted.
- Authenticated identity and permissions must come from the server-side session/security layer.
- Policy and workflow state must come from authoritative repositories and deterministic services.
- An LLM must not receive secrets, API keys, internal credentials, or unnecessary personal data.
- The model must not choose its own authorization scope or widen document access.
- The response presented to the user must be generated from validated data and persisted state, not a model claim that an action succeeded.

---

# Workstream A — AI security guardrails

## 5. A1. Define task-specific AI contracts

Each LLM use case must have a narrow, versioned contract. Do not use one permissive prompt for unrelated tasks.

Initial task types:

- `policy_rule_extraction`
- `policy_result_summary`
- `evidence_explanation`
- `document_field_extraction` (where used)
- `retrieval_query_rewrite` (optional, disabled until evaluated)

For each task, define:

- Task name and version.
- System instruction and prompt version.
- Input schema and maximum lengths.
- Output Pydantic schema.
- Permitted source fields.
- Permitted output fields.
- Evidence requirements.
- Prohibited claims/actions.
- Safe fallback behavior.
- Model and provider configuration.
- Timeout and retry limits.
- Whether the task may run on sensitive content.
- Evaluation dataset and release gate.

### Acceptance criteria

- Every LLM call is associated with a known task type and prompt version.
- Unrecognized task types cannot fall through to a generic unrestricted prompt.
- Output is parsed into a typed schema before being used by application code.
- Invalid output is rejected or converted into an explicit review/fallback result.
- No model output directly mutates workflow state or activates a rule.

## 6. A2. Harden against direct and indirect prompt injection

### Required behavior

1. Treat all document text and retrieved passages as untrusted data.
2. Keep system instructions separate from JSON-encoded evidence input.
3. Explicitly instruct the model to extract or summarize evidence without following instructions found inside evidence.
4. Do not concatenate raw document text into system/developer instructions.
5. Add injection detection as a signal for review, not as the only defense.
6. Validate model output against the task schema and source evidence.
7. Never permit document text to request or cause tool execution, secrets disclosure, policy override, or rule activation.
8. Ensure that a malicious passage cannot alter the authorization scope used by retrieval.
9. Avoid unnecessary tools/functions in extraction and explanation tasks.
10. Do not send secrets or credentials to the model.

### Adversarial cases to test

- “Ignore previous instructions” and similar phrases in normal document text.
- Hidden text, white-on-white text, tiny text, or suspicious OCR output.
- Instructions embedded in vendor quotes, tables, footnotes, and scanned pages.
- Documents that ask the model to reveal prompts, credentials, or other documents.
- A retrieved clause that attempts to force an approval or change the output schema.
- Cross-document attempts to cite a chunk from another vendor or document.
- A valid-looking but fabricated evidence ID.
- A real evidence ID paired with a quote that does not occur in the chunk.
- Unicode homoglyphs, unusual whitespace, and split instructions across chunks.
- Multiple malicious and legitimate clauses in the same document.

### Acceptance criteria

- An injection attempt cannot activate or approve a policy rule.
- Invalid/fabricated evidence is rejected.
- A suspicious document can be retained for human review without being treated as authoritative automatically.
- The system records a safe event code and correlation ID without logging full malicious content by default.
- Tests cover both LLM-enabled and deterministic fallback paths.

## 7. A3. Validate evidence and citations

Implement or strengthen a shared server-side evidence-validation service rather than duplicating checks in each endpoint.

For every evidence reference, validate:

- Evidence/chunk ID exists.
- The chunk belongs to the expected document.
- The document is in an allowed authoritative status for the task.
- Document version and content hash match the indexed chunk.
- The document is within the caller’s authorized scope.
- Vendor, department, jurisdiction, document type, and effective dates match applicable filters where relevant.
- Page/section metadata comes from the stored source record, not model output.
- A model-provided excerpt is an exact or safely normalized substring of the cited chunk.
- Superseded or archived documents are not treated as current evidence unless historical lookup was explicitly requested and authorized.

### Citation behavior

- If citation validation fails, reject the citation.
- If no valid evidence remains for a required claim, return `insufficient_evidence` or `needs_review`.
- Do not silently substitute a similar document or fabricate page numbers.
- Preserve document ID, version, content hash, chunk ID, page, section, and retrieval configuration in the persisted result.
- Render a user-facing source link only if the user is authorized to access that source.

### Acceptance criteria

- Every displayed policy citation resolves to the exact stored source version.
- Unauthorized users cannot use a citation to retrieve a restricted document.
- Model-supplied source metadata is never trusted over server metadata.
- Citation failures are measurable and visible in evaluation reports.

## 8. A4. Enforce structured output and safe failure behavior

For every LLM response:

1. Parse into the task’s Pydantic schema.
2. Reject unknown or prohibited fields where feasible (`extra="forbid"`).
3. Validate enums, types, string lengths, list lengths, numeric bounds, and evidence references.
4. Enforce maximum response size and candidate count.
5. Reject unsupported decision values and arbitrary expressions/code.
6. Do not repair malformed output by inventing missing business facts.
7. Use a deterministic fallback where one exists.
8. Otherwise return a review-required/unavailable result with a safe error code.
9. Do not expose raw provider errors, stack traces, prompts, or internal secrets to end users.

Retries must be bounded. A retry may request schema correction, but it must not weaken validation or silently change the task’s rules.

### Acceptance criteria

- Malformed, oversized, incomplete, or unexpected output never causes a successful policy pass by default.
- Provider timeout, authentication failure, rate limit, and invalid output are distinguishable operationally.
- A safe fallback is documented and tested for each LLM task.

## 9. A5. Keep AI advisory and deterministic decisions authoritative

- LLM-generated summaries may explain validated findings but cannot modify findings, severities, reason codes, approval requirements, or final workflow outcomes.
- LLM-derived policy rules remain candidates until authorized human review and approval.
- The rule evaluator must accept only the supported typed rule language.
- Never execute Python, JavaScript, SQL, shell, or arbitrary expressions produced by a model or a policy document.
- Money calculations use `Decimal` and deterministic code.
- Vendor ranking or recommendation must show its source data and limitations; it must not silently award a vendor.
- External procurement and payment actions remain disabled unless a separately reviewed feature explicitly authorizes them.
- When AI output conflicts with deterministic evaluation, deterministic evaluation wins and the discrepancy is recorded.

### Invariant tests

- Changing the LLM summary cannot change the evaluation outcome.
- An LLM cannot remove a hard block or reduce a review requirement.
- An LLM cannot approve or activate a rule.
- Missing evidence cannot be converted to `pass` by generated prose.
- A retrieval outage cannot be presented as a successful semantic search.
- A model timeout cannot advance workflow state.

## 10. A6. Authorization, privacy, and data minimization

- Verify server-side role checks for policy upload, search, rule review, approval, evaluation, and source-document access.
- Apply access filters before scoring/retrieval, not only after the model response.
- Add tenant/workspace scoping to every relevant repository query if the application supports multiple tenants.
- Do not trust `role`, `user_id`, `vendor_key`, or approval identity supplied by the client without server-side verification.
- Minimize personal, financial, and vendor-confidential data in model inputs.
- Redact credentials, access tokens, and unnecessary personal identifiers from logs.
- Define retention for prompts, model responses, retrieved excerpts, and telemetry.
- Use provider-approved data handling settings and the organization’s configured privacy requirements.
- Keep secrets in environment/secret-management infrastructure, never in code, test fixtures, or prompts.

### Acceptance criteria

- Authorization tests cover every endpoint that exposes documents, evidence, or policy results.
- Cross-vendor and cross-tenant retrieval tests fail closed.
- Logs contain enough information for diagnosis without recording secrets or unnecessary raw document text.
- Security configuration is documented for local development and deployed environments.

---

# Workstream B — Tokenization and context budgeting

## 11. B1. Replace approximate token counts with model-aware counting

The current chunker uses a word-based estimate (`words × 1.3`). This is useful as a rough estimate but is not an exact tokenizer count. The local hashed embedding tokenizer is a lightweight regex tokenizer and is not equivalent to an LLM tokenizer.

### Requirements

- Identify the exact tokenizer strategy for each configured model.
- Use a model-compatible tokenizer library where supported.
- Keep tokenization behind a small interface so providers can be changed without rewriting chunking.
- Maintain a documented conservative fallback if the model tokenizer is unavailable.
- Record the tokenizer/model version used to build an index.
- Do not label estimated counts as exact counts.
- Add tests for punctuation, long strings, URLs, JSON, tables, Unicode, Hindi/English mixed content, and OCR artifacts.

### Suggested interface

```python
from typing import Protocol

class TokenCounter(Protocol):
    def count(self, text: str, *, model: str) -> int:
        """Return a model-compatible token count or a documented conservative estimate."""
        ...
```

Implement a provider-specific counter behind this interface. Select the implementation from validated configuration; do not accept an arbitrary tokenizer name directly from an untrusted API request.

### Chunking requirements

- Preserve page boundaries and section headings where practical.
- Keep source page, section, document version, and content hash attached to each chunk.
- Enforce maximum chunk size using actual counts where supported.
- Keep overlap configurable and measured in tokens, not silently in words.
- Handle a single very long paragraph or table row without creating an unbounded chunk.
- Avoid excessive overlap that multiplies storage, embedding cost, and duplicate retrieval.
- Record `token_count`, `tokenizer_id`, `chunking_config_version`, and source metadata.
- Plan an explicit re-indexing path when the tokenizer or chunking configuration changes.

### Acceptance criteria

- No indexed chunk exceeds the configured token limit under the configured tokenizer.
- The approximate fallback is explicitly marked and cannot be mistaken for exact counting.
- Re-indexing is deterministic for a fixed source document and configuration.
- Chunk metadata allows every chunk to be traced to the original file and page.

## 12. B2. Budget complete LLM requests, not just chunks

Chunk size alone does not protect a model request. System instructions, conversation/history, JSON wrappers, evidence metadata, and generated output all consume context.

Before every call:

1. Resolve the configured model and context-window budget.
2. Count or conservatively estimate system instructions, user content, retrieved evidence, and message overhead.
3. Reserve an output-token budget.
4. Enforce a safety margin below the model’s maximum context.
5. Reduce evidence count or excerpt size using a deterministic policy if the budget is exceeded.
6. Never truncate away source IDs or provenance while retaining an unsupported claim.
7. If the request still cannot fit, return a safe `context_budget_exceeded` result or use a documented multi-part strategy.
8. Record actual provider usage when returned and compare it with the estimate.

### Suggested configuration

Use the existing configuration style where possible; validate all values at startup.

```json
{
  "token_budget": {
    "counter_mode": "model_compatible",
    "safety_margin_tokens": 512,
    "max_input_tokens": 12000,
    "max_output_tokens": 1200,
    "max_evidence_items": 8,
    "max_evidence_excerpt_chars": 1200,
    "on_overflow": "reduce_evidence_then_review"
  }
}
```

These numbers are examples only. Choose final limits according to the deployed model, task, provider limits, and cost target. Do not assume that every model supports the same context size.

## 13. B3. Usage, latency, and cost tracking

For each model request, record safe operational metadata:

- Correlation/request ID.
- Task name and prompt version.
- Provider and model ID.
- Tokenizer ID/version.
- Estimated input/output tokens.
- Actual input/output tokens if the provider reports them.
- Latency, retry count, outcome, and safe error category.
- Whether input was truncated or evidence was reduced.
- Whether the result passed schema/evidence validation.
- Cost estimate using versioned configured rates, if applicable.

Do not log API keys, authorization headers, or full prompts by default. Raw prompt capture, if ever required for debugging, must be separately authorized, redacted, access-controlled, and retention-limited.

### Acceptance criteria

- Each LLM task can be measured for token usage, latency, failure rate, and approximate cost.
- Estimated and provider-reported token counts can be compared.
- Dashboards and alerts can distinguish normal operation from degraded/fallback operation.

---

# Workstream C — RAG retrieval quality and source integrity

## 14. C1. Build a versioned RAG benchmark before changing retrieval

Create a curated benchmark of representative procurement/workplace policy queries. The dataset should contain only approved test data or safely anonymized examples.

Each benchmark case should include:

- `case_id`
- Query and relevant query metadata
- Expected document IDs/versions
- Expected relevant chunk IDs or gold source spans
- Expected answerability (`answerable`, `insufficient_evidence`, `conflicting_evidence`)
- Expected document scope/vendor scope
- Expected outcome category where deterministic rules are involved
- Expected citations
- Adversarial tags where applicable
- Dataset version and reviewer

Cover:

- Exact clause lookup.
- Semantic paraphrases.
- Numeric thresholds and currency amounts.
- Exceptions and negations.
- Effective dates and superseded policies.
- Conflicting policies.
- Vendor-specific clauses.
- Missing policy coverage.
- Tables, scanned PDFs, OCR errors, and mixed-language documents.
- Queries with misleading lexical overlap.
- Prompt injection inside source documents.
- Requests that the caller is not authorized to access.

### Acceptance criteria

- Benchmark cases are reviewed by a domain owner.
- Expected citations are grounded in real source text.
- The dataset is versioned and changes require review.
- Results are reproducible for a fixed index and configuration.

## 15. C2. Measure retrieval quality

Implement offline evaluation that reports at minimum:

- **Recall@k:** proportion of known relevant evidence retrieved.
- **Precision@k:** proportion of retrieved items that are relevant.
- **MRR:** ranking quality of the first relevant result.
- **nDCG@k:** ranking quality when relevance has graded labels.
- **Citation validity rate:** citations that resolve to correct stored evidence.
- **Answerability accuracy:** correct distinction between answerable and insufficient/conflicting evidence.
- **Scope violation count:** evidence returned outside the caller’s allowed scope.
- **Degraded-mode rate:** percentage of searches not using the intended semantic hybrid mode.
- **Latency and cost per query.**

Report results overall and by query/document type. A strong average score must not hide failures for security, vendor scope, conflict, or missing-evidence cases.

Do not use a single LLM judge as the sole evaluation method. Use known gold evidence, deterministic checks, human review for ambiguous cases, and optional model-assisted grading as supplementary evidence.

## 16. C3. Improve retrieval in measured steps

Keep the existing hybrid retrieval pipeline as the baseline. Experiment in this order:

1. Confirm document status, effective-date, vendor-scope, and content-hash filters.
2. Tune chunk size and overlap using the benchmark.
3. Improve PDF/table/OCR extraction where errors are observed.
4. Tune keyword/vector fusion and relevance thresholds against the gold set.
5. Test query normalization or expansion only if it improves benchmark results without increasing unsupported matches.
6. Test a reranker on a fixed candidate set if ranking quality remains insufficient.
7. Compare quality gains with latency, provider availability, and cost.
8. Adopt only a versioned configuration that meets release criteria.

Do not enable query rewriting or reranking globally merely because it is available. It can introduce drift, latency, or misleading matches.

## 17. C4. Treat embedding model changes as index migrations

- Store embedding provider, exact model ID, vector dimensions, and embedding/index version with indexed chunks.
- Never compare vectors from incompatible models or dimensions.
- Detect mixed-model indexes and define the retrieval behavior explicitly.
- If the embedding model changes, re-embed affected documents through a controlled migration.
- Keep the prior index/version available until validation passes, where practical.
- Validate vector count, dimensions, document coverage, content hashes, and benchmark quality after re-indexing.
- Record which index and retrieval configuration each evaluation used.

The local hashed vector fallback is not equivalent to a semantic embedding model. It must continue to be reported as degraded and must not be described as full semantic retrieval.

## 18. C5. Improve document extraction quality

For PDF ingestion and OCR:

- Track page-level text quality and low-text pages.
- Identify scanned pages and route them through the configured OCR path where supported.
- Preserve tables and row/column context when extracting pricing or policy requirements.
- Do not infer missing currency, units, effective dates, or exceptions.
- Flag low-quality extraction for human review.
- Store extraction tool/version and quality metadata.
- Verify that chunks map back to the correct page and document version.
- Ensure duplicate uploads and re-indexing do not create ambiguous evidence identities.

### Acceptance criteria

- A low-text or failed-OCR document is not silently treated as complete authoritative evidence.
- Table extraction tests cover rates, units, currencies, and exceptions.
- Low-quality documents are visibly marked and do not bypass existing review rules.

## 19. C6. RAG response and explanation contract

For a user-facing policy explanation, return structured data similar to:

```json
{
  "status": "review_required",
  "summary": "A human review is required because the applicable evidence is incomplete.",
  "findings": [
    {
      "finding_id": "fnd_example",
      "status": "unknown",
      "reason_code": "INSUFFICIENT_POLICY_EVIDENCE",
      "summary": "No sufficiently relevant supporting clause was found.",
      "evidence_ids": [],
      "required_action": "Ask the policy owner to confirm the applicable requirement."
    }
  ],
  "evidence": [],
  "retrieval_mode": "keyword_only_degraded",
  "insufficient_evidence": true,
  "warnings": ["Semantic retrieval was unavailable."],
  "policy_versions": [],
  "evaluation_id": "server-generated-id"
}
```

This is an illustrative contract, not a replacement for existing API schemas. Adapt to current Pydantic models and maintain backward compatibility where required.

Requirements:

- The deterministic result and its reason codes are stored independently of generated prose.
- Generated summaries can only paraphrase validated findings.
- The UI clearly distinguishes evidence found, evidence missing, conflict, degraded retrieval, and human review.
- Every displayed evidence item is clickable only for authorized users.
- The system never says a check passed simply because the LLM produced a confident summary.

---

# Workstream D — Evaluation, monitoring, and release assurance

## 20. D1. Add a layered test strategy

### Unit tests

- Token counts and chunk boundaries.
- Long paragraphs, Unicode, JSON, URLs, tables, and OCR text.
- Evidence ID, document version, and content hash validation.
- Schema validation and oversized output.
- Prompt-injection detection behavior.
- Deterministic fallback behavior.
- Retrieval scoring and filters.
- Degraded-mode metadata.
- Usage/cost telemetry redaction.

### Integration tests

- PDF ingestion through chunking and indexing.
- Retrieval through citation validation.
- LLM output through schema/evidence validation.
- Policy evaluation through persisted findings and audit records.
- Provider timeout and rate-limit handling.
- Re-indexing after a document/model/configuration change.
- Access control through every public retrieval API.
- Database unavailable, partial writes, and retry scenarios where relevant.

### End-to-end tests

- Upload an authoritative policy, retrieve its clause, evaluate a request, display source citations, and preserve audit provenance.
- Upload a suspicious document and confirm no automatic rule activation.
- Query with missing evidence and confirm review-required/unknown handling.
- Query with conflicting clauses and confirm conflict handling.
- Disable remote embeddings and verify the UI displays degraded retrieval.
- Attempt cross-vendor or unauthorized document access and confirm denial.
- Change the generated summary and confirm the deterministic outcome remains unchanged.

### Security tests

- Direct and indirect prompt injection.
- Data exfiltration attempts through prompts.
- Cross-scope retrieval.
- Citation spoofing.
- Malformed model output.
- Denial-of-service-style oversized inputs and repeated requests.
- Rate limiting and bounded retries.
- Secret and PII leakage in logs.
- Dependency and configuration checks.

## 21. D2. Version and persist evaluation metadata

For each policy evaluation, preserve or link to:

- Evaluation ID and timestamp.
- Request/workflow ID.
- Policy document IDs, versions, and content hashes.
- Rule-set version and evaluator version.
- Embedding provider/model and index version.
- Chunking/tokenizer version where relevant.
- Retrieval configuration version and mode.
- Prompt/task version and LLM model where used.
- Evidence IDs and validation outcomes.
- Deterministic findings and outcome.
- Optional LLM summary metadata.
- Safe error/degradation codes.
- Correlation ID.

Avoid putting raw prompts or complete document contents into immutable audit events by default. Store references and hashes, and use controlled access for sensitive diagnostic data.

## 22. D3. Operational metrics and alerts

Monitor:

- LLM success/error/timeout/rate-limit rate by task and provider.
- Embedding success rate and fallback rate.
- `hybrid`, `hybrid_local_hash`, and `keyword_only_degraded` usage.
- Retrieval latency and end-to-end evaluation latency.
- Recall/precision and citation-validity results from scheduled benchmark runs.
- Invalid schema and invalid citation rates.
- Prompt-injection detections and rejected outputs.
- Insufficient-evidence and conflict rates.
- Token usage, cost estimates, and unexpected usage spikes.
- OCR failure/low-text page rates.
- Re-index failures and stale index/document-version mismatches.

### Alert principles

- Alert on sustained failures, unusual cost/volume spikes, or an unexpected increase in degraded retrieval.
- Do not page operators for every individual suspicious phrase; aggregate and prioritize.
- Alerts must not include secrets or sensitive document excerpts.
- Define owner, severity, response steps, and runbook for each alert.
- If a mandatory security or retrieval component fails, follow the documented safe fallback rather than silently proceeding.

## 23. D4. Release gates

The release pipeline should fail if any of the following occur:

- A test demonstrates that an LLM can override a deterministic hard block or approval requirement.
- A fabricated citation is accepted.
- A restricted document is returned to an unauthorized user.
- Missing mandatory evidence is silently treated as compliant.
- A model/provider failure is represented as successful semantic retrieval.
- A chunk exceeds the configured hard token limit under the configured tokenizer.
- A model/index migration creates incompatible vector comparisons.
- Critical benchmark cases regress beyond the approved tolerance.
- Secrets are detected in committed code/configuration or unredacted logs in test fixtures.
- Required security tests are missing or skipped without explicit approval.

Set numeric quality thresholds after measuring the baseline on a reviewed dataset. Do not invent arbitrary target percentages before a trustworthy baseline exists. Security invariants and authorization failures should be zero-tolerance release blockers.

---

# 24. Proposed module layout

Reuse existing files where they already fulfill the responsibility. The names below describe logical boundaries, not a requirement to create every file.

```text
app/
  llm.py                              # Existing shared client; preserve safe fallback behavior
  ai_security/
    __init__.py
    contracts.py                      # Task-specific schemas/contracts
    evidence_validation.py            # Shared evidence/citation validation
    output_validation.py              # Typed output and size validation
    injection_signals.py              # Detection signals; not the only defense
    redaction.py                      # Safe telemetry redaction
  policy_rag/
    chunking.py                       # Model-aware token-count integration
    tokenization.py                   # TokenCounter interface/provider adapters
    embeddings.py                      # Model identity/dimensions/compatibility
    retrieval.py                       # Existing hybrid retrieval; benchmark-driven changes
    evaluation.py                      # Deterministic evaluation; preserve decision invariants
    benchmark.py                       # Offline benchmark runner
    telemetry.py                       # Retrieval/LLM usage events
  policy_rules/
    extraction.py                      # Existing candidate extraction; stronger validation
tests/
  ai_security/
  policy_rag/
  policy_rules/
  integration/
  e2e/
data/
  eval/
    rag_benchmark_v1.jsonl             # Approved, versioned benchmark cases
docs/
  AI_GUARDRAILS_TOKENIZATION_RAG_ENHANCEMENT_PLAN.md
```

Do not create this structure blindly. Inspect existing packages first, then add only missing responsibilities.

---

# 25. API and data contract considerations

Any new fields should be additive where possible and versioned appropriately. Existing API response contracts should not be broken without a migration plan.

Potential metadata fields:

- `task_name`
- `prompt_version`
- `model_id`
- `tokenizer_id`
- `token_count_estimated`
- `token_count_actual`
- `chunking_config_version`
- `index_version`
- `retrieval_config_version`
- `retrieval_mode`
- `degradation_reason`
- `citation_validation_status`
- `insufficient_evidence`
- `correlation_id`
- `safe_error_code`

Do not expose internal prompts, secrets, raw provider stack traces, hidden chain-of-thought, or private implementation details to the client.

---

# 26. Configuration and secrets

- Keep provider credentials in environment variables or an approved secret manager.
- Validate model IDs, limits, thresholds, timeouts, and retry counts at startup.
- Separate development/test/offline settings from production settings.
- Avoid silent production fallback to local hashed vectors if policy requires semantic retrieval; either explicitly allow degraded operation with a visible warning or fail closed for the affected mandatory check.
- Document whether each task may run when the provider is unavailable.
- Set request timeouts, bounded retries, and rate limits.
- Ensure logs redact secrets and sensitive input.
- Pin dependency versions and scan dependencies in CI.
- Never place real secrets in benchmark fixtures.

---

# 27. Implementation phases and order

## Phase AI-1 — Baseline and security invariants

**Deliverables**
- Inventory all LLM call sites and classify them by task.
- Confirm existing deterministic decision boundaries.
- Create invariant tests for approvals, hard blocks, missing evidence, and rule activation.
- Add or strengthen citation/evidence validation.
- Add adversarial prompt-injection tests.
- Verify role/vendor/document scope on retrieval APIs.

**Exit criteria**
- The AI layer cannot override deterministic outcomes in tests.
- Fabricated citations and unauthorized evidence are rejected.
- Existing tests pass without weakening their assertions.

## Phase AI-2 — Tokenization and request budgeting

**Deliverables**
- TokenCounter abstraction and configured provider/model strategy.
- Accurate or explicitly conservative token counts.
- Chunk limit and overlap enforcement.
- Full request context budgeting with output reserve and safety margin.
- Usage metadata and cost estimation.

**Exit criteria**
- Oversized requests are safely reduced or rejected.
- Token counts and fallback mode are visible in telemetry.
- Existing source provenance is retained after re-indexing.

## Phase AI-3 — RAG benchmark and quality improvement

**Deliverables**
- Reviewed benchmark dataset.
- Retrieval metric runner.
- Baseline report by query/document type.
- Improvements to chunking, fusion, thresholds, extraction, or reranking only where justified.
- Versioned index/configuration migration procedure.

**Exit criteria**
- Retrieval changes demonstrate measured improvement without security or scope regressions.
- Missing/conflicting evidence is correctly identified on the benchmark.
- Degraded retrieval remains explicit.

## Phase AI-4 — Monitoring and production release gates

**Deliverables**
- Structured telemetry and redaction.
- Operational dashboard and actionable alerts.
- CI release gates for security invariants and benchmark regressions.
- Runbooks for provider outage, degraded retrieval, bad index, and cost spike.
- Final security and regression review.

**Exit criteria**
- Operators can identify why a model or retrieval task failed without accessing secrets.
- Security invariants are release blockers.
- Benchmark and test results are available for every release.

---

# 28. Definition of done

This enhancement is complete only when all applicable criteria below are met.

### Guardrails
- [ ] All LLM call sites are inventoried and have task-specific contracts.
- [ ] Model outputs are schema-validated and evidence-validated.
- [ ] Prompt-injection tests cover document and retrieval paths.
- [ ] No LLM can change deterministic policy/approval outcomes.
- [ ] Candidate rules require authorized human approval.
- [ ] Access control and source-document authorization are tested.
- [ ] Sensitive data and secrets are excluded from ordinary logs.

### Tokenization
- [ ] Configured model/tokenizer mapping is explicit and tested.
- [ ] Approximate counts are marked as estimates.
- [ ] Chunk and request budgets are enforced.
- [ ] Output reserve and context safety margin are enforced.
- [ ] Token usage and cost metadata are recorded safely.
- [ ] Re-indexing after tokenization changes is documented and tested.

### RAG
- [ ] Benchmark dataset is reviewed and versioned.
- [ ] Recall@k, precision@k, MRR/nDCG where relevant, citation validity, answerability, latency, and cost are reported.
- [ ] Model/index compatibility is validated.
- [ ] Effective dates, status, vendor scope, and content hashes are enforced.
- [ ] OCR/table extraction quality is measured.
- [ ] Degraded modes are explicit and tested.
- [ ] No unsupported pass is returned for missing evidence.

### Operations
- [ ] Telemetry is redacted and actionable.
- [ ] Alerts have owners and runbooks.
- [ ] CI blocks security invariant failures.
- [ ] A release report records test and benchmark results.
- [ ] No procurement/payment execution was enabled by this work.

---

# 29. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Prompt injection bypasses simple pattern checks | Defense in depth: untrusted-data boundaries, no unnecessary tools, schema/evidence validation, least privilege, adversarial tests |
| Token counts differ from provider accounting | Model-specific tokenizer, safety margin, provider usage reconciliation |
| Reranking increases cost/latency | Benchmark offline first; gate adoption on measured quality gain |
| Fallback retrieval looks deceptively successful | Explicit mode and warning; policy-specific fail-safe behavior |
| Index contains stale or mixed-model vectors | Model/index metadata, compatibility checks, controlled re-indexing |
| OCR mistakes change policy meaning | Page-level quality flags, source inspection, human review for low-quality extraction |
| Logs expose sensitive documents | Structured redacted telemetry, access controls, retention limits |
| Benchmark misses real cases | Domain-owner review, production feedback loop, versioned additions |
| New abstraction duplicates existing code | Inspect current modules first; extend existing responsibilities |
| LLM summary contradicts deterministic findings | Store summary separately; UI prioritizes structured deterministic findings; invariant tests |

---

# 30. Final recommendation

Implement this plan in sequence:

1. **Security invariants and evidence validation first.**
2. **Model-aware tokenization and full-request budgeting second.**
3. **A reviewed RAG benchmark and measured retrieval improvements third.**
4. **Monitoring, alerting, and CI release gates fourth.**

Do not begin by switching models, adding a reranker, or creating more agents. Establish the baseline, add measurable tests, and then make changes that demonstrate an improvement.

The desired end state is not “AI makes more decisions.” It is a system where AI can extract and explain policy evidence more reliably while deterministic application logic, source provenance, authorization, and human review remain in control.
