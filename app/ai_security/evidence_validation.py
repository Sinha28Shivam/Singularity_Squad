"""Shared server-side citation validation (plan A3).

Every evidence reference produced by a model (or supplied by a client) is resolved against the stored chunk and
document records. Page/section/hash/version always come from the server record, never from the reference.
A citation is rejected with a stable reason code when:

- ``CITATION_NOT_FOUND``                 the chunk id does not exist;
- ``CITATION_WRONG_DOCUMENT``            the chunk belongs to a different document than expected/claimed;
- ``CITATION_DOCUMENT_NOT_AUTHORITATIVE`` the document is missing or its status is not allowed for the task;
- ``CITATION_HASH_MISMATCH``             chunk and document (or claimed) content hashes differ;
- ``CITATION_OUT_OF_SCOPE``              the document is scoped to another vendor;
- ``CITATION_EXPIRED``                   the document is not effective on ``as_of`` (unless historical lookup);
- ``CITATION_EXCERPT_MISMATCH``          the quoted excerpt is not a (whitespace/case-normalised) substring.

Rejected citations are never "repaired" by substituting a similar chunk.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.repositories import policy_chunk_repository as chunk_repo
from app.repositories import policy_document_repository as doc_repo

NOT_FOUND = "CITATION_NOT_FOUND"
WRONG_DOCUMENT = "CITATION_WRONG_DOCUMENT"
NOT_AUTHORITATIVE = "CITATION_DOCUMENT_NOT_AUTHORITATIVE"
HASH_MISMATCH = "CITATION_HASH_MISMATCH"
OUT_OF_SCOPE = "CITATION_OUT_OF_SCOPE"
EXPIRED = "CITATION_EXPIRED"
EXCERPT_MISMATCH = "CITATION_EXCERPT_MISMATCH"
REASON_CODES = (NOT_FOUND, WRONG_DOCUMENT, NOT_AUTHORITATIVE, HASH_MISMATCH, OUT_OF_SCOPE, EXPIRED,
                EXCERPT_MISMATCH)

HISTORICAL_STATUSES = frozenset({"superseded", "archived"})
ANY_VENDOR = object()  # sentinel: no vendor-scope filter requested
_WS = re.compile(r"\s+")


def _norm(text: Optional[str]) -> str:
    return _WS.sub(" ", text or "").strip().lower()


def _date(value: Any) -> Optional[dt.date]:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass
class CitationResult:
    valid: bool
    reason_code: Optional[str]
    chunk_id: Optional[str]
    document_id: Optional[str] = None
    document_version: Optional[int] = None
    document_status: Optional[str] = None
    content_hash: Optional[str] = None
    page_number: Optional[int] = None
    section: Optional[str] = None
    vendor_key: Optional[str] = None
    excerpt: Optional[str] = None
    chunk: Optional[dict] = field(default=None, repr=False)      # server record (not for clients)
    document: Optional[dict] = field(default=None, repr=False)   # server record (not for clients)

    def metadata(self) -> dict:
        """Server-sourced citation metadata safe to persist alongside a result."""
        return {"valid": self.valid, "reason_code": self.reason_code, "chunk_id": self.chunk_id,
                "document_id": self.document_id, "document_version": self.document_version,
                "content_hash": self.content_hash, "page_number": self.page_number, "section": self.section}


def _ref_get(ref: Any, *names: str) -> Optional[str]:
    for n in names:
        v = ref.get(n) if isinstance(ref, dict) else getattr(ref, n, None)
        if v not in (None, ""):
            return v if isinstance(v, str) else str(v)
    return None


def validate_citation(ref: Any, *, expected_document_id: Optional[str] = None,
                      allowed_statuses: Iterable[str] = ("ready",), vendor_key: Any = ANY_VENDOR,
                      as_of: Optional[dt.date] = None, include_historical: bool = False,
                      check_effective_dates: bool = True, _cache: Optional[dict] = None) -> CitationResult:
    """Validate one reference ``{evidence_id|chunk_id, document_id?, content_hash?, excerpt?}``.

    ``vendor_key``: omitted = no vendor filter; ``None`` = only vendor-neutral documents; a key = documents that
    are vendor-neutral or scoped to that vendor. ``as_of`` defaults to today. ``include_historical`` skips the
    effective-date check and additionally allows superseded/archived documents. ``check_effective_dates=False``
    is for tasks that legitimately read a not-yet-effective authoritative document (rule extraction copies the
    document's effective dates onto the candidate)."""
    chunk_id = _ref_get(ref, "evidence_id", "chunk_id")
    excerpt = ref.get("excerpt") if isinstance(ref, dict) else getattr(ref, "excerpt", None)
    excerpt = excerpt if isinstance(excerpt, str) and excerpt.strip() else None
    if not chunk_id:
        return CitationResult(False, NOT_FOUND, None)
    cache = _cache if _cache is not None else {}
    chunk = cache.get(("c", chunk_id)) if ("c", chunk_id) in cache else chunk_repo.get_chunk(chunk_id)
    cache[("c", chunk_id)] = chunk
    if not chunk:
        return CitationResult(False, NOT_FOUND, chunk_id)
    doc_id = chunk.get("document_id")
    claimed_doc = _ref_get(ref, "document_id")
    base = dict(chunk_id=chunk_id, document_id=doc_id, chunk=chunk, content_hash=chunk.get("content_hash"),
                page_number=int(chunk["page_number"]) if chunk.get("page_number") else None,
                section=(str(chunk["section"])[:200] if chunk.get("section") else None))
    if (expected_document_id and doc_id != expected_document_id) or (claimed_doc and claimed_doc != doc_id):
        return CitationResult(False, WRONG_DOCUMENT, **base)
    doc = cache.get(("d", doc_id)) if ("d", doc_id) in cache else doc_repo.get_document(doc_id)
    cache[("d", doc_id)] = doc
    statuses = set(allowed_statuses) | (HISTORICAL_STATUSES if include_historical else set())
    if not doc:
        return CitationResult(False, NOT_AUTHORITATIVE, **base)
    base.update(document=doc, document_version=int(doc.get("version") or 1), document_status=doc.get("status"),
                vendor_key=doc.get("vendor_key"))
    if doc.get("status") not in statuses:
        return CitationResult(False, NOT_AUTHORITATIVE, **base)
    claimed_hash = _ref_get(ref, "content_hash")
    if chunk.get("content_hash") != doc.get("content_hash") or (claimed_hash and claimed_hash != doc.get("content_hash")):
        return CitationResult(False, HASH_MISMATCH, **base)
    if vendor_key is not ANY_VENDOR and doc.get("vendor_key") not in (None, vendor_key):
        return CitationResult(False, OUT_OF_SCOPE, **base)
    if check_effective_dates and not include_historical:
        day = as_of or dt.date.today()
        start, end = _date(doc.get("effective_from")), _date(doc.get("effective_to"))
        if (start and day < start) or (end and day > end):
            return CitationResult(False, EXPIRED, **base)
    if excerpt is not None and _norm(excerpt) not in _norm(chunk.get("text")):
        return CitationResult(False, EXCERPT_MISMATCH, **base)
    return CitationResult(True, None, excerpt=excerpt, **base)


def validate_citations(refs: Iterable[Any], **kwargs) -> tuple[list[CitationResult], list[CitationResult], dict]:
    """Validate many references. Returns ``(valid, rejected, stats)``; stats count rejections by reason."""
    cache: dict = {}
    valid: list[CitationResult] = []
    rejected: list[CitationResult] = []
    for ref in refs:
        r = validate_citation(ref, _cache=cache, **kwargs)
        (valid if r.valid else rejected).append(r)
    stats = {"total": len(valid) + len(rejected), "valid": len(valid), "rejected": len(rejected),
             "by_reason": dict(Counter(r.reason_code for r in rejected))}
    return valid, rejected, stats
