"""Append-only policy library events (`policy_events`). Never stores document text or file paths.

Event types: document_uploaded, document_validation_failed, document_extraction_completed,
document_indexing_completed, document_marked_needs_review, document_version_superseded, document_archived,
document_reprocessed, rate_card_extraction_created, rate_card_verified, policy_evaluation_completed,
policy_conflict_detected, rule_engine_evaluated.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pymongo import DESCENDING
from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.db.errors import PersistenceError
from app.repositories.ticket_repository import NO_ID, SCHEMA_VERSION, _as_utc, _resolve_db

logger = logging.getLogger(__name__)

EVENT_TYPES = frozenset({
    "document_uploaded", "document_validation_failed", "document_extraction_completed",
    "document_indexing_completed", "document_marked_needs_review", "document_version_superseded",
    "document_archived", "document_reprocessed", "rate_card_extraction_created", "rate_card_verified",
    "policy_evaluation_completed", "policy_conflict_detected", "rule_engine_evaluated",
})
# Fields that could carry document contents or server paths are dropped defensively.
_FORBIDDEN_FIELDS = frozenset({"text", "excerpt", "content", "path", "storage_path", "storage_name", "data"})
MAX_EVENT_LIMIT = 500


def record(event_type: str, actor: str, *, db: Optional[Database] = None, **fields: Any) -> dict:
    """Insert one event and return it (without ``_id``). Raises ``PersistenceError`` if Mongo fails."""
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown policy event type: {event_type}")
    doc: dict[str, Any] = {k: v for k, v in fields.items() if k not in _FORBIDDEN_FIELDS and k != "_id"}
    doc.update({
        "event_id": f"pev_{uuid.uuid4().hex[:20]}",
        "event_type": event_type,
        "actor": actor or "system",
        "created_at": datetime.now(timezone.utc),
        "schema_version": SCHEMA_VERSION,
    })
    try:
        _resolve_db(db).policy_events.insert_one(dict(doc))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return doc


def record_safely(event_type: str, actor: str, *, db: Optional[Database] = None, **fields: Any) -> Optional[dict]:
    """Like ``record`` but never raises (used for best-effort ingestion progress events)."""
    try:
        return record(event_type, actor, db=db, **fields)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record policy event %s (%s).", event_type, type(exc).__name__)
        return None


def list_events(*, document_id: Optional[str] = None, event_type: Optional[str] = None,
                ticket_id: Optional[str] = None, limit: int = 200, db: Optional[Database] = None) -> list[dict]:
    """Most recent first."""
    query: dict[str, Any] = {}
    if document_id:
        query["document_id"] = document_id
    if event_type:
        query["event_type"] = event_type
    if ticket_id:
        query["ticket_id"] = ticket_id
    limit = max(1, min(int(limit), MAX_EVENT_LIMIT))
    try:
        docs = list(_resolve_db(db).policy_events.find(query, NO_ID).sort("created_at", DESCENDING).limit(limit))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    for d in docs:
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = _as_utc(d["created_at"])
    return docs
