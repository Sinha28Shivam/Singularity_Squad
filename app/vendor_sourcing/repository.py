"""Mongo persistence for candidate onboarding and its audit trail.

Consistency model (single-document outbox, no multi-document transactions required):

* Every state change (create / decide) is ONE write to the candidate document that sets the new state
  AND appends the matching audit event to ``pending_events`` (with a deterministic ``event_id`` of
  ``f"{candidate_id}:{seq}"``). State and its audit intent therefore can never diverge.
* After the write, pending events are delivered to ``vendor_candidate_events`` with an idempotent upsert on
  ``event_id`` and then pulled from the document. If delivery fails the state is still committed: callers
  receive the state with ``audit_pending: true`` (never a 503 for a committed write) and
  :func:`repair_pending_events` (also run best-effort on reads) delivers the event exactly once later.
* Duplicate active names are prevented by a unique *partial* index on ``normalized_name`` where
  ``active`` is true (``active`` is maintained on every write). Under mongomock (tests) a process lock
  serializes check-and-write, mirroring ``approval_repository``.
"""
from __future__ import annotations

import re
import threading
from contextlib import nullcontext
from datetime import date, datetime, timezone
from typing import Any, Optional

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.db.errors import PersistenceError
from app.db.mongo import get_database
from app.vendor_sourcing.schemas import ACTIVE_STATUSES, MAX_PAGE_LIMIT, MAX_PAGE_OFFSET

try:  # Agent B's counters; never let metrics break the registry.
    from app.vendor_sourcing import metrics as _metrics

    def _inc(name: str, label: Optional[str] = None) -> None:
        try:
            _metrics.inc(name, label)
        except Exception:  # pragma: no cover
            pass
except Exception:  # pragma: no cover - module missing
    def _inc(name: str, label: Optional[str] = None) -> None:
        return None

_MOCK_LOCK = threading.Lock()

ALLOWED_TRANSITIONS = {
    "pending_review": {"approve", "reject", "suspend"},
    "approved": {"suspend"},
    "rejected": set(),
    "suspended": {"approve"},
}
DECISION_STATUS = {"approve": "approved", "reject": "rejected", "suspend": "suspended"}
DECISION_EVENT = {"approve": "candidate_approved", "reject": "candidate_rejected", "suspend": "candidate_suspended"}
_INTERNAL_FIELDS = ("pending_events", "event_seq", "version", "active")
ACTIVE_NAME_INDEX = "vendor_candidate_active_name_unique"


