"""Mongo persistence for candidate onboarding and its audit trail."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.db.errors import PersistenceError
from app.db.mongo import get_database


def _utc(value: Optional[datetime] = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def ensure_indexes() -> None:
    try:
        db = get_database()
        db.vendor_candidates.create_index([("candidate_id", ASCENDING)], unique=True, name="vendor_candidate_id_unique")
        db.vendor_candidates.create_index([("normalized_name", ASCENDING), ("status", ASCENDING)],
                                          name="vendor_candidate_name_status")
        db.vendor_candidate_events.create_index([("event_id", ASCENDING)], unique=True, name="vendor_event_id_unique")
        db.vendor_candidate_events.create_index([("candidate_id", ASCENDING), ("created_at", ASCENDING)],
                                                name="vendor_event_candidate_time")
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: vendor registry indexes could not be initialized.") from exc


def _clean(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    doc.pop("_id", None)
    for key in ("created_at", "updated_at", "reviewed_at"):
        if isinstance(doc.get(key), datetime):
            doc[key] = _utc(doc[key])
    return doc


def list_candidates(*, query: str = "", status: Optional[str] = None, limit: int = 100) -> list[dict]:
    filters: dict[str, Any] = {}
    if status:
        filters["status"] = status
    if query.strip():
        # Regex is escaped so a user query cannot inject Mongo regex operators.
        import re
        filters["$or"] = [
            {"normalized_name": {"$regex": re.escape(query.strip()), "$options": "i"}},
            {"services": {"$regex": re.escape(query.strip()), "$options": "i"}},
            {"regions": {"$regex": re.escape(query.strip()), "$options": "i"}},
        ]
    try:
        docs = get_database().vendor_candidates.find(filters, {"_id": 0}).sort(
            [("updated_at", DESCENDING), ("candidate_id", ASCENDING)]
        ).limit(max(1, min(limit, 200)))
        return [_clean(d) for d in docs]
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: vendor candidates could not be listed.") from exc


def get_candidate(candidate_id: str) -> Optional[dict]:
    try:
        return _clean(get_database().vendor_candidates.find_one({"candidate_id": candidate_id}, {"_id": 0}))
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: vendor candidate could not be read.") from exc


def create_candidate(doc: dict, actor: str) -> dict:
    data = dict(doc)
    data["created_at"] = _utc(data.get("created_at"))
    data["updated_at"] = data["created_at"]
    data["created_by"] = actor
    data["status"] = "pending_review"
    data["normalized_name"] = data["name"].casefold()
    data["reviewed_by"] = None
    data["reviewed_at"] = None
    data["review_reason"] = None
    try:
        get_database().vendor_candidates.insert_one(data)
        append_event(data["candidate_id"], "candidate_submitted", actor,
                     {"status": "pending_review", "source": data["source"]})
        return _clean(data)
    except DuplicateKeyError as exc:
        raise ValueError("Candidate ID already exists.") from exc
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: candidate could not be saved.") from exc


def decide_candidate(candidate_id: str, decision: str, reason: str, actor: str) -> Optional[dict]:
    status = {"approve": "approved", "reject": "rejected", "suspend": "suspended"}[decision]
    now = _utc()
    try:
        coll = get_database().vendor_candidates
        before = coll.find_one({"candidate_id": candidate_id}, {"_id": 0})
        if before is None:
            return None
        if before.get("status") == "suspended" and decision != "approve":
            raise ValueError("Suspended candidates must be explicitly reinstated with approve or left suspended.")
        updated = coll.find_one_and_update(
            {"candidate_id": candidate_id, "status": before.get("status")},
            {"$set": {"status": status, "updated_at": now, "reviewed_by": actor,
                      "reviewed_at": now, "review_reason": reason}},
            return_document=True,
            projection={"_id": 0},
        )
        if updated is None:
            raise ValueError("Candidate changed concurrently; reload and retry.")
        append_event(candidate_id, "candidate_" + decision + "d" if decision != "suspend" else "candidate_suspended",
                     actor, {"from_status": before.get("status"), "to_status": status, "reason": reason})
        return _clean(updated)
    except ValueError:
        raise
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: candidate decision could not be saved.") from exc


def append_event(candidate_id: str, event_type: str, actor: str, details: dict) -> None:
    from uuid import uuid4
    event = {"event_id": "VEND-EVT-" + uuid4().hex, "candidate_id": candidate_id,
             "event_type": event_type, "actor": actor, "details": details, "created_at": _utc()}
    try:
        get_database().vendor_candidate_events.insert_one(event)
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: vendor audit event could not be recorded.") from exc
