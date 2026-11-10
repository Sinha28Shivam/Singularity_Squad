"""Admin-only vendor invoice / delivery-note PDF intake (``/api/admin/invoice-documents``).

Upload is multipart (field ``file``), PDF only (extension/MIME + ``%PDF-`` signature, 5 MiB, <= 20 pages, not
encrypted). Extracted fields are untrusted suggestions; a document is used for billing only after an admin
verifies (and optionally edits) the fields.
"""
from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.auth.deps import require_admin
from app.db.errors import PersistenceError
from app.invoice_intake import service
from app.invoice_intake.schemas import RejectIn, VerifyIn
from app.portal_schemas import UserPublic

router = APIRouter(prefix="/api/admin/invoice-documents", tags=["invoice intake"])

_UNAVAILABLE = "Invoice intake is temporarily unavailable."
_READ_CHUNK = 256 * 1024
_PDF_TYPES = {"application/pdf", "application/x-pdf"}


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except service.IntakeError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    except PersistenceError as exc:
        raise HTTPException(503, _UNAVAILABLE) from exc


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    buf = bytearray()
    while len(buf) <= limit:
        chunk = await upload.read(min(_READ_CHUNK, limit + 1 - len(buf)))
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


@router.post("", status_code=201)
async def upload_invoice_document(file: UploadFile = File(...), admin: UserPublic = Depends(require_admin)):
    name = file.filename or ""
    ctype = (file.content_type or "").split(";", 1)[0].strip().lower()
    if not name.lower().endswith(".pdf") or (ctype and ctype not in _PDF_TYPES | {"application/octet-stream"}):
        raise HTTPException(415, "Only PDF files are accepted.")
    data = await _read_limited(file, service.MAX_PDF_BYTES)
    if len(data) > service.MAX_PDF_BYTES:
        raise HTTPException(413, "PDF exceeds the 5 MiB upload limit.")
    return _call(service.upload, name, data, admin.username)


@router.get("")
def list_invoice_documents(status: Optional[str] = Query(None, pattern="^(pending_review|verified|rejected)$"),
                           limit: int = Query(100, ge=1, le=500), _: UserPublic = Depends(require_admin)):
    return {"documents": _call(service.list_documents, status, limit)}


@router.get("/{doc_id}")
def get_invoice_document(doc_id: str, _: UserPublic = Depends(require_admin)):
    return _call(service.get_document, doc_id)


@router.post("/{doc_id}/verify")
def verify_invoice_document(doc_id: str, body: VerifyIn, admin: UserPublic = Depends(require_admin)):
    return _call(service.verify, doc_id, body, admin.username)


@router.post("/{doc_id}/reject")
def reject_invoice_document(doc_id: str, body: RejectIn, admin: UserPublic = Depends(require_admin)):
    return _call(service.reject, doc_id, body, admin.username)


@router.get("/{doc_id}/pdf", response_class=Response, responses={200: {"content": {"application/pdf": {}}}})
def download_invoice_pdf(doc_id: str, _: UserPublic = Depends(require_admin)):
    data, filename = _call(service.read_pdf, doc_id)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[:120] or "invoice.pdf"
    return Response(content=data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{safe}"', "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store"})
