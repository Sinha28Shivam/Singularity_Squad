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


def _ensure_policy_rule_indexes(db: Database) -> None:
    """Phase 3 rule registry: rule versions, immutable policy sets and the append-only rule audit."""
    rules = db.policy_rules
    rules.create_index([("rule_id", ASCENDING), ("rule_version", ASCENDING)], unique=True,
                       name="rule_version_unique")
    rules.create_index([("status", ASCENDING), ("rule_id", ASCENDING)], name="rule_status")
    rules.create_index([("semantic_hash", ASCENDING)], name="rule_semantic_hash")
    db.policy_sets.create_index([("policy_set_version", ASCENDING)], unique=True,
                                name="policy_set_version_unique")
    db.policy_rule_events.create_index([("created_at", ASCENDING)], name="rule_event_time")
    db.policy_rule_events.create_index([("rule_id", ASCENDING), ("created_at", ASCENDING)], name="rule_event_rule")


def _ensure_payment_simulation_indexes(db: Database) -> None:
    """Dummy payment initiator (SIMULATION ONLY): unique indexes make duplicate logical payments impossible."""
    sims = db.payment_simulations
    sims.create_index([("payment_id", ASCENDING)], unique=True, name="payment_id_unique")
    sims.create_index([("idempotency_key_hash", ASCENDING)], unique=True, name="idempotency_key_hash_unique")
    sims.create_index([("intent_key", ASCENDING)], unique=True, name="intent_key_unique")
    sims.create_index([("ticket_id", ASCENDING), ("created_at", DESCENDING)], name="payment_ticket_created")
    sims.create_index([("status", ASCENDING)], name="payment_status")
    events = db.payment_simulation_events
    events.create_index([("event_id", ASCENDING)], unique=True, name="payment_event_id_unique")
    events.create_index([("payment_id", ASCENDING), ("at", ASCENDING)], name="payment_event_payment_at")
    events.create_index([("ticket_id", ASCENDING)], name="payment_event_ticket")


def _ensure_vendor_sourcing_indexes(db: Database) -> None:
    """Vendor candidate registry and append-only onboarding audit events."""
    candidates = db.vendor_candidates
    candidates.create_index([("candidate_id", ASCENDING)], unique=True, name="vendor_candidate_id_unique")
    candidates.create_index([("normalized_name", ASCENDING), ("status", ASCENDING)],
                            name="vendor_candidate_name_status")
    events = db.vendor_candidate_events
    events.create_index([("event_id", ASCENDING)], unique=True, name="vendor_event_id_unique")
    events.create_index([("candidate_id", ASCENDING), ("created_at", ASCENDING)],
                        name="vendor_event_candidate_time")


def _ensure_vendor_rfq_indexes(db: Database) -> None:
    """RFQs, vendor quotations and append-only sourcing events."""
    rfqs = db.vendor_rfqs
    rfqs.create_index([("rfq_id", ASCENDING)], unique=True, name="vendor_rfq_id_unique")
    rfqs.create_index([("request_id", ASCENDING), ("created_at", DESCENDING)], name="vendor_rfq_request_created")
    quotes = db.vendor_quotes
    quotes.create_index([("quote_id", ASCENDING)], unique=True, name="vendor_quote_id_unique")
    quotes.create_index([("rfq_id", ASCENDING), ("vendor_id", ASCENDING)], unique=True,
                        name="vendor_quote_rfq_vendor_unique")
    quotes.create_index([("rfq_id", ASCENDING), ("submitted_at", DESCENDING)], name="vendor_quote_rfq_submitted")
    events = db.vendor_rfq_events
    events.create_index([("event_id", ASCENDING)], unique=True, name="vendor_rfq_event_unique")
    events.create_index([("rfq_id", ASCENDING), ("created_at", ASCENDING)], name="vendor_rfq_event_time")


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
        _ensure_policy_rule_indexes(db)
        _ensure_payment_simulation_indexes(db)
        _ensure_vendor_sourcing_indexes(db)
        _ensure_vendor_rfq_indexes(db)
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: could not initialize indexes. Please try again later.") from exc
