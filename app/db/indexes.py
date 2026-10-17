"""Idempotent index creation. Call once at startup, not before every write."""
from __future__ import annotations

from typing import Optional

from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.db.errors import PersistenceError
from app.db.mongo import get_database


def _ensure_approval_indexes(db: Database) -> None:
    coll = db.approval_requests
    coll.create_index([("approval_id", ASCENDING)], unique=True, name="approval_id_unique")
    coll.create_index([("ticket_id", ASCENDING), ("request_version", ASCENDING)], unique=True,
                      name="ticket_version_unique")
    coll.create_index([("status", ASCENDING), ("created_at", DESCENDING)], name="status_created")
    coll.create_index([("requester_id", ASCENDING), ("created_at", DESCENDING)], name="requester_created")
    try:
        # At most one pending request per ticket. Also enforced in code for backends lacking partial indexes.
        coll.create_index([("ticket_id", ASCENDING)], unique=True, name="one_pending_per_ticket",
                          partialFilterExpression={"status": "pending"})
    except (PyMongoError, NotImplementedError, TypeError):
        pass


def ensure_indexes(db: Optional[Database] = None) -> None:
    db = db if db is not None else get_database()
    try:
        db.tickets.create_index([("ticket_id", ASCENDING)], unique=True, name="ticket_id_unique")
        db.tickets.create_index([("updated_at", DESCENDING)], name="updated_at_desc")
        db.tickets.create_index([("owner_username", ASCENDING), ("updated_at", DESCENDING)],
                                name="owner_username_updated")
        db.tickets.create_index([("status", ASCENDING), ("updated_at", DESCENDING)], name="status_updated")
        db.purchase_orders.create_index([("po_id", ASCENDING)], unique=True, name="po_id_unique")
        db.purchase_orders.create_index([("ticket_id", ASCENDING)], name="ticket_id")
        db.agent_logs.create_index(
            [("ticket_id", ASCENDING), ("sequence", ASCENDING)], unique=True, name="ticket_id_sequence_unique"
        )
        db.agent_logs.create_index([("ticket_id", ASCENDING), ("timestamp", ASCENDING)], name="ticket_id_timestamp")
        db.users.create_index([("username", ASCENDING)], unique=True, name="username_unique")
        db.users.create_index([("email", ASCENDING)], unique=True, name="email_unique")
        db.sessions.create_index([("token_hash", ASCENDING)], unique=True, name="token_hash_unique")
        db.sessions.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0, name="expires_at_ttl")
        _ensure_approval_indexes(db)
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: could not initialize indexes. Please try again later.") from exc
