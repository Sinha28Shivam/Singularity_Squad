"""Structured rate-card rows (`rate_card_entries`). One document per extracted rate entry.

Rows are written once at extraction time with ``verification_status="needs_review"``; only the
verification fields change afterwards (conditional update so a decided row is never overwritten).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Optional

from pymongo import ASCENDING, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.db.errors import PersistenceError
from app.repositories.ticket_repository import NO_ID, _as_utc, _resolve_db


def _coll(db: Optional[Database]):
    return _resolve_db(db).rate_card_entries


def _to_mongo(entry: dict) -> dict:
    data = dict(entry)
    data.pop("_id", None)
    for key in ("effective_from", "effective_to"):
        v = data.get(key)
        if isinstance(v, (dt.date,)) and not isinstance(v, dt.datetime):
            data[key] = v.isoformat()
    if isinstance(data.get("verified_at"), dt.datetime):
        data["verified_at"] = _as_utc(data["verified_at"])
    return data


def _normalize(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    doc.pop("_id", None)
    if isinstance(doc.get("verified_at"), dt.datetime):
        doc["verified_at"] = _as_utc(doc["verified_at"])
    return doc


def insert_entries(entries: list[dict], *, db: Optional[Database] = None) -> int:
    """Insert new rows; rows whose rate_id already exists are left untouched. Returns rows inserted."""
    inserted = 0
    coll = _coll(db)
    for entry in entries:
        try:
            if coll.find_one({"rate_id": entry.get("rate_id")}, {"_id": 1}):
                continue  # code-level guard in addition to the unique index
            coll.insert_one(_to_mongo(entry))
            inserted += 1
        except DuplicateKeyError:
            continue
        except PyMongoError as exc:
            raise PersistenceError() from exc
    return inserted


def get_entry(rate_id: str, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        return _normalize(_coll(db).find_one({"rate_id": rate_id}, NO_ID))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def list_entries(*, vendor_key: Optional[str] = None, item_or_service_code: Optional[str] = None,
                 verification_status: Optional[str] = None, document_id: Optional[str] = None,
                 document_ids: Optional[list[str]] = None, limit: int = 500,
                 db: Optional[Database] = None) -> list[dict]:
    query: dict[str, Any] = {}
    if vendor_key:
        query["vendor_key"] = vendor_key
    if item_or_service_code:
        query["item_or_service_code"] = item_or_service_code
    if verification_status:
        query["verification_status"] = verification_status
    if document_id:
        query["document_id"] = document_id
    elif document_ids is not None:
        query["document_id"] = {"$in": list(document_ids)}
    limit = max(1, min(int(limit), 2000))
    try:
        cursor = _coll(db).find(query, NO_ID).sort(
            [("vendor_key", ASCENDING), ("source_page", ASCENDING), ("item_or_service_code", ASCENDING)]
        ).limit(limit)
        return [_normalize(d) for d in cursor]
    except PyMongoError as exc:
        raise PersistenceError() from exc


def set_verification(rate_id: str, status: str, actor: str, at: dt.datetime,
                     *, db: Optional[Database] = None) -> Optional[dict]:
    """Move a ``needs_review`` row to ``verified``/``rejected``. Returns the updated row, or None when
    the row does not exist or was already decided."""
    try:
        doc = _coll(db).find_one_and_update(
            {"rate_id": rate_id, "verification_status": "needs_review"},
            {"$set": {"verification_status": status, "verified_by": actor, "verified_at": _as_utc(at)}},
            return_document=ReturnDocument.AFTER)
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return _normalize(doc)


def count_for_document(document_id: str, *, db: Optional[Database] = None) -> int:
    try:
        return int(_coll(db).count_documents({"document_id": document_id}))
    except PyMongoError as exc:
        raise PersistenceError() from exc
