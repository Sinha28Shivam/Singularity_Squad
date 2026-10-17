"""Ticket persistence. One document per workflow ticket in the `tickets` collection."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pymongo import DESCENDING
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.db.errors import PersistenceError
from app.db.mongo import get_database

SCHEMA_VERSION = 1
NO_ID = {"_id": 0}


def _resolve_db(db: Optional[Database]) -> Database:
    return db if db is not None else get_database()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_ticket(document: dict, *, db: Optional[Database] = None) -> None:
    if not document.get("ticket_id"):
        raise ValueError("ticket_id is required")
    doc: dict[str, Any] = dict(document)
    doc.pop("_id", None)
    now = _now()
    doc["created_at"] = _as_utc(doc["created_at"]) if isinstance(doc.get("created_at"), datetime) else now
    doc["updated_at"] = _as_utc(doc["updated_at"]) if isinstance(doc.get("updated_at"), datetime) else doc["created_at"]
    doc["schema_version"] = SCHEMA_VERSION
    try:
        _resolve_db(db).tickets.insert_one(doc)
    except DuplicateKeyError as exc:
        raise PersistenceError("Ticket ID collision; please retry the request.") from exc
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_ticket(ticket_id: str, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        return _resolve_db(db).tickets.find_one({"ticket_id": ticket_id}, NO_ID)
    except PyMongoError as exc:
        raise PersistenceError() from exc


def update_ticket_result(ticket_id: str, result: dict, status: str, updated_at: datetime,
                         *, db: Optional[Database] = None) -> None:
    try:
        res = _resolve_db(db).tickets.update_one(
            {"ticket_id": ticket_id},
            {"$set": {"workflow_result": result, "status": status, "updated_at": _as_utc(updated_at)}},
        )
    except PyMongoError as exc:
        raise PersistenceError() from exc
    if res.matched_count == 0:
        raise PersistenceError(f"Ticket {ticket_id} not found while saving workflow result.")


def mark_ticket_status(ticket_id: str, status: str, error: Optional[str] = None,
                       *, db: Optional[Database] = None) -> None:
    """Set a ticket status such as "failed"/"persistence_error". Raises PersistenceError; callers may swallow."""
    fields: dict[str, Any] = {"status": status, "updated_at": _now()}
    if error is not None:
        fields["error"] = error
    try:
        _resolve_db(db).tickets.update_one({"ticket_id": ticket_id}, {"$set": fields})
    except PyMongoError as exc:
        raise PersistenceError() from exc


_LIST_PROJECTION = {"_id": 0, "ticket_id": 1, "created_at": 1, "updated_at": 1, "status": 1,
                    "owner_username": 1, "owner_name": 1, "submission": 1, "run_count": 1,
                    "workflow_result.final_status": 1}


def create_submitted_ticket(ticket_id: str, *, owner_username: str, owner_name: str, owner_email: str,
                            submission: dict, raw_text: str, created_at: Optional[datetime] = None,
                            db: Optional[Database] = None) -> dict:
    """Create a portal ticket in status ``submitted``; returns the stored document (without ``_id``)."""
    now = _as_utc(created_at) if created_at else _now()
    doc = {
        "ticket_id": ticket_id,
        "created_at": now,
        "updated_at": now,
        "status": "submitted",
        "owner_username": owner_username,
        "owner_name": owner_name,
        "owner_email": owner_email,
        "submission": submission,
        "raw_text": raw_text,
        "workflow_result": None,
        "notes": [],
        "decision": None,
        "run_count": 0,
        "last_run_by": None,
        "last_run_at": None,
        "error": None,
    }
    create_ticket(doc, db=db)
    doc.pop("_id", None)
    doc["schema_version"] = SCHEMA_VERSION
    return doc


def _find_list(query: dict, limit: int, db: Optional[Database]) -> list[dict]:
    try:
        cursor = (_resolve_db(db).tickets.find(query, _LIST_PROJECTION)
                  .sort("updated_at", DESCENDING).limit(limit))
        return list(cursor)
    except PyMongoError as exc:
        raise PersistenceError() from exc


def list_tickets_for_owner(username: str, limit: int = 50, *, db: Optional[Database] = None) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    return _find_list({"owner_username": username}, limit, db)


def list_tickets(status: Optional[str] = None, limit: int = 100, *, db: Optional[Database] = None) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    return _find_list({"status": status} if status else {}, limit, db)


def count_tickets_by_status(*, db: Optional[Database] = None) -> dict[str, int]:
    try:
        rows = list(_resolve_db(db).tickets.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return {str(r["_id"]): int(r["n"]) for r in rows if r.get("_id") is not None}


def _conditional_update(ticket_id: str, expected: Optional[set[str]], update: dict,
                        db: Optional[Database]) -> bool:
    query: dict[str, Any] = {"ticket_id": ticket_id}
    if expected is not None:
        query["status"] = {"$in": sorted(expected)}
    try:
        res = _resolve_db(db).tickets.update_one(query, update)
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return res.matched_count > 0


def set_status(ticket_id: str, status: str, *, expected: Optional[set[str]] = None,
               error: Optional[str] = None, db: Optional[Database] = None) -> bool:
    """Atomically set the status, optionally only if the current status is in ``expected``."""
    fields: dict[str, Any] = {"status": status, "updated_at": _now()}
    if error is not None:
        fields["error"] = error
    return _conditional_update(ticket_id, expected, {"$set": fields}, db)


def set_invoice_match(ticket_id: str, match: dict, *, expected: Optional[set[str]] = None,
                      db: Optional[Database] = None) -> bool:
    """Store a post-approval invoice match (separate from the immutable workflow_result snapshot)."""
    return _conditional_update(ticket_id, expected,
                               {"$set": {"invoice_match": match, "updated_at": _now()}}, db)


def push_note(ticket_id: str, note: dict, *, db: Optional[Database] = None) -> bool:
    return _conditional_update(ticket_id, None, {"$push": {"notes": note}, "$set": {"updated_at": _now()}}, db)


def set_decision(ticket_id: str, decision: dict, status: str, *, expected: Optional[set[str]] = None,
                 db: Optional[Database] = None) -> bool:
    expected = {"reviewed"} if expected is None else expected
    return _conditional_update(ticket_id, expected,
                               {"$set": {"decision": decision, "status": status, "updated_at": _now()}}, db)


def update_submission(ticket_id: str, submission: dict, raw_text: str, *,
                      expected: Optional[set[str]] = None, db: Optional[Database] = None) -> bool:
    """Replace the submission and move the ticket back to ``submitted`` (only from ``expected``)."""
    expected = {"submitted", "needs_info"} if expected is None else expected
    return _conditional_update(ticket_id, expected, {"$set": {
        "submission": submission, "raw_text": raw_text, "status": "submitted", "updated_at": _now()}}, db)


def save_run_result(ticket_id: str, result: dict, status: str, run_by: str, run_at: datetime,
                    *, db: Optional[Database] = None) -> None:
    """Commit point of a portal run: store the snapshot, new status and run metadata."""
    run_at = _as_utc(run_at)
    try:
        res = _resolve_db(db).tickets.update_one(
            {"ticket_id": ticket_id},
            {"$set": {"workflow_result": result, "status": status, "last_run_by": run_by,
                      "last_run_at": run_at, "updated_at": run_at, "error": None},
             "$inc": {"run_count": 1}},
        )
    except PyMongoError as exc:
        raise PersistenceError() from exc
    if res.matched_count == 0:
        raise PersistenceError(f"Ticket {ticket_id} not found while saving workflow result.")


def list_recent_tickets(limit: int = 20, *, db: Optional[Database] = None) -> list[dict]:
    """Minimal ticket summaries. Request text is deliberately excluded (it may contain sensitive
    details); the full text is only returned by get_ticket()."""
    limit = max(1, min(int(limit), 100))
    projection = {"_id": 0, "ticket_id": 1, "created_at": 1, "updated_at": 1, "status": 1,
                  "workflow_result.final_status": 1}
    try:
        cursor = _resolve_db(db).tickets.find({}, projection).sort("updated_at", DESCENDING).limit(limit)
        docs = list(cursor)
    except PyMongoError as exc:
        raise PersistenceError() from exc
    out = []
    for d in docs:
        out.append({
            "ticket_id": d.get("ticket_id"),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
            "status": d.get("status"),
            "final_status": (d.get("workflow_result") or {}).get("final_status"),
        })
    return out
