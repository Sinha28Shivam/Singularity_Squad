"""/api/policy-documents/* — policy library upload and inventory.

Admins upload, reprocess and archive. Approvers and admins read metadata. Normal users get 403. Responses
never include document text or server file paths. Identity comes from the bearer session only.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Path, Query, UploadFile
from pydantic import ValidationError

from app.auth.deps import require_admin, require_approver_or_admin
from app.policy_rag import events, ingestion
from app.policy_rag.errors import PolicyRagError
from app.policy_rag.schemas import DocumentStatus, DocumentType, PolicyDocumentMetadataIn
from app.policy_rag.storage import DOCUMENT_ID_PATTERN
from app.portal_schemas import UserPublic
from app.repositories import policy_document_repository as doc_repo

router = APIRouter(prefix="/api/policy-documents", tags=["policy-documents"])

MSG_NO_FILES = "Select at least one PDF to upload."
MSG_TOO_MANY_FILES = f"At most {ingestion.MAX_FILES_PER_BATCH} files can be uploaded at once."
MSG_BATCH_TOO_LARGE = "The upload batch exceeds the maximum total size."
MSG_METADATA_JSON = "metadata must be a JSON list with one object per file."
MSG_METADATA_COUNT = "metadata must contain exactly one entry per uploaded file."
_READ_CHUNK = 1024 * 1024


def _metadata_error(index: int, exc: ValidationError) -> PolicyRagError:
    fields = sorted({".".join(str(p) for p in err.get("loc", ())) or "metadata" for err in exc.errors()})
    msgs = sorted({str(err.get("msg", "invalid value")).replace("Value error, ", "") for err in exc.errors()})
    return PolicyRagError(422, f"Invalid metadata for file #{index + 1} ({', '.join(fields)}): {'; '.join(msgs)}")


def _parse_metadata(raw: str, count: int) -> list[PolicyDocumentMetadataIn]:
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise PolicyRagError(422, MSG_METADATA_JSON) from None
    if isinstance(parsed, dict):
        parsed = parsed["documents"] if "documents" in parsed else ([parsed] if count == 1 else parsed)
    if not isinstance(parsed, list):
        raise PolicyRagError(422, MSG_METADATA_JSON)
    if len(parsed) != count:
        raise PolicyRagError(422, MSG_METADATA_COUNT)
    metas = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            raise PolicyRagError(422, MSG_METADATA_JSON)
        try:
            metas.append(PolicyDocumentMetadataIn.model_validate(item))
        except ValidationError as exc:
            raise _metadata_error(i, exc) from None
    return metas


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    """Read at most ``limit + 1`` bytes so oversize files are detected without buffering them fully."""
    buf = bytearray()
    while len(buf) <= limit:
        chunk = await upload.read(min(_READ_CHUNK, limit + 1 - len(buf)))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


@router.post("/upload")
async def upload(files: list[UploadFile] = File(...), metadata: str = Form(...),
                 actor: UserPublic = Depends(require_admin)):
    if not files:
        raise PolicyRagError(422, MSG_NO_FILES)
    if len(files) > ingestion.MAX_FILES_PER_BATCH:
        raise PolicyRagError(422, MSG_TOO_MANY_FILES)
    metas = _parse_metadata(metadata, len(files))
    per_file = ingestion.upload_max_bytes()
    batch_limit = ingestion.batch_max_bytes()
    items: list[tuple[str, bytes]] = []
    total = 0
    for upload_file in files:
        data = await _read_limited(upload_file, per_file)
        total += len(data)
        if total > batch_limit:
            raise PolicyRagError(413, MSG_BATCH_TOO_LARGE)
        items.append((upload_file.filename or "document.pdf", data))
    results = ingestion.ingest_batch(items, metas, actor.username)
    return {"results": [r.model_dump(mode="json") for r in results]}


@router.get("")
def list_documents(document_type: Optional[DocumentType] = Query(None),
                   vendor_key: Optional[str] = Query(None, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$"),
                   status: Optional[DocumentStatus] = Query(None),
                   logical_key: Optional[str] = Query(None, max_length=96, pattern=r"^[a-z0-9][a-z0-9_.-]*$"),
                   limit: int = Query(200, ge=1, le=500),
                   _: UserPublic = Depends(require_approver_or_admin)):
    docs = doc_repo.list_documents(document_type=document_type, vendor_key=vendor_key, status=status,
                                   logical_key=logical_key, limit=limit)
    return {"documents": [doc_repo.public_view(d) for d in docs], "counts": doc_repo.count_documents_by_status()}


@router.get("/limits")
def upload_limits(_: UserPublic = Depends(require_approver_or_admin)):
    """Upload limits enforced by POST /upload, so clients can pre-validate (the server stays authoritative)."""
    mb = 1024 * 1024
    return {"upload_max_mb": round(ingestion.upload_max_bytes() / mb, 2),
            "upload_batch_max_mb": round(ingestion.batch_max_bytes() / mb, 2),
            "max_files_per_batch": ingestion.MAX_FILES_PER_BATCH,
            "max_pdf_pages": int(ingestion.config().get("max_pdf_pages", 500)),
            "allowed_content_types": ["application/pdf"]}


@router.get("/{document_id}")
def get_document(document_id: str = Path(..., pattern=DOCUMENT_ID_PATTERN),
                 _: UserPublic = Depends(require_approver_or_admin)):
    doc = doc_repo.get_document(document_id)
    if doc is None:
        raise PolicyRagError(404, ingestion.MSG_NOT_FOUND)
    history = [{k: e.get(k) for k in ("event_type", "actor", "created_at", "error_code", "reason",
                                      "superseded_by", "index_status", "chunk_count", "rate_entry_count")
                if e.get(k) is not None}
               for e in events.list_events(document_id=document_id, limit=50)]
    return {**doc_repo.public_view(doc), "events": history}


@router.post("/{document_id}/reprocess")
def reprocess(document_id: str = Path(..., pattern=DOCUMENT_ID_PATTERN),
              actor: UserPublic = Depends(require_admin)):
    result = ingestion.reprocess(document_id, actor.username)
    doc = doc_repo.get_document(document_id)
    return {"result": result.model_dump(mode="json"), "document": doc_repo.public_view(doc) if doc else None}


@router.post("/{document_id}/archive")
def archive(document_id: str = Path(..., pattern=DOCUMENT_ID_PATTERN),
            actor: UserPublic = Depends(require_admin)):
    return {"document": ingestion.archive(document_id, actor.username)}
