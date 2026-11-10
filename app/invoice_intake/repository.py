"""Persistence for invoice intake: MongoDB ``invoice_documents`` metadata + a private on-disk PDF store.

PDF bytes never live in MongoDB. The store follows ``app.vendor_sourcing.storage``: root
``INVOICE_DOC_STORE_DIR`` (default ``data/invoice_doc_store``, gitignored), server-generated keys only, every
resolved path re-checked to stay inside the root, atomic writes. Mongo failures become ``PersistenceError``.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import PyMongoError

from app.data_loader import DATA_DIR
from app.db.errors import PersistenceError
from app.db.mongo import get_database

COLLECTION = "invoice_documents"
MAX_PDF_BYTES = 5 * 1024 * 1024
DOC_ID_RE = re.compile(r"^INVDOC-[A-F0-9]{12}$")
_KEY_RE = re.compile(r"^(INVDOC-[A-F0-9]{12})-([a-f0-9]{16})\.pdf$")
_PROJECTION = {"_id": 0}


class StorageError(Exception):
    """Invalid key/input or the private store could not be read/written."""


# ------------------------------------------------------------------------------------------- file store
def store_dir() -> Path:
    raw = os.getenv("INVOICE_DOC_STORE_DIR", "").strip()
    path = Path(raw) if raw else DATA_DIR / "invoice_doc_store"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _path_for(key: str) -> Path:
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise StorageError("invalid storage key")
    root = store_dir()
    path = (root / key).resolve()
    if path.parent != root:
        raise StorageError("invalid storage key")
    return path


def save_pdf(doc_id: str, data: bytes) -> dict:
    if not DOC_ID_RE.match(doc_id or ""):
        raise StorageError("invalid doc_id")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise StorageError("PDF data must be non-empty bytes")
    if len(data) > MAX_PDF_BYTES:
        raise StorageError("PDF exceeds the storage size limit")
    key = f"{doc_id}-{uuid4().hex[:16]}.pdf"
    target = _path_for(key)
    tmp = target.with_name(f".{target.name}.{uuid4().hex[:8]}.part")
    try:
        with open(tmp, "xb") as fh:
            fh.write(bytes(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise StorageError("PDF could not be stored") from exc
    return {"storage_key": key, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def read_pdf(key: str) -> bytes:
    path = _path_for(key)
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise StorageError("stored PDF not found") from exc
    except OSError as exc:
        raise StorageError("stored PDF could not be read") from exc


def delete_pdf(key: str) -> bool:
    try:
        _path_for(key).unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StorageError("stored PDF could not be deleted") from exc
    return True


# ---------------------------------------------------------------------------------------------- mongo
def _coll():
    return get_database()[COLLECTION]


def ensure_indexes() -> None:
    try:
        coll = _coll()
        coll.create_index([("doc_id", ASCENDING)], unique=True, name="doc_id_unique")
        coll.create_index([("status", ASCENDING), ("uploaded_at", DESCENDING)], name="status_uploaded")
        coll.create_index([("verified.invoice_id", ASCENDING)], name="verified_invoice_id")
    except PyMongoError as exc:
        raise PersistenceError() from exc


def insert(doc: dict) -> None:
    ensure_indexes()
    try:
        _coll().insert_one(dict(doc))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get(doc_id: str) -> Optional[dict]:
    try:
        return _coll().find_one({"doc_id": doc_id}, _PROJECTION)
    except PyMongoError as exc:
        raise PersistenceError() from exc


def list_docs(status: Optional[str] = None, limit: int = 100) -> list[dict]:
    query: dict[str, Any] = {"status": status} if status else {}
    try:
        return list(_coll().find(query, {**_PROJECTION, "extracted_text": 0})
                    .sort("uploaded_at", DESCENDING).limit(max(1, min(int(limit), 500))))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def update_if_status(doc_id: str, expected_status: str, fields: dict, event: Optional[dict] = None) -> bool:
    """Atomic state transition (+ in-document audit event); False if no longer in ``expected_status``."""
    update: dict[str, Any] = {"$set": dict(fields)}
    if event is not None:
        update["$push"] = {"events": dict(event)}
    try:
        res = _coll().update_one({"doc_id": doc_id, "status": expected_status}, update)
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return res.modified_count == 1


def find_by_sha256(sha256: str) -> Optional[dict]:
    try:
        return _coll().find_one({"pdf_sha256": sha256}, {"_id": 0, "doc_id": 1, "status": 1})
    except PyMongoError as exc:
        raise PersistenceError() from exc


def verified_docs() -> list[dict]:
    try:
        return list(_coll().find({"status": "verified"}, {"_id": 0, "doc_id": 1, "verified": 1,
                                                        "verified_delivery": 1}).sort("verified_at", ASCENDING))
    except PyMongoError as exc:
        raise PersistenceError() from exc
