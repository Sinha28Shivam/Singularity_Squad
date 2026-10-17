"""Policy document metadata (`policy_documents`). Binary PDFs live on disk (app.policy_rag.storage).

Dates (effective_from/to) are stored as ISO strings because BSON has no date-only type. ``storage_name`` is
kept server-side and stripped by ``public_view``. Versions are separate records linked by ``logical_key``;
nothing is ever overwritten in place except lifecycle fields (status, counts, supersede links).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Optional

from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.db.errors import PersistenceError
from app.policy_rag.schemas import PolicyDocument
from app.repositories.ticket_repository import NO_ID, _as_utc, _resolve_db

_DATE_FIELDS = ("effective_from", "effective_to")
_DATETIME_FIELDS = ("uploaded_at", "updated_at", "archived_at")
# Never exposed to clients: server paths/storage names.
PRIVATE_FIELDS = ("storage_name",)


class DocumentVersionConflict(PersistenceError):
    def __init__(self) -> None:
        super().__init__("A document version with this logical key already exists; please retry.")


def _coll(db: Optional[Database]):
    return _resolve_db(db).policy_documents


def _to_storage(doc: dict) -> dict:
    data: dict[str, Any] = dict(doc)
    data.pop("_id", None)
    for key in _DATE_FIELDS:
        if isinstance(data.get(key), dt.date):
            data[key] = data[key].isoformat()
    for key in _DATETIME_FIELDS:
        if isinstance(data.get(key), dt.datetime):
            data[key] = _as_utc(data[key])
    return data


def _normalize(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    doc.pop("_id", None)
    for key in _DATETIME_FIELDS:
        if isinstance(doc.get(key), dt.datetime):
            doc[key] = _as_utc(doc[key])
    return doc


def to_model(doc: dict) -> PolicyDocument:
    fields = {k: v for k, v in doc.items() if k in PolicyDocument.model_fields}
    return PolicyDocument.model_validate(fields)


def public_view(doc: dict) -> dict:
    """Metadata safe for approver/admin listings: no storage name/path, no text."""
    return {k: v for k, v in doc.items() if k not in PRIVATE_FIELDS}


def insert_document(doc: dict | PolicyDocument, *, db: Optional[Database] = None) -> dict:
    data = doc.model_dump() if isinstance(doc, PolicyDocument) else dict(doc)
    if not data.get("document_id"):
        raise ValueError("document_id is required")
    data = _to_storage(data)
    data.setdefault("updated_at", data.get("uploaded_at"))
    try:
        _coll(db).insert_one(dict(data))
    except DuplicateKeyError as exc:
        raise DocumentVersionConflict() from exc
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return data


def get_document(document_id: str, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        return _normalize(_coll(db).find_one({"document_id": document_id}, NO_ID))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def find_by_hash(content_hash: str, *, exclude_statuses: Iterable[str] = ("archived", "failed"),
                 db: Optional[Database] = None) -> Optional[dict]:
    query: dict[str, Any] = {"content_hash": content_hash}
    excluded = list(exclude_statuses)
    if excluded:
        query["status"] = {"$nin": excluded}
    try:
        cur = _coll(db).find(query, NO_ID).sort("uploaded_at", ASCENDING).limit(1)
        docs = list(cur)
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return _normalize(docs[0]) if docs else None


def latest_version(logical_key: str, *, db: Optional[Database] = None) -> Optional[dict]:
    """Highest version record for a logical key (any status)."""
    try:
        docs = list(_coll(db).find({"logical_key": logical_key}, NO_ID).sort("version", DESCENDING).limit(1))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return _normalize(docs[0]) if docs else None


def list_documents(*, document_type: Optional[str] = None, vendor_key: Optional[str] = None,
                   status: Optional[str] = None, statuses: Optional[Iterable[str]] = None,
                   logical_key: Optional[str] = None, limit: int = 500,
                   db: Optional[Database] = None) -> list[dict]:
    """Newest first. Returns public metadata only (no storage name)."""
    query: dict[str, Any] = {}
    if document_type:
        query["document_type"] = document_type
    if vendor_key:
        query["vendor_key"] = vendor_key
    if status:
        query["status"] = status
    elif statuses is not None:
        query["status"] = {"$in": list(statuses)}
    if logical_key:
        query["logical_key"] = logical_key
    projection = {"_id": 0, **{f: 0 for f in PRIVATE_FIELDS}}
    try:
        docs = list(_coll(db).find(query, projection).sort("uploaded_at", DESCENDING).limit(max(1, min(limit, 1000))))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return [_normalize(d) for d in docs]


def update_document(document_id: str, fields: dict, *, expected_status: Optional[str] = None,
                    db: Optional[Database] = None) -> Optional[dict]:
    """Set lifecycle fields; returns the updated doc or None if not matched."""
    data = _to_storage(fields)
    data.pop("document_id", None)
    data["updated_at"] = dt.datetime.now(dt.timezone.utc)
    query: dict[str, Any] = {"document_id": document_id}
    if expected_status is not None:
        query["status"] = expected_status
    try:
        res = _coll(db).update_one(query, {"$set": data})
    except PyMongoError as exc:
        raise PersistenceError() from exc
    if res.matched_count == 0:
        return None
    return get_document(document_id, db=db)


def count_documents_by_status(*, db: Optional[Database] = None) -> dict[str, int]:
    try:
        rows = list(_coll(db).aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return {r["_id"]: int(r["n"]) for r in rows if r.get("_id")}
