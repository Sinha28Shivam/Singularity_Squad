"""Simulated-payment persistence (SIMULATION ONLY — no money is ever transferred).

Collections:
- ``payment_simulations``: one document per logical payment intent. Unique indexes on ``payment_id``,
  ``idempotency_key_hash`` and ``intent_key`` (``f"{ticket_id}|{invoice_id}"``) make duplicate creation
  impossible even under concurrent requests. Status changes are conditional (compare-and-set) updates.
  Pending audit events live in an ``outbox`` array on the record so a failed audit write is recoverable.
- ``payment_simulation_events``: append-only audit events with a unique, deterministic ``event_id`` so
  re-writing an event during reconciliation is idempotent.
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
from app.payments.schemas import ALLOWED_TRANSITIONS
from app.repositories.ticket_repository import NO_ID, _as_utc, _resolve_db

_MOCK_LOCK = threading.Lock()
_DATE_FIELDS = ("created_at", "updated_at", "at")


class PaymentDuplicateError(PersistenceError):
    """A record with the same payment_id / idempotency key / payment intent already exists."""

    def __init__(self) -> None:
        super().__init__("A simulated payment for this request already exists.")


class IllegalTransitionError(ValueError):
    pass


def _db(db: Optional[Database]) -> Database:
    return _resolve_db(db)


def _is_mongomock(coll: Any) -> bool:
    return type(coll).__module__.startswith("mongomock")


def _guard(coll: Any):
    # mongomock is not atomic across threads (find-then-write); real MongoDB enforces this server-side.
    return _MOCK_LOCK if _is_mongomock(coll) else nullcontext()


def _normalize(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    doc.pop("_id", None)
    for key in _DATE_FIELDS:
        if isinstance(doc.get(key), datetime):
            doc[key] = _as_utc(doc[key])
    for ev in doc.get("outbox") or []:
        if isinstance(ev.get("at"), datetime):
            ev["at"] = _as_utc(ev["at"])
    # Server invariants, regardless of what is stored.
    doc["mode"] = "SIMULATION"
    doc["money_transferred"] = False
    return doc


def check_transition(current: Optional[str], new: str) -> None:
    if new not in ALLOWED_TRANSITIONS.get(current, set()):
        raise IllegalTransitionError(f"Illegal simulated payment transition {current!r} -> {new!r}")


# ---------- payment_simulations ----------
def insert_simulation(doc: dict, *, db: Optional[Database] = None) -> dict:
    data = dict(doc)
    data.pop("_id", None)
    for key in ("payment_id", "idempotency_key_hash", "intent_key"):
        if not data.get(key):
            raise ValueError(f"{key} is required")
    check_transition(None, data.get("status"))
    data["mode"] = "SIMULATION"
    data["money_transferred"] = False
    for key in ("created_at", "updated_at"):
        data[key] = _as_utc(data[key])
    coll = _db(db).payment_simulations
    try:
        with _guard(coll):
            if _is_mongomock(coll) and coll.find_one(
                    {"$or": [{"payment_id": data["payment_id"]},
                             {"idempotency_key_hash": data["idempotency_key_hash"]},
                             {"intent_key": data["intent_key"]}]}, {"_id": 1}):
                raise PaymentDuplicateError()
            coll.insert_one(data)
    except DuplicateKeyError as exc:
        raise PaymentDuplicateError() from exc
    except PyMongoError as exc:
        raise PersistenceError() from exc
    data.pop("_id", None)
    return data


def _find_one(query: dict, db: Optional[Database]) -> Optional[dict]:
    try:
        return _normalize(_db(db).payment_simulations.find_one(query, NO_ID))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_by_payment_id(payment_id: str, *, db: Optional[Database] = None) -> Optional[dict]:
    return _find_one({"payment_id": payment_id}, db)


def get_by_idempotency_hash(key_hash: str, *, db: Optional[Database] = None) -> Optional[dict]:
    return _find_one({"idempotency_key_hash": key_hash}, db)


def get_by_intent_key(intent_key: str, *, db: Optional[Database] = None) -> Optional[dict]:
    return _find_one({"intent_key": intent_key}, db)


def list_simulations(ticket_id: Optional[str] = None, limit: int = 100, *,
                     db: Optional[Database] = None) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    query: dict[str, Any] = {"ticket_id": ticket_id} if ticket_id else {}
    try:
        docs = list(_db(db).payment_simulations.find(query, NO_ID).sort("created_at", DESCENDING).limit(limit))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return [_normalize(d) for d in docs]


def transition_status(payment_id: str, expected: str, new: str, now: datetime, *,
                      set_fields: Optional[dict] = None, inc_fields: Optional[dict] = None,
                      push_outbox: Optional[list[dict]] = None, db: Optional[Database] = None) -> Optional[dict]:
    """Compare-and-set status change validated against ALLOWED_TRANSITIONS. Returns the updated record, or
    None when the record is no longer in ``expected`` (a concurrent writer won)."""
    check_transition(expected, new)
    fields = dict(set_fields or {})
    fields.update({"status": new, "updated_at": _as_utc(now), "audit_complete": False})
    update: dict[str, Any] = {"$set": fields}
    if inc_fields:
        update["$inc"] = dict(inc_fields)
    if push_outbox:
        update["$push"] = {"outbox": {"$each": list(push_outbox)}}
    coll = _db(db).payment_simulations
    try:
        with _guard(coll):
            doc = coll.find_one_and_update({"payment_id": payment_id, "status": expected}, update,
                                           return_document=ReturnDocument.AFTER)
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return _normalize(doc)


def clear_outbox(payment_id: str, event_ids: list[str], *, db: Optional[Database] = None) -> bool:
    """Drop delivered outbox events; mark ``audit_complete`` once nothing is pending. Returns audit_complete."""
    coll = _db(db).payment_simulations
    try:
        with _guard(coll):
            if event_ids:
                coll.update_one({"payment_id": payment_id},
                                {"$pull": {"outbox": {"event_id": {"$in": list(event_ids)}}}})
            doc = coll.find_one({"payment_id": payment_id}, {"_id": 0, "outbox": 1})
            if doc is None:
                return False
            if doc.get("outbox"):
                return False
            coll.update_one({"payment_id": payment_id, "outbox": {"$size": 0}},
                            {"$set": {"audit_complete": True}})
            return True
    except PyMongoError as exc:
        raise PersistenceError() from exc


# ---------- payment_simulation_events ----------
def insert_event(event: dict, *, db: Optional[Database] = None) -> bool:
    """Insert one audit event. Returns False when an event with the same event_id already exists."""
    data = dict(event)
    data.pop("_id", None)
    if not data.get("event_id"):
        raise ValueError("event_id is required")
    data["mode"] = "SIMULATION"
    data["money_transferred"] = False
    if isinstance(data.get("at"), datetime):
        data["at"] = _as_utc(data["at"])
    coll = _db(db).payment_simulation_events
    try:
        with _guard(coll):
            if _is_mongomock(coll) and coll.find_one({"event_id": data["event_id"]}, {"_id": 1}):
                return False
            coll.insert_one(data)
    except DuplicateKeyError:
        return False
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return True


def list_events(payment_id: str, *, db: Optional[Database] = None) -> list[dict]:
    try:
        docs = list(_db(db).payment_simulation_events.find({"payment_id": payment_id}, NO_ID)
                    .sort([("at", ASCENDING), ("seq", ASCENDING)]))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return [_normalize(d) for d in docs]


def list_events_for_ticket(ticket_id: str, limit: int = 200, *, db: Optional[Database] = None) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    try:
        docs = list(_db(db).payment_simulation_events.find({"ticket_id": ticket_id}, NO_ID)
                    .sort("at", ASCENDING).limit(limit))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return [_normalize(d) for d in docs]
