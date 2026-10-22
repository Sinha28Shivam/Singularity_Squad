"""Offline, reproducible RAG retrieval benchmark (AI plan C1/C2).

The benchmark builds its own SYNTHETIC corpus (``data/eval/rag_corpus_v1.json``), renders each document to a
PDF and ingests it through the real ingestion path (extract -> chunk -> embed -> index) into the *current*
database (use a fresh/in-memory DB). Gold evidence is declared as (document key, exact substring); expected
chunk ids are resolved after indexing by substring match, so the dataset stays valid when chunking changes.

Metrics (deterministic; no LLM judge):

- ``recall_at_k``: gold spans found in the top-k evidence / gold spans (cases with gold spans only).
- ``precision_at_k``: relevant evidence in top-k / evidence returned in top-k (cases that returned evidence).
- ``mrr``: 1 / rank of the first relevant evidence (0 when none).
- ``ndcg_at_k``: binary relevance; ideal DCG uses min(k, number of relevant chunks).
- ``citation_validity_rate``: returned evidence whose chunk exists, belongs to the cited document, whose
  content hash matches the stored document and whose excerpt is real chunk text.
- ``answerability_accuracy``: ``insufficient_evidence`` flag vs expectation (``conflicting_evidence`` cases are
  correct only when every conflicting gold passage is retrieved, so the deterministic layer can flag it).
- ``scope_violations``: evidence outside the caller's scope (other vendor, superseded/out-of-date document
  without ``include_historical``, or any evidence returned to an unauthorized caller).
- ``forbidden_evidence_hits``: evidence containing a span the case declares must not be returned.
- ``degraded_mode_rate``: searches not in the intended semantic ``hybrid`` mode.
- latency p50/p95 (ms, nearest rank; informational, never gated).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Optional

from app.data_loader import DATA_DIR

EVAL_DIR = DATA_DIR / "eval"
DEFAULT_DATASET = EVAL_DIR / "rag_benchmark_v1.jsonl"
DEFAULT_CORPUS = EVAL_DIR / "rag_corpus_v1.json"
DEFAULT_K = 5
BENCHMARK_VERSION = "rag-benchmark-runner-v1"
ANSWERABILITY = ("answerable", "insufficient_evidence", "conflicting_evidence")
QUERY_TYPES = ("exact_clause", "paraphrase", "numeric_threshold", "exception_negation", "effective_date",
               "vendor_specific", "missing_coverage", "misleading_overlap", "injection", "unauthorized_scope",
               "mixed_language", "table", "conflicting_policy")
# Metrics where higher is better / lower is better (used by regression comparison).
HIGHER_IS_BETTER = ("recall_at_k", "precision_at_k", "mrr", "ndcg_at_k", "answerability_accuracy",
                    "citation_validity_rate")
LOWER_IS_BETTER = ("degraded_mode_rate",)
WORDS_PER_LINE = 12


class BenchmarkError(RuntimeError):
    """The benchmark itself is invalid (bad dataset, ingestion failure, unresolvable gold span)."""


# ------------------------------------------------------------------------------------------- loading
def load_cases(path: Path | str = DEFAULT_DATASET) -> list[dict]:
    cases = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        case = json.loads(line)
        for key in ("case_id", "dataset_version", "query", "query_type", "expected_answerability"):
            if not case.get(key):
                raise BenchmarkError(f"line {n}: missing {key}")
        if case["expected_answerability"] not in ANSWERABILITY:
            raise BenchmarkError(f"{case['case_id']}: bad expected_answerability")
        if case["query_type"] not in QUERY_TYPES:
            raise BenchmarkError(f"{case['case_id']}: unknown query_type {case['query_type']}")
        cases.append(case)
    ids = [c["case_id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise BenchmarkError("duplicate case_id")
    if len({c["dataset_version"] for c in cases}) != 1:
        raise BenchmarkError("mixed dataset_version values")
    return cases


def load_corpus(path: Path | str = DEFAULT_CORPUS) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _sha256_file(path: Path | str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --------------------------------------------------------------------------------- synthetic PDFs
def _wrap(text: str, words_per_line: int = WORDS_PER_LINE) -> list[str]:
    words = text.split()
    return [" ".join(words[i:i + words_per_line]) for i in range(0, len(words), words_per_line)]


def page_lines(page: list[dict]) -> list[str]:
    lines: list[str] = []
    for block in page:
        if block.get("heading"):
            lines.append(block["heading"])
        if block.get("text"):
            lines.extend(_wrap(block["text"]))
        lines.extend(block.get("rows") or [])
    return lines


def render_pdf(pages: list[list[str]]) -> bytes:
    """Minimal PDF with a Type0/Identity-H font and a ToUnicode map, so any Unicode text (e.g. Devanagari,
    the rupee sign) round-trips through text extraction. One page per list of lines."""
    chars = sorted({ch for lines in pages for line in lines for ch in line})
    cid = {ch: i + 1 for i, ch in enumerate(chars)}
    if len(cid) > 0xFFFE:
        raise BenchmarkError("too many distinct characters")
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog_id, pages_id = add(b""), add(b"")
    entries = [b"<%04X> <%s>" % (cid[ch], ch.encode("utf-16-be").hex().upper().encode()) for ch in chars]
    body = b""
    for i in range(0, len(entries), 100):
        part = entries[i:i + 100]
        body += b"%d beginbfchar\n" % len(part) + b"\n".join(part) + b"\nendbfchar\n"
    cmap = (b"/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            b"/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n" + body
            + b"endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n")
    to_unicode = add(b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"endstream")
    descendant = add(b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /BenchSans /CIDSystemInfo << /Registry "
                     b"(Adobe) /Ordering (Identity) /Supplement 0 >> /DW 500 /CIDToGIDMap /Identity >>")
    font = add(b"<< /Type /Font /Subtype /Type0 /BaseFont /BenchSans /Encoding /Identity-H "
               b"/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>" % (descendant, to_unicode))
    kids = []
    for lines in pages:
        parts = [b"BT", b"/F1 10 Tf", b"13 TL", b"50 770 Td"]
        for line in lines:
            parts.append(b"<" + "".join("%04X" % cid[ch] for ch in line).encode() + b"> Tj T*")
        parts.append(b"ET")
        stream = b"\n".join(parts) + b"\n"
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"endstream")
        kids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 %d 0 R "
                        b">> >> /Contents %d 0 R >>" % (pages_id, font, content)))
    objects[catalog_id - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
    objects[pages_id - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % k for k in kids), len(kids))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog_id, xref)
    return bytes(out)


# ------------------------------------------------------------------------------------------ indexing
def norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text or "")).strip()


def ingest_corpus(corpus: dict, *, uploaded_by: str = "benchmark") -> dict[str, str]:
    """Ingest every corpus document via ``ingestion.ingest_bytes``. Returns {document key: document_id}."""
    from app.policy_rag import ingestion
    from app.policy_rag.schemas import PolicyDocumentMetadataIn

    doc_ids: dict[str, str] = {}
    for doc in corpus["documents"]:
        pdf = render_pdf([page_lines(p) for p in doc["pages"]])
        res = ingestion.ingest_bytes(pdf, doc["filename"], PolicyDocumentMetadataIn(**doc["metadata"]), uploaded_by,
                                     source_system="rag-benchmark")
        if not res.ok or not res.document_id or res.status not in ("ready", "superseded"):
            raise BenchmarkError(f"ingestion failed for {doc['key']}: {res.status} {res.error_code}")
        doc_ids[doc["key"]] = res.document_id
    return doc_ids


def _chunks_by_doc(doc_ids: Iterable[str]) -> dict[str, list[dict]]:
    from app.repositories import policy_chunk_repository as chunk_repo
    out: dict[str, list[dict]] = defaultdict(list)
    for c in chunk_repo.list_chunks(document_ids=list(doc_ids), include_embeddings=False):
        out[c["document_id"]].append(c)
    return out


def resolve_spans(spans: list[dict], doc_ids: dict[str, str], chunks: dict[str, list[dict]],
                  case_id: str) -> list[set[str]]:
    """For each span, the chunk ids (of that document) whose text contains the span."""
    resolved = []
    for span in spans:
        if span["doc"] not in doc_ids:
            raise BenchmarkError(f"{case_id}: unknown document key {span['doc']}")
        needle = norm_text(span["text"])
        ids = {c["chunk_id"] for c in chunks.get(doc_ids[span["doc"]], []) if needle in norm_text(c.get("text"))}
        if not ids:
            raise BenchmarkError(f"{case_id}: gold span not found in any chunk of {span['doc']}")
        resolved.append(ids)
    return resolved


# ------------------------------------------------------------------------------------------- checks
def _date(value: Any) -> Optional[dt.date]:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def citation_valid(ev: Any, chunk: Optional[dict], doc: Optional[dict]) -> bool:
    if chunk is None or doc is None:
        return False
    if chunk.get("document_id") != ev.document_id or doc.get("document_id") != ev.document_id:
        return False
    if not (chunk.get("content_hash") == doc.get("content_hash") == ev.content_hash):
        return False
    if int(chunk.get("page_number") or 0) != int(ev.page_number):
        return False
    excerpt = norm_text((ev.excerpt or "").strip("…"))
    return bool(excerpt) and excerpt in norm_text(chunk.get("text"))


def scope_violation(ev: Any, doc: Optional[dict], filters: dict) -> Optional[str]:
    if doc is None:
        return "unknown_document"
    vendor = filters.get("vendor_key")
    if doc.get("vendor_key") and doc.get("vendor_key") != vendor:
        return "other_vendor_document"
    if not filters.get("include_historical"):
        if doc.get("status") != "ready":
            return "non_current_document"
        as_of = _date(filters.get("as_of"))
        start, end = _date(doc.get("effective_from")), _date(doc.get("effective_to"))
        if as_of and ((start and start > as_of) or (end and end < as_of)):
            return "not_effective_on_as_of"
    return None


def percentile(values: list[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 4) if values else None


# --------------------------------------------------------------------------------------------- run
def _viewer(role: str):
    from app.portal_schemas import UserPublic
    return UserPublic(username=f"bench-{role}", name="Benchmark", email=f"bench-{role}@example.com", role=role)


def run_case(case: dict, doc_ids: dict[str, str], chunks: dict[str, list[dict]], k: int) -> dict:
    from app.ai_security.injection_signals import suspicious_hits
    from app.policy_rag import retrieval
    from app.policy_rag.errors import PolicyRagError
    from app.policy_rag.schemas import PolicySearchIn
    from app.repositories import policy_chunk_repository as chunk_repo
    from app.repositories import policy_document_repository as doc_repo

    filters = dict(case.get("filters") or {})
    gold = resolve_spans(case.get("gold_spans") or [], doc_ids, chunks, case["case_id"])
    forbidden = resolve_spans(case.get("forbidden_spans") or [], doc_ids, chunks, case["case_id"])
    relevant = set().union(*gold) if gold else set()
    forbidden_ids = set().union(*forbidden) if forbidden else set()
    params = PolicySearchIn(query=case["query"], vendor_key=filters.get("vendor_key"),
                            document_types=filters.get("document_types"), as_of=_date(filters.get("as_of")),
                            include_historical=bool(filters.get("include_historical")), top_k=k)
    role = case.get("caller_role") or "approver"
    out: dict[str, Any] = {"case_id": case["case_id"], "query_type": case["query_type"],
                           "critical": bool(case.get("critical")), "expected_answerability":
                           case["expected_answerability"], "caller_role": role}
    started = time.perf_counter()
    denied = False
    try:
        result = retrieval.search(params, _viewer(role))
    except PolicyRagError as exc:
        if exc.status_code != 403:
            raise
        denied, result = True, None
    out["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)

    authorized_expected = role in ("approver", "admin")
    if result is None:
        evidence, mode, insufficient = [], None, True
        out.update(retrieval_mode=None, degradation_reason=None, retrieval_config_version=None,
                   embedding_model=None)
    else:
        evidence, mode, insufficient = result.evidence[:k], result.retrieval_mode, result.insufficient_evidence
        out.update(retrieval_mode=mode, degradation_reason=result.degradation_reason,
                   retrieval_config_version=result.retrieval_config_version, embedding_model=result.embedding_model)
    out["access_denied"] = denied
    out["authorization_correct"] = denied != authorized_expected

    returned = [e.chunk_id for e in evidence]
    out["returned_chunk_ids"] = returned
    out["returned_documents"] = [next((key for key, i in doc_ids.items() if i == e.document_id), e.document_id)
                                 for e in evidence]
    valid, violations = 0, []
    injection_flagged = 0
    for e in evidence:
        chunk = chunk_repo.get_chunk(e.chunk_id)
        doc = doc_repo.get_document(e.document_id)
        valid += 1 if citation_valid(e, chunk, doc) else 0
        reason = "unauthorized_caller" if not authorized_expected else scope_violation(e, doc, filters)
        if reason:
            violations.append({"chunk_id": e.chunk_id, "reason": reason})
        if suspicious_hits(e.excerpt or ""):
            injection_flagged += 1
    out["citations_total"] = len(evidence)
    out["citations_valid"] = valid
    out["scope_violations"] = violations
    out["forbidden_hits"] = [cid for cid in returned if cid in forbidden_ids]
    out["injection_flagged_evidence"] = injection_flagged

    if gold:
        found = sum(1 for ids in gold if ids & set(returned))
        out["recall_at_k"] = round(found / len(gold), 4)
        rel_flags = [1 if cid in relevant else 0 for cid in returned]
        out["precision_at_k"] = round(sum(rel_flags) / len(returned), 4) if returned else None
        first = next((i for i, f in enumerate(rel_flags, start=1) if f), None)
        out["mrr"] = round(1 / first, 4) if first else 0.0
        dcg = sum(f / math.log2(i + 1) for i, f in enumerate(rel_flags, start=1))
        idcg = sum(1 / math.log2(i + 1) for i in range(1, min(k, len(relevant)) + 1))
        out["ndcg_at_k"] = round(dcg / idcg, 4) if idcg else 0.0
    else:
        out.update(recall_at_k=None, precision_at_k=None, mrr=None, ndcg_at_k=None)

    expected = case["expected_answerability"]
    if expected == "insufficient_evidence":
        correct = bool(insufficient)
        predicted = "insufficient_evidence" if insufficient else "answerable"
    elif expected == "conflicting_evidence":
        correct = (not insufficient) and out["recall_at_k"] == 1.0
        predicted = "conflicting_evidence" if correct else ("insufficient_evidence" if insufficient else "answerable")
    else:
        correct = not insufficient
        predicted = "insufficient_evidence" if insufficient else "answerable"
    out["predicted_answerability"] = predicted
    out["answerability_correct"] = correct
    out["degraded"] = mode is not None and mode != "hybrid"

    failures = []
    if not correct:
        failures.append("answerability")
    if gold and out["recall_at_k"] < 1.0:
        failures.append("recall")
    if violations:
        failures.append("scope_violation")
    if out["forbidden_hits"]:
        failures.append("forbidden_evidence")
    if valid != len(evidence):
        failures.append("citation_invalid")
    if not out["authorization_correct"]:
        failures.append("authorization")
    out["failures"] = failures
    out["passed"] = not failures
    return out


def aggregate(results: list[dict]) -> dict:
    def vals(key):
        return [r[key] for r in results if r.get(key) is not None]

    cit_total = sum(r["citations_total"] for r in results)
    cit_valid = sum(r["citations_valid"] for r in results)
    searched = [r for r in results if r.get("retrieval_mode") is not None]
    lat = vals("latency_ms")
    return {
        "cases": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "recall_at_k": _mean(vals("recall_at_k")),
        "precision_at_k": _mean(vals("precision_at_k")),
        "mrr": _mean(vals("mrr")),
        "ndcg_at_k": _mean(vals("ndcg_at_k")),
        "citation_validity_rate": round(cit_valid / cit_total, 4) if cit_total else 1.0,
        "citations_total": cit_total,
        "answerability_accuracy": _mean([1.0 if r["answerability_correct"] else 0.0 for r in results]),
        "scope_violations": sum(len(r["scope_violations"]) for r in results),
        "forbidden_evidence_hits": sum(len(r["forbidden_hits"]) for r in results),
        "authorization_failures": sum(1 for r in results if not r["authorization_correct"]),
        "degraded_mode_rate": (round(sum(1 for r in searched if r["degraded"]) / len(searched), 4)
                               if searched else None),
        "injection_flagged_evidence": sum(r["injection_flagged_evidence"] for r in results),
        "latency_ms_p50": percentile(lat, 50),
        "latency_ms_p95": percentile(lat, 95),
    }


def _tokenizer_id() -> Optional[str]:
    try:
        from app.policy_rag.tokenization import get_token_counter
        return get_token_counter().tokenizer_id
    except Exception:  # noqa: BLE001 - metadata only
        return None


def run_benchmark(*, dataset_path: Path | str = DEFAULT_DATASET, corpus_path: Path | str = DEFAULT_CORPUS,
                  k: int = DEFAULT_K) -> dict:
    """Ingest the synthetic corpus into the current DB, run every case and return a JSON-serializable report."""
    from app.policy_rag import embeddings as emb

    cases = load_cases(dataset_path)
    corpus = load_corpus(corpus_path)
    doc_ids = ingest_corpus(corpus)
    chunks = _chunks_by_doc(doc_ids.values())
    results = [run_case(c, doc_ids, chunks, k) for c in cases]

    by_type: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_type[r["query_type"]].append(r)
    cfg = emb.load_config()
    modes = Counter(r["retrieval_mode"] for r in results if r["retrieval_mode"])
    return {
        "report_type": "rag_retrieval_benchmark",
        "benchmark_version": BENCHMARK_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "config": {
            "dataset_version": cases[0]["dataset_version"],
            "dataset_status": sorted({c.get("status") or "unknown" for c in cases}),
            "dataset_reviewers": sorted({c.get("reviewer") or "unknown" for c in cases}),
            "dataset_sha256": _sha256_file(dataset_path),
            "corpus_version": corpus.get("corpus_version"),
            "corpus_sha256": _sha256_file(corpus_path),
            "k": k,
            "retrieval_config_versions": sorted({r["retrieval_config_version"] for r in results
                                                 if r["retrieval_config_version"]}),
            "retrieval_modes": dict(modes),
            "embedding_models": sorted({r["embedding_model"] for r in results if r["embedding_model"]}),
            "tokenizer_id": _tokenizer_id(),
            "min_relevance": cfg.get("min_relevance"),
            "chunk_target_tokens": cfg.get("chunk_target_tokens"),
            "chunk_overlap_tokens": cfg.get("chunk_overlap_tokens"),
            "chunk_max_tokens": cfg.get("chunk_max_tokens"),
            "llm_judge": False,
        },
        "indexed_documents": len(doc_ids),
        "indexed_chunks": sum(len(v) for v in chunks.values()),
        "overall": aggregate(results),
        "by_query_type": {t: aggregate(rs) for t, rs in sorted(by_type.items())},
        "failing_critical_cases": [{"case_id": r["case_id"], "query_type": r["query_type"], "failures": r["failures"]}
                                   for r in results if r["critical"] and not r["passed"]],
        "cases": results,
    }


# ------------------------------------------------------------------------------- regression check
def compare_reports(current: dict, baseline: dict, *, tolerance: float = 0.0) -> list[str]:
    """Return human-readable regressions (empty list = no regression).

    Zero tolerance (regardless of baseline): scope violations, forbidden evidence, authorization failures,
    citation validity below 1.0. Critical cases passing in the baseline must still pass. Quality metrics may
    not drop (or degraded-mode rate rise) by more than ``tolerance``."""
    problems: list[str] = []
    cur_v = (current.get("config") or {}).get("dataset_version")
    base_v = (baseline.get("config") or {}).get("dataset_version")
    if cur_v != base_v:
        problems.append(f"dataset_version mismatch: current={cur_v} baseline={base_v} (re-baseline with review)")
    cur, base = current.get("overall") or {}, baseline.get("overall") or {}
    for key in ("scope_violations", "forbidden_evidence_hits", "authorization_failures"):
        if (cur.get(key) or 0) > 0:
            problems.append(f"security: {key}={cur.get(key)} (must be 0)")
        elif (cur.get(key) or 0) > (base.get(key) or 0):
            problems.append(f"security: {key} regressed {base.get(key)} -> {cur.get(key)}")
    if cur.get("citation_validity_rate") is None or cur["citation_validity_rate"] < 1.0:
        problems.append(f"security: citation_validity_rate={cur.get('citation_validity_rate')} (must be 1.0)")
    for key in HIGHER_IS_BETTER:
        c, b = cur.get(key), base.get(key)
        if b is not None and (c is None or c < b - tolerance - 1e-9):
            problems.append(f"quality: {key} dropped {b} -> {c} (tolerance {tolerance})")
    for key in LOWER_IS_BETTER:
        c, b = cur.get(key), base.get(key)
        if b is not None and c is not None and c > b + tolerance + 1e-9:
            problems.append(f"quality: {key} rose {b} -> {c} (tolerance {tolerance})")
    base_cases = {r["case_id"]: r for r in baseline.get("cases") or []}
    for r in current.get("cases") or []:
        prev = base_cases.get(r["case_id"])
        if r.get("critical") and not r.get("passed") and (prev is None or prev.get("passed")):
            problems.append(f"critical case regressed: {r['case_id']} ({', '.join(r.get('failures') or [])})")
    cur_ids = {r["case_id"] for r in current.get("cases") or []}
    for cid, prev in base_cases.items():
        if prev.get("critical") and cid not in cur_ids:
            problems.append(f"critical case missing from current run: {cid}")
    return problems
