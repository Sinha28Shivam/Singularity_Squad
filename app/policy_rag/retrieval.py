"""Hybrid policy retrieval with citations.

Order of operations (spec §6.2): access/applicability filters first (status, effective dates, vendor scope,
document type), then BM25 keyword scoring and cosine similarity over stored vectors, reciprocal-rank fusion
for ordering, a relevance threshold, and top-k. Every returned field comes from stored chunk/document
metadata; scores are the real computed values. Retrieved text is data only: nothing here interprets it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import math
import re
import secrets
import time
from collections import Counter
from typing import Any, Optional

from app.policy_rag import embeddings as emb
from app.policy_rag.errors import PolicyRagError
from app.policy_rag.schemas import SEARCHABLE_STATUSES, PolicyEvidence, PolicySearchIn, PolicySearchResult

EXCERPT_CHARS = 400
RRF_K = 60
BM25_K1 = 1.5
BM25_B = 0.75
KEYWORD_WEIGHT = 0.6
VECTOR_WEIGHT = 0.4
HISTORICAL_STATUSES = {"superseded"}
DEGRADED_WARNING = ("Semantic (vector) retrieval is unavailable; results use keyword-only matching "
                    "(degraded mode). Absence of evidence is not evidence of compliance.")
LOCAL_HASH_WARNING = ("Vectors come from the local hashed fallback, not a semantic embedding model; retrieval "
                      "is degraded (hybrid_local_hash). Absence of evidence is not evidence of compliance.")
MIXED_INDEX_WARNING = ("The policy index mixes embedding models or vector dimensions; only vectors matching the "
                       "query embedder are compared, other chunks are ranked by keywords only. Re-index to fix.")
DEGRADED_MODES = {"hybrid_local_hash", "keyword_only_degraded"}


def _config_version() -> str:
    return emb.retrieval_config_version({"rrf_k": RRF_K, "kw_weight": KEYWORD_WEIGHT, "vec_weight": VECTOR_WEIGHT})


def _mode_metadata(mode: str, model: Optional[str], reason: Optional[str], mixed_index: bool = False) -> dict:
    from app.policy_rag import chunking, tokenization

    counter = tokenization.get_token_counter()
    return {"embedding_provider": emb.provider_for_model(model) if mode != "keyword_only_degraded" else "none",
            "embedding_model": model if mode != "keyword_only_degraded" else None,
            "embedding_available": mode == "hybrid",
            "degradation_reason": None if mode == "hybrid" and not mixed_index else reason,
            "retrieval_config_version": _config_version(),
            "tokenizer_id": counter.tokenizer_id,
            "chunking_config_version": chunking.current_chunking_config_version(emb.load_config(), counter),
            "mixed_index_warning": bool(mixed_index)}


def _record_telemetry(result: PolicySearchResult, started: float) -> None:
    """Safe per-search telemetry: operational metadata only, never the query or document text."""
    try:
        from app.ai_security import telemetry
        telemetry.record({"kind": "retrieval", "retrieval_mode": result.retrieval_mode,
                          "degradation_reason": result.degradation_reason, "result_count": len(result.evidence),
                          "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                          "insufficient_evidence": result.insufficient_evidence,
                          "retrieval_config_version": result.retrieval_config_version,
                          "embedding_model": result.embedding_model, "tokenizer_id": result.tokenizer_id,
                          "mixed_index_warning": result.mixed_index_warning})
    except Exception:  # noqa: BLE001 - telemetry must never break retrieval
        pass
_STOPWORDS = frozenset("""a an and are as at be been by can do does for from has have how i if in into is it its
may must not of on or our per should so such than that the their them then there these they this those to
under up was we were what when where which who will with within without you your any all also only""".split())


def _terms(text: str) -> list[str]:
    return [t for t in emb.tokenize(text) if t not in _STOPWORDS and len(t) > 1]


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


def _effective(doc: dict, as_of: Optional[dt.date]) -> bool:
    if as_of is None:
        return True
    start, end = _date(doc.get("effective_from")), _date(doc.get("effective_to"))
    return (start is None or start <= as_of) and (end is None or as_of <= end)


def evidence_id_for(chunk: dict) -> str:
    raw = f"{chunk.get('chunk_id')}:{chunk.get('content_hash')}"
    return "ev_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _excerpt(text: str, terms: set[str]) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= EXCERPT_CHARS:
        return flat
    lower = flat.lower()
    pos = -1
    for t in sorted(terms, key=len, reverse=True):
        m = re.search(r"\b" + re.escape(t), lower)
        if m and (pos < 0 or m.start() < pos):
            pos = m.start()
    start = max(0, pos - 120) if pos >= 0 else 0
    if start:
        sp = flat.find(" ", start)
        start = sp + 1 if 0 <= sp < start + 30 else start
    piece = flat[start:start + EXCERPT_CHARS - 2].rstrip()
    return ("…" if start else "") + piece + ("…" if start + len(piece) < len(flat) else "")


def _check_viewer(viewer) -> None:
    if viewer is not None and getattr(viewer, "role", None) not in ("approver", "admin"):
        raise PolicyRagError(403, "Approver or admin access required.")


def applicable_documents(params: PolicySearchIn, *, documents: Optional[list[dict]] = None) -> list[dict]:
    """Access/applicability filter applied BEFORE any scoring."""
    statuses = set(SEARCHABLE_STATUSES) | (HISTORICAL_STATUSES if params.include_historical else set())
    if documents is None:
        from app.repositories import policy_document_repository as docs_repo
        documents = docs_repo.list_documents(statuses=sorted(statuses))
    as_of = params.as_of or (None if params.include_historical else dt.date.today())
    types = set(params.document_types or [])
    out = []
    for d in documents:
        if d.get("status") not in statuses:
            continue
        if types and d.get("document_type") not in types:
            continue
        doc_vendor = d.get("vendor_key")
        if params.vendor_key and doc_vendor and doc_vendor != params.vendor_key:
            continue  # another vendor's document is never evidence for this vendor
        if not _effective(d, as_of):
            continue
        out.append(d)
    return out


def _bm25(query_terms: list[str], docs_terms: list[list[str]]) -> tuple[list[float], dict[str, float]]:
    n = len(docs_terms)
    df: Counter = Counter()
    for terms in docs_terms:
        df.update(set(terms))
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in df}
    avgdl = (sum(len(t) for t in docs_terms) / n) if n else 0.0
    scores = []
    for terms in docs_terms:
        tf = Counter(terms)
        dl = len(terms) or 1
        s = 0.0
        for q in set(query_terms):
            f = tf.get(q, 0)
            if f:
                s += idf[q] * f * (BM25_K1 + 1) / (f + BM25_K1 * (1 - BM25_B + BM25_B * dl / (avgdl or 1)))
        scores.append(s)
    return scores, idf


def _coverage(query_terms: list[str], terms: list[str], idf: dict[str, float], n_docs: int) -> float:
    """IDF-weighted share of the query terms present in the chunk, in [0, 1]."""
    unseen_idf = math.log(1 + (n_docs + 0.5) / 0.5) if n_docs else 1.0
    q = set(query_terms)
    total = sum(idf.get(t, unseen_idf) for t in q)
    if total <= 0:
        return 0.0
    present = set(terms)
    return sum(idf.get(t, unseen_idf) for t in q if t in present) / total


def search(params: PolicySearchIn, viewer=None, *, min_relevance: Optional[float] = None) -> PolicySearchResult:
    """Retrieve applicable evidence. ``viewer=None`` is the trusted system (policy evaluation)."""
    _check_viewer(viewer)
    started = time.perf_counter()
    result = _search(params, min_relevance)
    _record_telemetry(result, started)
    return result


def _search(params: PolicySearchIn, min_relevance: Optional[float]) -> PolicySearchResult:
    from app.repositories import policy_chunk_repository as chunk_repo

    cfg = emb.load_config()
    threshold = float(cfg.get("min_relevance", 0.15) if min_relevance is None else min_relevance)
    query_id = f"query_{secrets.token_hex(8)}"
    warnings: list[str] = []
    docs = applicable_documents(params)
    by_id = {d["document_id"]: d for d in docs}
    chunks = []
    if by_id:
        for c in chunk_repo.list_chunks(document_ids=list(by_id)):
            d = by_id.get(c.get("document_id"))
            if d is None or c.get("content_hash") != d.get("content_hash"):
                continue
            if c.get("document_status") not in (None, d.get("status")):
                continue
            chunks.append(c)

    if not chunks:
        # Nothing to retrieve: report the mode the configured embedder would provide (no search ran).
        provider, model = emb.preferred_provider()
        mode = "hybrid" if provider == "openai" else "hybrid_local_hash"
        return PolicySearchResult(query_id=query_id, evidence=[], retrieval_mode=mode,
                                  insufficient_evidence=True,
                                  warnings=["No applicable policy documents are available for this query."],
                                  **_mode_metadata(mode, model, "local_hash_embeddings_not_semantic"))

    q_terms = _terms(params.query)
    docs_terms = [_terms(f"{c.get('section') or ''} {c.get('text') or ''}") for c in chunks]
    kw_scores, idf = _bm25(q_terms, docs_terms)
    coverage = [_coverage(q_terms, t, idf, len(chunks)) for t in docs_terms]

    # Vector scores: only comparable with chunks embedded by the same model AND dimension as the query vector.
    index_keys = [emb.index_key(c) for c in chunks]
    keys = Counter(k for k in index_keys if k is not None)
    mixed_index = len(keys) > 1
    vec_scores: list[Optional[float]] = [None] * len(chunks)
    mode = "keyword_only_degraded"
    model: Optional[str] = None
    reason = "no_stored_embeddings"
    vectors_used = False
    if keys:
        # Deterministic choice: most chunks first, then model name / dims.
        (model, dims), _n = sorted(keys.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))[0]
        embedder = emb.embedder_for_model(model)
        qv = embedder.embed([params.query]) if embedder is not None else None
        if qv and len(qv[0]) == dims:
            vectors_used = True
            mode = "hybrid" if emb.provider_for_model(model) == "openai" else "hybrid_local_hash"
            reason = "local_hash_embeddings_not_semantic"
            for i, c in enumerate(chunks):
                if index_keys[i] == (model, dims) and len(c["embedding"]) == dims:
                    vec_scores[i] = emb.cosine(qv[0], c["embedding"])
            if sum(keys.values()) < len(chunks) or mixed_index:
                warnings.append("Some chunks have no comparable embedding; they are ranked by keywords only.")
        elif qv:
            reason = "query_embedding_dimension_mismatch"
        else:
            reason = "embedding_provider_unavailable"
    if mixed_index:
        warnings.append(MIXED_INDEX_WARNING)
        if mode == "hybrid":
            reason = "mixed_embedding_index"  # semantic, but part of the library is keyword-only for now
    if mode == "keyword_only_degraded":
        warnings.append(DEGRADED_WARNING)
    elif mode == "hybrid_local_hash":
        warnings.append(LOCAL_HASH_WARNING)

    def rank(values: list[Optional[float]]) -> dict[int, int]:
        order = sorted((i for i, v in enumerate(values) if v is not None and v > 0), key=lambda i: -values[i])
        return {i: r for r, i in enumerate(order, start=1)}

    kw_rank, vec_rank = rank(kw_scores), rank(vec_scores)
    scored = []
    for i, c in enumerate(chunks):
        rrf = (1 / (RRF_K + kw_rank[i]) if i in kw_rank else 0.0) + (1 / (RRF_K + vec_rank[i]) if i in vec_rank else 0.0)
        if rrf <= 0:
            continue
        v = vec_scores[i]
        if vectors_used and v is not None:
            relevance = KEYWORD_WEIGHT * coverage[i] + VECTOR_WEIGHT * max(0.0, v)
        else:
            relevance = coverage[i]
        scored.append((rrf, relevance, i))
    scored.sort(key=lambda x: (-x[0], -x[1], chunks[x[2]].get("chunk_id") or ""))

    q_set = set(q_terms)
    evidence: list[PolicyEvidence] = []
    seen_text: set[tuple[str, str]] = set()
    for _rrf, relevance, i in scored:
        if relevance < threshold:
            continue
        c = chunks[i]
        d = by_id[c["document_id"]]
        key = (d.get("logical_key") or d["document_id"], re.sub(r"\s+", " ", c.get("text") or "")[:200])
        if key in seen_text:
            continue  # same passage from another version of the same logical document
        seen_text.add(key)
        evidence.append(PolicyEvidence(
            evidence_id=evidence_id_for(c), chunk_id=c["chunk_id"], document_id=d["document_id"],
            filename=d.get("filename") or "", content_hash=d.get("content_hash") or "",
            version_label=d.get("version_label"), version=int(d.get("version") or 1),
            page_number=int(c.get("page_number") or 0), section=c.get("section"),
            excerpt=_excerpt(c.get("text") or "", q_set), relevance_score=round(float(relevance), 4),
            document_type=d.get("document_type") or "other", vendor_key=d.get("vendor_key"),
            effective_from=_date(d.get("effective_from")), effective_to=_date(d.get("effective_to"))))
        if len(evidence) >= params.top_k:
            break
    insufficient = not evidence
    if insufficient:
        warnings.append("No sufficiently relevant policy evidence was found.")
    return PolicySearchResult(query_id=query_id, evidence=evidence, retrieval_mode=mode,
                              insufficient_evidence=insufficient, warnings=warnings,
                              **_mode_metadata(mode, model, reason, mixed_index))
