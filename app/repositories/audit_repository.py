"""Audit trail persistence (`agent_logs`), one document per AuditEntry."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Union

from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database
from pymongo.errors import BulkWriteError, PyMongoError

from app.db.errors import AuditConflictError, PersistenceError
from app.repositories.ticket_repository import NO_ID, SCHEMA_VERSION, _as_utc, _now, _resolve_db
from app.schemas import AuditEntry

MAX_AUDIT_LIMIT = 500
_COMPARED_FIELDS = ("agent", "action", "status", "detail", "evidence", "llm_used")
_DUPLICATE_KEY = 11000


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, str) and value:
        try:
            return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            pass
    return _now()


def _to_doc(ticket_id: str, entry: Union[AuditEntry, dict]) -> dict:
    data = entry.model_dump(mode="json") if isinstance(entry, AuditEntry) else dict(entry)
    sequence = data.get("step", data.get("sequence"))
    if sequence is None:
        raise ValueError("Audit entry requires a step/sequence number")
    evidence = [e.model_dump(mode="json") if hasattr(e, "model_dump") else dict(e) for e in data.get("evidence") or []]
    return {
        "ticket_id": ticket_id,
        "sequence": int(sequence),
        "agent": data.get("agent"),
        "action": data.get("action"),
        "status": data.get("status"),
        "detail": data.get("detail"),
        "evidence": evidence,
        "llm_used": bool(data.get("llm_used", False)),
        "timestamp": _parse_ts(data.get("timestamp")),
        "schema_version": SCHEMA_VERSION,
    }


def _same_content(a: dict, b: dict) -> bool:
    return all(a.get(f) == b.get(f) for f in _COMPARED_FIELDS)


def append_audit_entries(ticket_id: str, entries: list[Union[AuditEntry, dict]],
                         *, db: Optional[Database] = None) -> int:
    """Insert audit entries; returns the number newly inserted.

    Retry policy, enforced by the unique (ticket_id, sequence) index:
    - If an event with the same (ticket_id, sequence) exists with identical content
      (agent, action, status, detail, evidence, llm_used), the new one is ignored (idempotent retry).
    - If it exists with different content, AuditConflictError is raised.
    - Existing events are never overwritten.
    """
    docs = [_to_doc(ticket_id, e) for e in entries]
    if not docs:
        return 0
    coll = _resolve_db(db).agent_logs
    try:
        return len(coll.insert_many(docs, ordered=False).inserted_ids)
    except BulkWriteError as exc:
        details = exc.details or {}
        write_errors = details.get("writeErrors", [])
        if any(err.get("code") != _DUPLICATE_KEY for err in write_errors):
            raise PersistenceError() from exc
        try:
            for err in write_errors:
                doc = docs[err["index"]]
                existing = coll.find_one({"ticket_id": ticket_id, "sequence": doc["sequence"]}, NO_ID)
                if existing is None or not _same_content(existing, doc):
                    raise AuditConflictError(ticket_id, doc["sequence"])
        except PyMongoError as inner:
            raise PersistenceError() from inner
        return int(details.get("nInserted", len(docs) - len(write_errors)))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_last_sequence(ticket_id: str, *, db: Optional[Database] = None) -> int:
    """Highest stored audit sequence for a ticket (0 if none)."""
    try:
        doc = _resolve_db(db).agent_logs.find_one({"ticket_id": ticket_id}, {"_id": 0, "sequence": 1},
                                                  sort=[("sequence", DESCENDING)])
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return int(doc["sequence"]) if doc and doc.get("sequence") is not None else 0


def get_audit_entries(ticket_id: str, limit: int = MAX_AUDIT_LIMIT, *, db: Optional[Database] = None) -> list[dict]:
    limit = max(1, min(int(limit), MAX_AUDIT_LIMIT))
    try:
        docs = list(_resolve_db(db).agent_logs.find({"ticket_id": ticket_id}, NO_ID)
                    .sort("sequence", ASCENDING).limit(limit))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    for d in docs:
        ts = d.get("timestamp")
        if isinstance(ts, datetime):
            d["timestamp"] = _as_utc(ts).isoformat()
    return docs
