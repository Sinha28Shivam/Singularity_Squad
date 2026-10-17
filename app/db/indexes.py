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


def _ensure_policy_rag_b_indexes(db: Database) -> None:
    """Rate-card rows and append-only policy evidence snapshots (Policy RAG)."""
    rates = db.rate_card_entries
    rates.create_index([("rate_id", ASCENDING)], unique=True, name="rate_id_unique")
    rates.create_index([("vendor_key", ASCENDING), ("item_or_service_code", ASCENDING),
                        ("effective_from", ASCENDING), ("effective_to", ASCENDING)],
                       name="rate_vendor_item_validity")
    rates.create_index([("document_id", ASCENDING)], name="rate_document")
    rates.create_index([("verification_status", ASCENDING)], name="rate_verification")
    snaps = db.policy_evidence_snapshots
    snaps.create_index([("snapshot_id", ASCENDING)], unique=True, name="snapshot_id_unique")
    snaps.create_index([("ticket_id", ASCENDING), ("run_count", ASCENDING)], unique=True,
                       name="snapshot_ticket_run_unique")


def _ensure_policy_rag_a_indexes(db: Database) -> None:
    """Policy library documents, chunks and append-only policy events (Policy RAG ingestion)."""
    docs = db.policy_documents
    docs.create_index([("document_id", ASCENDING)], unique=True, name="document_id_unique")
    docs.create_index([("content_hash", ASCENDING)], name="content_hash")
    docs.create_index([("document_type", ASCENDING), ("status", ASCENDING)], name="type_status")
    docs.create_index([("vendor_key", ASCENDING), ("document_type", ASCENDING), ("status", ASCENDING)],
                      name="vendor_type_status")
    docs.create_index([("effective_from", ASCENDING), ("effective_to", ASCENDING)], name="effective_range")
    docs.create_index([("logical_key", ASCENDING), ("version", ASCENDING)], unique=True,
                      name="logical_key_version_unique")
    chunks = db.policy_chunks
    chunks.create_index([("chunk_id", ASCENDING)], unique=True, name="chunk_id_unique")
    chunks.create_index([("document_id", ASCENDING), ("content_hash", ASCENDING), ("page_number", ASCENDING)],
                        name="doc_page")
    chunks.create_index([("document_status", ASCENDING), ("document_type", ASCENDING)], name="chunk_status_type")
    db.policy_events.create_index([("created_at", ASCENDING)], name="policy_event_time")
    db.policy_events.create_index([("document_id", ASCENDING), ("created_at", ASCENDING)],
                                  name="policy_event_document")


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
        _ensure_policy_rag_b_indexes(db)
        _ensure_policy_rag_a_indexes(db)
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: could not initialize indexes. Please try again later.") from exc