def _utc(value: Optional[datetime] = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _is_mongomock(coll: Any) -> bool:
    return type(coll).__module__.startswith("mongomock")


def _lock_for(coll: Any):
    return _MOCK_LOCK if _is_mongomock(coll) else nullcontext()


def _candidates():
    return get_database().vendor_candidates


def _events():
    return get_database().vendor_candidate_events


def normalize_name(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def ensure_indexes() -> None:
    """Create vendor-candidate indexes (same definitions as ``app.db.indexes``)."""
    from app.db.indexes import _ensure_vendor_sourcing_indexes
    try:
        _ensure_vendor_sourcing_indexes(get_database())
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: vendor registry indexes could not be initialized.") from exc


def _clean(doc: Optional[dict]) -> Optional[dict]:
    """Public view: strip ``_id`` and outbox internals, normalize datetimes, expose ``audit_pending``."""
    if doc is None:
        return None
    doc.pop("_id", None)
    doc["audit_pending"] = bool(doc.get("pending_events"))
    for key in _INTERNAL_FIELDS:
        doc.pop(key, None)
    for key in ("created_at", "updated_at", "reviewed_at"):
        if isinstance(doc.get(key), datetime):
            doc[key] = _utc(doc[key])
    for entry in doc.get("status_history") or []:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = _utc(entry["at"])
    doc.setdefault("compliance_evidence", [])
    doc.setdefault("status_history", [])
    return doc


def _event(candidate_id: str, seq: int, event_type: str, action: str, actor: str, *,
           from_status: Optional[str], to_status: str, reason: Optional[str], at: datetime,
           correlation_id: Optional[str], details: dict) -> dict:
    """Structured audit record (plan §18): actor, action, entity, previous/new state, reason, time, correlation."""
    return {
        "event_id": f"{candidate_id}:{seq}", "seq": seq, "candidate_id": candidate_id,
        "event_type": event_type, "action": action, "actor": actor,
        "entity_type": "vendor_candidate", "entity_id": candidate_id,
        "from_status": from_status, "to_status": to_status, "reason": reason,
        "created_at": at, "correlation_id": correlation_id, "details": details,
    }


def _history(from_status: Optional[str], to_status: str, actor: str, reason: Optional[str], at: datetime,
             correlation_id: Optional[str]) -> dict:
    return {"from_status": from_status, "to_status": to_status, "actor": actor, "reason": reason,
            "at": at, "correlation_id": correlation_id}


def _insert_event(event: dict) -> None:
    """Idempotent delivery: upsert keyed by the deterministic event_id (re-delivery is a no-op)."""
    _events().update_one({"event_id": event["event_id"]}, {"$setOnInsert": event}, upsert=True)


def _deliver_pending(candidate_id: str, pending: Optional[list] = None) -> bool:
    """Deliver queued events for one candidate. Returns True when nothing remains pending."""
    coll = _candidates()
    try:
        if pending is None:
            doc = coll.find_one({"candidate_id": candidate_id}, {"_id": 0, "pending_events": 1})
            pending = (doc or {}).get("pending_events") or []
        if not pending:
            return True
        delivered: list[str] = []
        for event in sorted(pending, key=lambda e: e.get("seq", 0)):
            try:
                _insert_event(dict(event))
            except DuplicateKeyError:
                pass  # already delivered by a concurrent repairer
            delivered.append(event["event_id"])
        coll.update_one({"candidate_id": candidate_id},
                        {"$pull": {"pending_events": {"event_id": {"$in": delivered}}}})
        return True
    except PyMongoError:
        _inc("persistence_failures", "vendor_candidate_audit")
        return False


def repair_pending_events(limit: int = 500) -> dict:
    """Deliver every queued candidate audit event. Idempotent; safe to run concurrently or repeatedly."""
    limit = max(1, min(int(limit), 5000))
    try:
        docs = list(_candidates().find({"pending_events.0": {"$exists": True}},
                                       {"_id": 0, "candidate_id": 1, "pending_events": 1}).limit(limit))
    except PyMongoError as exc:
        _inc("persistence_failures", "vendor_candidates")
        raise PersistenceError("Database unavailable: vendor audit repair could not run.") from exc
    repaired = failed = events = 0
    for doc in docs:
        n = len(doc.get("pending_events") or [])
        if _deliver_pending(doc["candidate_id"], doc.get("pending_events")):
            repaired += 1
            events += n
        else:
            failed += 1
    return {"scanned": len(docs), "repaired_candidates": repaired, "delivered_events": events,
            "failed_candidates": failed}


def _repair_on_read(doc: Optional[dict]) -> Optional[dict]:
    if doc and doc.get("pending_events"):
        if _deliver_pending(doc["candidate_id"], doc["pending_events"]):
            doc["pending_events"] = []
    return doc


def _filters(query: str = "", status: Optional[str] = None, service: Optional[str] = None) -> dict:
    filters: dict[str, Any] = {}
    if status:
        filters["status"] = status
    if service and service.strip():
        filters["services"] = {"$regex": "^" + re.escape(" ".join(service.split())) + "$", "$options": "i"}
    if query.strip():
        # Regex is escaped so a user query cannot inject Mongo regex operators.
        q = re.escape(query.strip())
        filters["$or"] = [
            {"normalized_name": {"$regex": q, "$options": "i"}},
            {"services": {"$regex": q, "$options": "i"}},
            {"regions": {"$regex": q, "$options": "i"}},
        ]
    return filters


def _bounds(limit: int, offset: int) -> tuple[int, int]:
    return max(1, min(int(limit), MAX_PAGE_LIMIT)), max(0, min(int(offset), MAX_PAGE_OFFSET))


def list_candidates(*, query: str = "", status: Optional[str] = None, limit: int = 100, offset: int = 0,
                    service: Optional[str] = None) -> list[dict]:
    limit, offset = _bounds(limit, offset)
    try:
        docs = list(_candidates().find(_filters(query, status, service)).sort(
            [("updated_at", DESCENDING), ("candidate_id", ASCENDING)]
        ).skip(offset).limit(limit))
    except PyMongoError as exc:
        _inc("persistence_failures", "vendor_candidates")
        raise PersistenceError("Database unavailable: vendor candidates could not be listed.") from exc
    return [_clean(_repair_on_read(d)) for d in docs]


def page_candidates(*, query: str = "", status: Optional[str] = None, service: Optional[str] = None,
                    limit: int = 50, offset: int = 0) -> dict:
    limit, offset = _bounds(limit, offset)
    items = list_candidates(query=query, status=status, service=service, limit=limit, offset=offset)
    try:
        total = _candidates().count_documents(_filters(query, status, service))
    except PyMongoError as exc:
        _inc("persistence_failures", "vendor_candidates")
        raise PersistenceError("Database unavailable: vendor candidates could not be counted.") from exc
    next_offset = offset + len(items)
    return {"items": items, "total": total, "limit": limit, "offset": offset,
            "next_offset": next_offset if next_offset < total and next_offset <= MAX_PAGE_OFFSET else None}


def get_candidate(candidate_id: str) -> Optional[dict]:
    try:
        doc = _candidates().find_one({"candidate_id": candidate_id})
    except PyMongoError as exc:
        _inc("persistence_failures", "vendor_candidates")
        raise PersistenceError("Database unavailable: vendor candidate could not be read.") from exc
    return _clean(_repair_on_read(doc))


def list_events(candidate_id: str, limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    try:
        docs = list(_events().find({"candidate_id": candidate_id}, {"_id": 0}).sort(
            [("created_at", ASCENDING), ("seq", ASCENDING)]).limit(limit))
    except PyMongoError as exc:
        _inc("persistence_failures", "vendor_candidates")
        raise PersistenceError("Database unavailable: vendor audit events could not be read.") from exc
    for d in docs:
        if isinstance(d.get("created_at"), datetime):
            d["created_at"] = _utc(d["created_at"])
    return docs


def _evidence_doc(items: list) -> list[dict]:
    out = []
    for item in items or []:
        expires = item.get("expires_on")
        if isinstance(expires, date):
            expires = expires.isoformat()  # BSON has no date-only type
        out.append({"document_type": item["document_type"], "reference": item["reference"],
                    "expires_on": expires, "verified": False, "verified_by": None, "verified_at": None})
    return out


def _is_name_conflict(exc: DuplicateKeyError, coll: Any, data: dict) -> bool:
    details = getattr(exc, "details", None) or {}
    pattern = details.get("keyPattern") or {}
    if pattern:
        return "normalized_name" in pattern
    text = str(exc)
    if "normalized_name" in text or ACTIVE_NAME_INDEX in text:
        return True
    try:  # backend gave no key details (e.g. mongomock): classify by lookup
        return coll.find_one({"candidate_id": data["candidate_id"]}, {"_id": 1}) is None
    except PyMongoError:
        return True


def create_candidate(doc: dict, actor: str, correlation_id: Optional[str] = None) -> dict:
    data = dict(doc)
    now = _utc(data.get("created_at"))
    candidate_id = data["candidate_id"]
    data.update({
        "created_at": now, "updated_at": now, "created_by": actor,
        "status": "pending_review", "active": True, "normalized_name": normalize_name(data["name"]),
        "reviewed_by": None, "reviewed_at": None, "review_reason": None,
        "compliance_evidence": _evidence_doc(data.get("compliance_evidence") or []),
        "status_history": [_history(None, "pending_review", actor, None, now, correlation_id)],
        "event_seq": 1, "version": 1,
    })
    data["pending_events"] = [_event(
        candidate_id, 1, "candidate_submitted", "submit", actor, from_status=None, to_status="pending_review",
        reason=None, at=now, correlation_id=correlation_id,
        details={"status": "pending_review", "source": data.get("source"),
                 "evidence_count": len(data["compliance_evidence"])})]
    coll = _candidates()
    try:
        with _lock_for(coll):
            # Code-level guard in addition to the unique partial index (fast path + backends without it).
            if coll.find_one({"normalized_name": data["normalized_name"],
                              "status": {"$in": list(ACTIVE_STATUSES)}}, {"_id": 1}):
                raise ValueError("A candidate with this name is already pending, approved, or suspended.")
            coll.insert_one(data)
    except DuplicateKeyError as exc:
        if _is_name_conflict(exc, coll, data):
            raise ValueError("A candidate with this name is already pending, approved, or suspended.") from exc
        raise ValueError("Candidate ID already exists.") from exc
    except PyMongoError as exc:
        _inc("persistence_failures", "vendor_candidates")
        raise PersistenceError("Database unavailable: candidate could not be saved.") from exc
    _inc("candidates_submitted", data.get("source"))
    if _deliver_pending(candidate_id, data["pending_events"]):
        data["pending_events"] = []
    else:
        _inc("candidate_audit_pending", "submit")
    return _clean(data)


def decide_candidate(candidate_id: str, decision: str, reason: str, actor: str,
                     correlation_id: Optional[str] = None) -> Optional[dict]:
    status = DECISION_STATUS[decision]
    now = _utc()
    coll = _candidates()
    try:
        with _lock_for(coll):
            before = coll.find_one({"candidate_id": candidate_id})
            if before is None:
                return None
            current = before.get("status")
            if decision not in ALLOWED_TRANSITIONS.get(current, set()):
                raise ValueError(f"Decision '{decision}' is not allowed for candidate status '{current}'.")
            seq = int(before.get("event_seq") or 0) + 1
            event = _event(candidate_id, seq, DECISION_EVENT[decision], decision, actor,
                           from_status=current, to_status=status, reason=reason, at=now,
                           correlation_id=correlation_id,
                           details={"from_status": current, "to_status": status, "reason": reason})
            # Compare-and-set on status AND version: concurrent reviewers cannot both win.
            # NOTE: no projection here (mongomock returns None for projected find_one_and_update).
            updated = coll.find_one_and_update(
                {"candidate_id": candidate_id, "status": current, "version": before.get("version")},
                {"$set": {"status": status, "active": status in ACTIVE_STATUSES, "updated_at": now,
                          "reviewed_by": actor, "reviewed_at": now, "review_reason": reason,
                          "event_seq": seq},
                 "$inc": {"version": 1},
                 "$push": {"status_history": _history(current, status, actor, reason, now, correlation_id),
                           "pending_events": event}},
                return_document=ReturnDocument.AFTER,
            )
            if updated is None:
                raise ValueError("Candidate changed concurrently; reload and retry.")
    except ValueError:
        raise
    except PyMongoError as exc:
        _inc("persistence_failures", "vendor_candidates")
        raise PersistenceError("Database unavailable: candidate decision could not be saved.") from exc
    _inc("decisions_by_type", decision)
    if _deliver_pending(candidate_id, updated.get("pending_events")):
        updated["pending_events"] = []
    else:
        _inc("candidate_audit_pending", decision)
    return _clean(updated)


def append_event(candidate_id: str, event_type: str, actor: str, details: dict) -> None:
    """Legacy direct append (kept for compatibility). Prefer the outbox paths above."""
    from uuid import uuid4
    event = {"event_id": "VEND-EVT-" + uuid4().hex, "candidate_id": candidate_id,
             "event_type": event_type, "action": event_type, "actor": actor,
             "entity_type": "vendor_candidate", "entity_id": candidate_id,
             "from_status": details.get("from_status"), "to_status": details.get("to_status"),
             "reason": details.get("reason"), "correlation_id": details.get("correlation_id"),
             "details": details, "created_at": _utc()}
    try:
        _insert_event(event)
    except PyMongoError as exc:
        _inc("persistence_failures", "vendor_candidate_audit")
        raise PersistenceError("Database unavailable: vendor audit event could not be recorded.") from exc
