"""Approval request persistence (`approval_requests`). One document per (ticket_id, request_version).

The decision commit is a single conditional ``find_one_and_update`` on one document, so concurrent
approvers cannot both win. Secondary writes (ticket, PO, audit) live elsewhere and are best effort.
"""
from __future__ import annotations

import threading
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Optional

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.db.errors import PersistenceError
from app.repositories.ticket_repository import NO_ID, SCHEMA_VERSION, _as_utc, _resolve_db


class ApprovalConflictError(PersistenceError):
    """Duplicate approval_id / (ticket_id, request_version) / second pending request for a ticket."""

    def __init__(self) -> None:
        super().__init__("An approval request for this ticket version already exists.")


_MOCK_LOCK = threading.Lock()


def _coll(db: Optional[Database]):
    return _resolve_db(db).approval_requests


def _is_mongomock(coll: Any) -> bool:
    return type(coll).__module__.startswith("mongomock")


def _normalize(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    doc.pop("_id", None)
    for key in ("created_at", "expires_at", "decided_at", "updated_at"):
        if isinstance(doc.get(key), datetime):
            doc[key] = _as_utc(doc[key])
    for dec in doc.get("decisions") or []:
        if isinstance(dec.get("decided_at"), datetime):
            dec["decided_at"] = _as_utc(dec["decided_at"])
    return doc


def create_approval(doc: dict, *, db: Optional[Database] = None) -> dict:
    data: dict[str, Any] = dict(doc)
    data.pop("_id", None)
    if not data.get("approval_id") or not data.get("ticket_id"):
        raise ValueError("approval_id and ticket_id are required")
    for key in ("created_at", "expires_at"):
        data[key] = _as_utc(data[key])
    data.setdefault("decisions", [])
    data.setdefault("reconciliation_required", False)
    data["schema_version"] = SCHEMA_VERSION
    coll = _coll(db)
    try:
        # Code-level guard in addition to the partial unique index (not every backend supports it).
        with _MOCK_LOCK if _is_mongomock(coll) else nullcontext():
            if data.get("status") == "pending" and coll.find_one(
                    {"ticket_id": data["ticket_id"], "status": "pending"}, {"_id": 1}):
                raise ApprovalConflictError()
            coll.insert_one(data)
    except DuplicateKeyError as exc:
        raise ApprovalConflictError() from exc
    except PyMongoError as exc:
        raise PersistenceError() from exc
    data.pop("_id", None)
    return data


def get_approval(approval_id: str, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        return _normalize(_coll(db).find_one({"approval_id": approval_id}, NO_ID))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_current_for_ticket(ticket_id: str, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        return _normalize(_coll(db).find_one({"ticket_id": ticket_id}, NO_ID,
                                             sort=[("request_version", DESCENDING)]))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_pending_for_ticket(ticket_id: str, *, db: Optional[Database] = None) -> list[dict]:
    try:
        docs = list(_coll(db).find({"ticket_id": ticket_id, "status": "pending"}, NO_ID)
                    .sort("request_version", ASCENDING))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return [_normalize(d) for d in docs]


def list_for_ticket(ticket_id: str, *, db: Optional[Database] = None) -> list[dict]:
    try:
        docs = list(_coll(db).find({"ticket_id": ticket_id}, NO_ID).sort("request_version", ASCENDING))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return [_normalize(d) for d in docs]


def list_approvals(status: Optional[str] = None, limit: int = 100, *, requester_id: Optional[str] = None,
                   db: Optional[Database] = None) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    query: dict[str, Any] = {}
    if status:
        query["status"] = status
    if requester_id is not None:
        query["requester_id"] = requester_id
    try:
        docs = list(_coll(db).find(query, NO_ID).sort("created_at", DESCENDING).limit(limit))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return [_normalize(d) for d in docs]


def count_approvals_by_status(*, requester_id: Optional[str] = None, db: Optional[Database] = None) -> dict[str, int]:
    pipeline: list[dict] = []
    if requester_id is not None:
        pipeline.append({"$match": {"requester_id": requester_id}})
    pipeline.append({"$group": {"_id": "$status", "n": {"$sum": 1}}})
    try:
        rows = list(_coll(db).aggregate(pipeline))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return {str(r["_id"]): int(r["n"]) for r in rows if r.get("_id") is not None}


def next_version(ticket_id: str, *, db: Optional[Database] = None) -> int:
    try:
        doc = _coll(db).find_one({"ticket_id": ticket_id}, {"_id": 0, "request_version": 1},
                                 sort=[("request_version", DESCENDING)])
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return int(doc["request_version"]) + 1 if doc and doc.get("request_version") else 1


def decide_atomic(approval_id: str, request_version: int, snapshot_hash: str, decision_dict: dict,
                  new_status: str, now: datetime, *, db: Optional[Database] = None) -> Optional[dict]:
    """The authoritative decision commit. Returns the updated document, or None if nothing matched
    (already decided, stale version/hash, or expired)."""
    now = _as_utc(now)
    decision = dict(decision_dict)
    if isinstance(decision.get("decided_at"), datetime):
        decision["decided_at"] = _as_utc(decision["decided_at"])
    coll = _coll(db)
    try:
        # mongomock's find_one_and_update is find-then-update (not atomic); real MongoDB is atomic.
        with _MOCK_LOCK if _is_mongomock(coll) else nullcontext():
            doc = coll.find_one_and_update(
                {"approval_id": approval_id, "status": "pending", "request_version": int(request_version),
                 "workflow_snapshot_hash": snapshot_hash, "expires_at": {"$gt": now}},
                {"$set": {"status": new_status, "decided_at": now, "updated_at": now},
                 "$push": {"decisions": decision}},
                # No projection: mongomock re-reads by _id only when _id is returned. _normalize drops it.
                return_document=ReturnDocument.AFTER,
            )
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return _normalize(doc)


def transition_if_pending(approval_id: str, new_status: str, now: datetime, extra: Optional[dict] = None,
                          *, db: Optional[Database] = None) -> bool:
    """Atomically move a pending request to ``expired``/``superseded``. False if no longer pending."""
    if new_status not in ("expired", "superseded"):
        raise ValueError("transition_if_pending only supports expired/superseded")
    fields: dict[str, Any] = dict(extra or {})
    fields.update({"status": new_status, "updated_at": _as_utc(now)})
    coll = _coll(db)
    try:
        with _MOCK_LOCK if _is_mongomock(coll) else nullcontext():
            res = coll.update_one({"approval_id": approval_id, "status": "pending"}, {"$set": fields})
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return res.modified_count > 0


def set_reconciliation(approval_id: str, flag: bool, *, db: Optional[Database] = None) -> bool:
    try:
        res = _coll(db).update_one({"approval_id": approval_id}, {"$set": {"reconciliation_required": bool(flag)}})
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return res.matched_count > 0


def find_decision_by_key(approval_id: str, idempotency_key: str, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        doc = _coll(db).find_one({"approval_id": approval_id, "decisions.idempotency_key": idempotency_key},
                                 {"_id": 0, "decisions": 1})
    except PyMongoError as exc:
        raise PersistenceError() from exc
    if not doc:
        return None
    for dec in doc.get("decisions") or []:
        if dec.get("idempotency_key") == idempotency_key:
            if isinstance(dec.get("decided_at"), datetime):
                dec["decided_at"] = _as_utc(dec["decided_at"])
            return dec
    return None
