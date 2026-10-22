# RAG retrieval benchmark (`rag-bench-v1`)

Offline, reproducible benchmark for policy retrieval (AI plan §14 C1, §15 C2, §23 D4).

> **Status: DRAFT — domain-owner review pending.** Every case carries `"status": "draft"` and
> `"reviewer": "pending-domain-owner-review"`. The plan's acceptance criterion "benchmark cases are reviewed by a
> domain owner" is **not yet met**. Until review, treat the numbers as an engineering baseline, not a quality claim.

## Files

| File | Purpose |
|------|---------|
| `rag_corpus_v1.json` | SYNTHETIC policy corpus (not real company policy): numbered-clause procurement policy, two vendor contracts (`bench-ven-alpha`, `bench-ven-beta`), superseded travel policy v1→v2, expired gift policy, a document with a prompt-injection paragraph, a table-like AV rate sheet, a mixed Hindi/English clause and a conflicting marketing guideline. |
| `rag_benchmark_v1.jsonl` | 30 cases: `case_id`, `query`, `query_type`, `filters` (`vendor_key`, `as_of`, `include_historical`, `document_types`), `caller_role`, `gold_spans` / `forbidden_spans` as *(document key, exact substring)*, `expected_answerability`, `critical`, `adversarial_tags`, `notes`. |
| `baseline_rag_bench_v1.json` | Measured baseline report used by the release gate. |
| `reports/` | Run outputs (git-ignored). |

Gold evidence is a **substring of the source text**, resolved to chunk ids after indexing (chunk ids are
content-derived), so the dataset stays valid when chunking/tokenization changes. A gold span that cannot be
found in any chunk makes the run invalid (exit code 2) instead of silently scoring zero.

## How it runs

`app/policy_rag/benchmark.py` renders each corpus document to a PDF (Unicode-capable, so Devanagari and `₹`
survive extraction), ingests it through the real `ingestion.ingest_bytes` path (extract → chunk → embed →
index → versioning/supersede), then calls `retrieval.search` per case with the case's caller role. No LLM judge.

```powershell
& .\venv\Scripts\python.exe scripts\run_rag_benchmark.py                       # -> data/eval/reports/latest.json
& .\venv\Scripts\python.exe scripts\run_rag_benchmark.py --baseline data\eval\baseline_rag_bench_v1.json
```

The script uses a fresh in-memory mongomock DB, `WORKPLACEOPS_OFFLINE=1` (local hashed embeddings),
`POLICY_RAG_ENABLED=1` and a temporary `POLICY_STORE_DIR`.

## Metrics

Recall@k, Precision@k, MRR, nDCG@k (binary relevance), citation validity rate (chunk exists, belongs to the
cited document, content hash matches the stored document, excerpt is real chunk text), answerability accuracy,
scope violations (other-vendor / non-current / not-effective document, or any evidence to an unauthorized
caller), forbidden-evidence hits, authorization failures, degraded-mode rate (not semantic `hybrid`), latency
p50/p95. Reported overall, per `query_type`, plus failing critical cases.

`conflicting_evidence` cases are scored correct when **all** conflicting passages are retrieved (retrieval has no
conflict detector; the deterministic policy layer flags conflicts).

## Thresholds

Numeric quality thresholds are **taken from the measured baseline** (`baseline_rag_bench_v1.json`), not invented.
Regression rules (`compare_reports`):

- Zero tolerance, independent of baseline: `scope_violations`, `forbidden_evidence_hits`,
  `authorization_failures` must be 0 and `citation_validity_rate` must be 1.0.
- Any critical case that passed in the baseline must still pass.
- Quality metrics may not drop (degraded-mode rate may not rise) by more than `--tolerance` (default 0.0).
- Latency is reported, never gated (machine dependent).

### Measured baseline (k=5, offline `hybrid_local_hash`, `local-hash-512`, `rcfg-fa04094e926b`)

| Metric | Value |
|---|---|
| Recall@5 | 0.9167 |
| Precision@5 | 0.6712 |
| MRR | 0.9167 |
| nDCG@5 | 0.9167 |
| Citation validity | 1.0 |
| Answerability accuracy | 0.9333 |
| Scope violations / forbidden hits / auth failures | 0 / 0 / 0 |
| Degraded-mode rate | 1.0 (offline local hash is never semantic) |
| Failing critical cases | none |

Known non-critical failures in the baseline: `RB-006` (paraphrase with almost no lexical overlap — local hashed
vectors are not semantic) and `RB-027` (Devanagari-only query — retrieval term tokenizer keeps `[a-z0-9]` only).

## Changing the dataset

The dataset is versioned. Any change to cases or corpus requires review, a new `dataset_version` if semantics
change, and a re-measured baseline committed together with the reason. `compare_reports` refuses to compare
reports with different dataset versions.
