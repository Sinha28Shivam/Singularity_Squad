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


def list_recent_tickets(limit: int = 20, *, db: Optional[Database] = None) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    projection = {"_id": 0, "ticket_id": 1, "created_at": 1, "updated_at": 1, "status": 1,
                  "raw_text": 1, "workflow_result.final_status": 1}
    try:
        cursor = _resolve_db(db).tickets.find({}, projection).sort("updated_at", DESCENDING).limit(limit)
        docs = list(cursor)
    except PyMongoError as exc:
        raise PersistenceError() from exc
    out = []
    for d in docs:
        raw = d.get("raw_text") or ""
        out.append({
            "ticket_id": d.get("ticket_id"),
            "created_at": d.get("created_at"),
            "updated_at": d.get("updated_at"),
            "status": d.get("status"),
            "raw_text": raw[:120],
            "final_status": (d.get("workflow_result") or {}).get("final_status"),
        })
    return out
