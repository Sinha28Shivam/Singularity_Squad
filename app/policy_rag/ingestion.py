"""Policy PDF ingestion: validate -> hash/dedupe -> extract -> store -> chunk -> embed -> index -> rate cards.

Each file is processed independently; a bad file yields a failed ``DocumentUploadResult`` and never aborts
the batch. Database unavailability (``PersistenceError``) propagates so callers fail closed (HTTP 503).
Document text is never logged and never returned by this module.

Versioning: documents sharing a ``logical_key`` are separate records with ``version`` 1, 2, ... A new version
links ``supersedes`` to the previous current version; once the new version is ``ready`` the previous
``ready`` version is marked ``superseded`` (never overwritten or deleted).
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from app.data_loader import DATA_DIR, load_json
from app.db.errors import PersistenceError
from app.policy_rag import events, storage
from app.policy_rag.chunking import chunk_pages, chunking_settings, current_chunking_config_version
from app.policy_rag.errors import PolicyRagError
from app.policy_rag.pdf_extract import (ERROR_MESSAGES, PageText, PdfError, extract_pages, extraction_quality,
                                        has_pdf_signature)
from app.policy_rag.tokenization import get_token_counter
from app.policy_rag.schemas import DocumentUploadResult, PolicyDocument, PolicyDocumentMetadataIn
from app.repositories import policy_chunk_repository as chunk_repo
from app.repositories import policy_document_repository as doc_repo

logger = logging.getLogger(__name__)

CONFIG_FILE = "policy_rag_config.json"
MANIFEST_FILE = "manifest.json"
DEFAULT_SEED_DIR = DATA_DIR / "policies"
LOW_TEXT_REVIEW_RATIO = 0.30
MAX_FILES_PER_BATCH = 20
CURRENT_STATUSES = ("ready", "needs_review", "needs_ocr")  # candidates to be superseded by a new version

MSG_NOT_FOUND = "Policy document not found."
MSG_ARCHIVED = "Archived documents cannot be reprocessed."
MSG_FILE_MISSING = "The stored PDF for this document is unavailable; upload it again."
ERROR_MESSAGES = {**ERROR_MESSAGES,
                  "processing_error": "The document could not be processed. Please retry or contact an admin.",
                  "file_missing": MSG_FILE_MISSING}


def config() -> dict:
    return load_json(CONFIG_FILE)


def upload_max_bytes() -> int:
    return int(float(config().get("upload_max_mb", 20)) * 1024 * 1024)


def batch_max_bytes() -> int:
    cfg = config()
    return int(float(cfg.get("upload_batch_max_mb", float(cfg.get("upload_max_mb", 20)) * 5)) * 1024 * 1024)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_document_id() -> str:
    return f"doc_{uuid.uuid4().hex[:16]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _failed(filename: str, code: str, document_id: Optional[str] = None) -> DocumentUploadResult:
    return DocumentUploadResult(filename=filename, ok=False, document_id=document_id,
                                status="failed" if document_id else None, error_code=code,
                                message=ERROR_MESSAGES.get(code, "The document could not be processed."))


# ---------------------------------------------------------------------------------------------- B hooks
def _embed(texts: list[str]) -> tuple[Optional[list[list[float]]], Optional[str]]:
    """Vectors + model name, or (None, None) when no embedder is available (index_status 'degraded')."""
    if not texts:
        return None, None
    try:
        from app.policy_rag import embeddings as emb  # Agent B module; optional at import time
    except ImportError:
        return None, None
    fallback = getattr(emb, "embed_with_fallback", None)
    if callable(fallback):
        try:
            vectors, model = fallback(texts)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding failed during ingestion (%s); storing chunks keyword-only.",
                           type(exc).__name__)
            return None, None
        if not vectors or len(vectors) != len(texts):
            return None, None
        return vectors, str(model) if model else None
    get_embedder = getattr(emb, "get_embedder", None)
    if not callable(get_embedder):
        return None, None
    try:
        embedder = get_embedder()
        if embedder is None:
            return None, None
        vectors = embedder.embed(texts)
    except Exception as exc:  # noqa: BLE001 - embedding failure degrades indexing, never fails ingestion
        logger.warning("Embedding failed during ingestion (%s); storing chunks keyword-only.", type(exc).__name__)
        return None, None
    if not vectors or len(vectors) != len(texts):
        return None, None
    model = (getattr(embedder, "model", None) or getattr(embedder, "model_name", None)
             or getattr(embedder, "name", None) or type(embedder).__name__)
    return vectors, str(model)


def _index_meta(model: Optional[str], vectors: Optional[list[list[float]]]) -> dict:
    try:
        from app.policy_rag.embeddings import index_metadata
        return index_metadata(model, vectors)
    except ImportError:  # pragma: no cover
        return {"embedding_model": model if vectors else None, "embedding_provider": "none",
                "embedding_dims": len(vectors[0]) if vectors else None}


def _quality(pages: list[PageText], cfg: Optional[dict] = None) -> dict:
    cfg = cfg or config()
    return extraction_quality(pages, low_text_chars=int(cfg.get("low_text_chars_per_page", 80)),
                              review_ratio=LOW_TEXT_REVIEW_RATIO)


def _extract_rate_entries(document: PolicyDocument, pages: list[PageText]) -> Optional[int]:
    """Returns the number of rate rows created, or None when the rate-card module is unavailable."""
    try:
        from app.policy_rag.rate_cards import extract_rate_entries  # Agent B module
    except ImportError:
        return None
    entries = extract_rate_entries(document, pages)
    return len(entries or [])


# ------------------------------------------------------------------------------------------- pipeline
def _index_document(doc: dict, pages: list[PageText], actor: str) -> dict:
    """Chunk, embed, store chunks, extract rate rows and set the final status. Returns the updated doc."""
    cfg = config()
    model = doc_repo.to_model(doc)
    document_id = model.document_id
    settings = chunking_settings(cfg)
    chunks = chunk_pages(pages, model, **settings)
    vectors, embedding_model = _embed([c.text for c in chunks])
    index_meta = _index_meta(embedding_model, vectors)
    if vectors is not None:
        for c in chunks:
            c.embedding_model = embedding_model
            c.embedding_provider = index_meta["embedding_provider"]
            c.embedding_dims = index_meta["embedding_dims"]
    chunk_repo.replace_chunks(document_id, chunks, embeddings=vectors, document_type=model.document_type,
                              vendor_key=model.vendor_key, document_status="indexing")
    index_status = "ready" if vectors is not None else "degraded"
    events.record_safely("document_indexing_completed", actor, document_id=document_id,
                         chunk_count=len(chunks), index_status=index_status, embedding_model=embedding_model)

    rate_count = 0
    rate_failed = False
    if model.document_type == "rate_card":
        # Rate ids are deterministic and inserts skip existing ids, so reprocess keeps verification decisions.
        try:
            created = _extract_rate_entries(model, pages)
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - extraction issues route the document to review
            logger.warning("Rate-card extraction failed for %s (%s).", document_id, type(exc).__name__)
            created, rate_failed = 0, True
        if created is not None:
            rate_count = created
            if created:
                events.record_safely("rate_card_extraction_created", actor, document_id=document_id,
                                     rate_entry_count=created)
            else:
                rate_failed = True

    low = [p.page_number for p in pages if p.low_text]
    needs_review = (not chunks) or (pages and len(low) / len(pages) > LOW_TEXT_REVIEW_RATIO) or rate_failed
    if doc.get("status") in ("superseded",):
        status = "superseded"  # reprocessing a historical version keeps it historical
    else:
        status = "needs_review" if needs_review else "ready"
    quality = _quality(pages, cfg)
    first = chunks[0] if chunks else None
    fields = {"status": status, "index_status": index_status, "chunk_count": len(chunks),
              "rate_entry_count": rate_count, "extraction_status": "partial" if low else "complete",
              "low_text_pages": low, "page_count": len(pages), "error_code": None,
              "extraction_quality": quality,
              "index_metadata": {**index_meta,
                                 "tokenizer_id": first.tokenizer_id if first else None,
                                 "token_count_exact": first.token_count_exact if first else None,
                                 "chunking_config_version": (first.chunking_config_version if first else
                                                             current_chunking_config_version(cfg)),
                                 "max_chunk_tokens": max((c.token_count or 0) for c in chunks) if chunks else 0,
                                 **settings}}
    updated = doc_repo.update_document(document_id, fields) or {**doc, **fields}
    chunk_repo.set_document_status(document_id, status)
    if status == "needs_review":
        reason = "no_chunks" if not chunks else ("rate_card_extraction" if rate_failed else "low_text_pages")
        events.record_safely("document_marked_needs_review", actor, document_id=document_id, reason=reason,
                             low_text_pages=low)
    return updated


def _supersede_previous(new_doc: dict, actor: str) -> None:
    previous_id = new_doc.get("supersedes")
    if not previous_id or new_doc.get("status") != "ready":
        return
    prev = doc_repo.get_document(previous_id)
    if prev is None or prev.get("status") not in CURRENT_STATUSES:
        return
    doc_repo.update_document(previous_id, {"status": "superseded", "superseded_by": new_doc["document_id"]})
    chunk_repo.set_document_status(previous_id, "superseded")
    events.record_safely("document_version_superseded", actor, document_id=previous_id,
                         superseded_by=new_doc["document_id"], logical_key=new_doc.get("logical_key"))


def _insert_new_version(base: dict, logical_key: str) -> dict:
    """Insert with version = latest + 1, retrying on a concurrent version collision."""
    for _ in range(5):
        latest = doc_repo.latest_version(logical_key)
        version = int(latest["version"]) + 1 if latest else 1
        supersedes = None
        if latest:
            # Link to the newest current version (ready/needs_review), else the newest record of any status.
            current = doc_repo.list_documents(logical_key=logical_key, statuses=CURRENT_STATUSES, limit=1)
            supersedes = (current[0] if current else latest)["document_id"]
        doc = {**base, "version": version, "supersedes": supersedes}
        try:
            return doc_repo.insert_document(PolicyDocument.model_validate(doc).model_dump() | {
                "storage_name": base["storage_name"]})
        except doc_repo.DocumentVersionConflict:
            continue
    raise PersistenceError("Could not allocate a document version; please retry.")


def ingest_bytes(data: bytes, filename: str, meta: PolicyDocumentMetadataIn, uploaded_by: str,
                 *, source_system: Optional[str] = None) -> DocumentUploadResult:
    """Ingest one PDF. Never raises for bad files (returns ok=False); raises PersistenceError if Mongo fails."""
    safe_name = storage.sanitize_filename(filename)
    actor = uploaded_by or "system"
    data = data or b""

    def fail(code: str) -> DocumentUploadResult:
        events.record_safely("document_validation_failed", actor, filename=safe_name, error_code=code,
                             size_bytes=len(data))
        return _failed(safe_name, code)

    if not safe_name.lower().endswith(".pdf") or not has_pdf_signature(data):
        return fail("not_pdf")
    if len(data) > upload_max_bytes():
        return fail("too_large")

    digest = content_hash(data)
    existing = doc_repo.find_by_hash(digest)
    if existing is not None:
        events.record_safely("document_uploaded", actor, filename=safe_name, content_hash=digest,
                             duplicate_of=existing["document_id"])
        return DocumentUploadResult(filename=safe_name, ok=True, document_id=existing["document_id"],
                                    status=existing.get("status"), duplicate_of=existing["document_id"],
                                    message="Identical content already exists; the existing document was kept.")

    cfg = config()
    empty_text = False
    try:
        pages = extract_pages(data, max_pages=int(cfg.get("max_pdf_pages", 500)),
                              low_text_chars=int(cfg.get("low_text_chars_per_page", 80)))
    except PdfError as exc:
        if exc.code != "empty_text":
            return fail(exc.code)
        pages = getattr(exc, "pages", [])
        empty_text = True

    document_id = new_document_id()
    logical_key = meta.logical_key or storage.slugify(safe_name)
    now = _now()
    base: dict[str, Any] = {
        **meta.model_dump(), "document_id": document_id, "logical_key": logical_key, "filename": safe_name,
        "storage_name": storage.storage_name_for(document_id), "content_hash": digest, "status": "extracting",
        "uploaded_by": actor, "uploaded_at": now, "extraction_status": "pending", "index_status": "pending",
        "page_count": len(pages), "low_text_pages": [p.page_number for p in pages if p.low_text],
        "source_system": source_system,
    }
    try:
        storage.save_pdf(document_id, data)
    except OSError as exc:
        logger.error("Could not store uploaded PDF (%s).", type(exc).__name__)
        return fail("processing_error")
    try:
        doc = _insert_new_version(base, logical_key)
    except Exception:
        storage.delete_pdf(base["storage_name"])
        raise
    events.record_safely("document_uploaded", actor, document_id=document_id, filename=safe_name,
                         content_hash=digest, document_type=meta.document_type, vendor_key=meta.vendor_key,
                         logical_key=logical_key, version=doc["version"], size_bytes=len(data))

    if empty_text:
        doc = doc_repo.update_document(document_id, {"status": "needs_ocr", "extraction_status": "failed",
                                                     "index_status": "failed", "error_code": "empty_text",
                                                     "extraction_quality": _quality(pages, cfg)}) or doc
        events.record_safely("document_marked_needs_review", actor, document_id=document_id, reason="empty_text")
        return DocumentUploadResult(filename=safe_name, ok=True, document_id=document_id, status="needs_ocr",
                                    error_code="empty_text", message=ERROR_MESSAGES["empty_text"])

    events.record_safely("document_extraction_completed", actor, document_id=document_id, page_count=len(pages),
                         low_text_pages=base["low_text_pages"])
    try:
        doc = _index_document(doc, pages, actor)
    except PersistenceError:
        raise
    except Exception as exc:  # noqa: BLE001 - per-file isolation; safe error code only
        logger.error("Indexing failed for %s (%s).", document_id, type(exc).__name__)
        chunk_repo.delete_chunks(document_id)
        doc_repo.update_document(document_id, {"status": "failed", "index_status": "failed",
                                               "error_code": "processing_error"})
        return _failed(safe_name, "processing_error", document_id)
    _supersede_previous(doc, actor)
    message = None
    if doc["status"] == "needs_review":
        message = "Extraction quality is low or incomplete; the document needs review before it is used."
    elif doc.get("index_status") == "degraded":
        message = "Indexed for keyword search only (embeddings unavailable)."
    return DocumentUploadResult(filename=safe_name, ok=True, document_id=document_id, status=doc["status"],
                                message=message)


def ingest_batch(files: Iterable[tuple[str, bytes]], metas: list[PolicyDocumentMetadataIn],
                 uploaded_by: str) -> list[DocumentUploadResult]:
    """Ingest each (filename, bytes) with its aligned metadata; results are per file and independent."""
    results = []
    for (name, data), meta in zip(files, metas):
        try:
            results.append(ingest_bytes(data, name, meta, uploaded_by))
        except PersistenceError:
            raise
        except Exception as exc:  # noqa: BLE001 - defensive: one file must never break the batch
            logger.error("Unexpected ingestion failure (%s).", type(exc).__name__)
            results.append(_failed(storage.sanitize_filename(name), "processing_error"))
    return results


# ----------------------------------------------------------------------------------------- lifecycle
def _get_or_404(document_id: str) -> dict:
    doc = doc_repo.get_document(document_id)
    if doc is None:
        raise PolicyRagError(404, MSG_NOT_FOUND)
    return doc


def reprocess(document_id: str, actor: str) -> DocumentUploadResult:
    """Re-extract and re-index a stored PDF (e.g. after a chunking/embedding change). Archived -> 409."""
    doc = _get_or_404(document_id)
    if doc.get("status") == "archived":
        raise PolicyRagError(409, MSG_ARCHIVED)
    filename = doc.get("filename", "document.pdf")
    data = storage.load_pdf(doc.get("storage_name", ""))
    if data is None or content_hash(data) != doc.get("content_hash"):
        doc_repo.update_document(document_id, {"status": "failed", "error_code": "file_missing"})
        chunk_repo.set_document_status(document_id, "failed")
        return _failed(filename, "file_missing", document_id)
    cfg = config()
    events.record_safely("document_reprocessed", actor, document_id=document_id)
    try:
        pages = extract_pages(data, max_pages=int(cfg.get("max_pdf_pages", 500)),
                              low_text_chars=int(cfg.get("low_text_chars_per_page", 80)))
    except PdfError as exc:
        status = "needs_ocr" if exc.code == "empty_text" else "failed"
        fields = {"status": status, "error_code": exc.code}
        if exc.code == "empty_text":
            fields["extraction_quality"] = _quality(getattr(exc, "pages", []), cfg)
        doc_repo.update_document(document_id, fields)
        chunk_repo.delete_chunks(document_id)
        return DocumentUploadResult(filename=filename, ok=status != "failed", document_id=document_id,
                                    status=status, error_code=exc.code, message=ERROR_MESSAGES.get(exc.code))
    if doc.get("status") != "superseded":
        doc = doc_repo.update_document(document_id, {"status": "indexing"}) or doc
    events.record_safely("document_extraction_completed", actor, document_id=document_id, page_count=len(pages),
                         low_text_pages=[p.page_number for p in pages if p.low_text])
    try:
        doc = _index_document(doc, pages, actor)
    except PersistenceError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("Reprocess failed for %s (%s).", document_id, type(exc).__name__)
        chunk_repo.delete_chunks(document_id)
        doc_repo.update_document(document_id, {"status": "failed", "error_code": "processing_error"})
        return _failed(filename, "processing_error", document_id)
    _supersede_previous(doc, actor)
    return DocumentUploadResult(filename=filename, ok=True, document_id=document_id, status=doc["status"])


def current_index_signature() -> dict:
    """Tokenizer and chunking configuration a fresh index build would use now."""
    cfg = config()
    counter = get_token_counter()
    return {"tokenizer_id": counter.tokenizer_id, "token_count_exact": bool(counter.exact),
            "chunking_config_version": current_chunking_config_version(cfg, counter), **chunking_settings(cfg)}


def documents_needing_reindex(*, embedding_model: Optional[str] = None) -> list[str]:
    """Ids of non-archived documents whose chunks were built with a different chunking config or tokenizer
    (or, when given, a different embedding model). Re-index them with ``reindex_document``."""
    sig = current_index_signature()
    return chunk_repo.stale_chunk_documents(sig["chunking_config_version"], sig["tokenizer_id"],
                                            embedding_model=embedding_model)


def reindex_document(document_id: str, actor: str = "system") -> DocumentUploadResult:
    """Controlled re-index of one document from its stored PDF (re-extract, re-chunk, re-embed). Deterministic
    for a fixed file and configuration: chunk ids depend only on document id, content hash and position."""
    return reprocess(document_id, actor)


def reindex_stale_documents(actor: str = "system", *, embedding_model: Optional[str] = None
                            ) -> list[DocumentUploadResult]:
    return [reindex_document(d, actor) for d in documents_needing_reindex(embedding_model=embedding_model)]


def archive(document_id: str, actor: str) -> dict:
    """Mark archived (idempotent). Chunks are excluded from search; file, metadata and events are kept."""
    doc = _get_or_404(document_id)
    if doc.get("status") == "archived":
        return doc_repo.public_view(doc)
    updated = doc_repo.update_document(document_id, {"status": "archived", "archived_at": _now(),
                                                     "archived_by": actor, "status_before_archive": doc.get("status")})
    chunk_repo.set_document_status(document_id, "archived")
    events.record("document_archived", actor, document_id=document_id, previous_status=doc.get("status"))
    return doc_repo.public_view(updated or doc)


# --------------------------------------------------------------------------------------------- seeding
def load_manifest(directory: Path) -> dict[str, dict]:
    path = Path(directory) / MANIFEST_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = raw.get("documents", raw) if isinstance(raw, dict) else raw
    out: dict[str, dict] = {}
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and item.get("filename"):
                out[item["filename"]] = {k: v for k, v in item.items() if k != "filename"}
    elif isinstance(items, dict):
        out = {k: v for k, v in items.items() if isinstance(v, dict)}
    return out


def seed_from_directory(path: str | Path = DEFAULT_SEED_DIR, uploaded_by: str = "system") -> list[DocumentUploadResult]:
    """Ingest PDFs in ``path`` whose content hash is not yet present. Metadata comes from manifest.json
    (unlisted files default to document_type "other"). Already-present files are skipped (not returned)."""
    directory = Path(path)
    if not directory.is_absolute() and not directory.exists():
        directory = DATA_DIR.parent / directory
    manifest = load_manifest(directory)
    results: list[DocumentUploadResult] = []
    for pdf in sorted(directory.glob("*.pdf")):
        try:
            data = pdf.read_bytes()
        except OSError:
            continue
        if doc_repo.find_by_hash(content_hash(data), exclude_statuses=("failed",)) is not None:
            continue
        try:
            meta = PolicyDocumentMetadataIn.model_validate(manifest.get(pdf.name) or {"document_type": "other"})
        except ValueError:
            logger.warning("Invalid manifest metadata for a seed policy file; skipping it.")
            continue
        results.append(ingest_bytes(data, pdf.name, meta, uploaded_by, source_system="seed"))
    return results
