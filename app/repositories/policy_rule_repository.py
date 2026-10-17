"""Policy rule registry persistence (Phase 3).

Collections:
- ``policy_rules``: one immutable document per (rule_id, rule_version); only lifecycle/review fields change,
  always guarded by an expected status (compare-and-set).
- ``policy_sets``: immutable active-policy-set versions (``policy_set_version`` unique).
- ``policy_set_pointer``: a single document ``{_id: "active", version, set_id}`` moved by compare-and-set.
- ``policy_rule_events``: append-only audit of candidate creation, validation, approval, rejection,
  supersession, activation and extraction. Never stores document text, secrets or file paths.

Dates (effective_from/until) are stored as ISO strings because BSON has no date-only type.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Iterable, Optional

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.db.errors import PersistenceError
from app.repositories.ticket_repository import NO_ID, SCHEMA_VERSION, _as_utc, _resolve_db

POINTER_ID = "active"
MAX_RULE_LIMIT = 1000
MAX_EVENT_LIMIT = 500
_DATE_FIELDS = ("effective_from", "effective_until")
_DATETIME_FIELDS = ("created_at", "reviewed_at", "approved_at", "updated_at", "rejected_at", "superseded_at")
RULE_EVENT_TYPES = frozenset({
    "rule_candidate_created", "rule_validated", "rule_validation_failed", "rule_approved", "rule_rejected",
    "rule_superseded", "rule_deactivated", "policy_set_activated", "rule_activation_failed",
    "rule_duplicate_linked", "rule_extraction_completed", "rule_extraction_failed", "rule_bootstrap_completed",
})
_FORBIDDEN_EVENT_FIELDS = frozenset({"text", "content", "path", "storage_path", "storage_name", "data",
                                     "token", "password", "api_key"})


class RuleVersionConflict(PersistenceError):
    def __init__(self) -> None:
        super().__init__("A rule with this id and version already exists; please retry.")


def _rules(db: Optional[Database]):
    return _resolve_db(db).policy_rules


def _sets(db: Optional[Database]):
    return _resolve_db(db).policy_sets


def _pointer(db: Optional[Database]):
    return _resolve_db(db).policy_set_pointer


def _events(db: Optional[Database]):
    return _resolve_db(db).policy_rule_events


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _to_storage(doc: dict) -> dict:
    data: dict[str, Any] = dict(doc)
    data.pop("_id", None)
    for key in _DATE_FIELDS:
        if isinstance(data.get(key), dt.date) and not isinstance(data.get(key), dt.datetime):
            data[key] = data[key].isoformat()
    for key in _DATETIME_FIELDS:
        if isinstance(data.get(key), dt.datetime):
            data[key] = _as_utc(data[key])
    return data


def _normalize(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    doc.pop("_id", None)
    for key in _DATETIME_FIELDS:
        if isinstance(doc.get(key), dt.datetime):
            doc[key] = _as_utc(doc[key])
    return doc


# ---------------------------------------------------------------- rules
def insert_rule(doc: dict, *, db: Optional[Database] = None) -> dict:
    data = _to_storage(doc)
    if not data.get("rule_id") or not data.get("rule_version"):
        raise ValueError("rule_id and rule_version are required")
    data.setdefault("updated_at", data.get("created_at") or _now())
    data.setdefault("storage_schema_version", SCHEMA_VERSION)
    coll = _rules(db)
    try:
        if coll.find_one({"rule_id": data["rule_id"], "rule_version": data["rule_version"]}, {"_id": 1}):
            raise RuleVersionConflict()
        coll.insert_one(dict(data))
    except DuplicateKeyError as exc:
        raise RuleVersionConflict() from exc
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return _normalize(dict(data))


def get_rule(rule_id: str, rule_version: int, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        return _normalize(_rules(db).find_one({"rule_id": rule_id, "rule_version": int(rule_version)}, NO_ID))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def list_rules(*, status: Optional[str] = None, statuses: Optional[Iterable[str]] = None,
               rule_id: Optional[str] = None, semantic_hash: Optional[str] = None, limit: int = 200,
               db: Optional[Database] = None) -> list[dict]:
    """Ordered by (rule_id, rule_version desc)."""
    query: dict[str, Any] = {}
    if status:
        query["status"] = status
    elif statuses is not None:
        query["status"] = {"$in": list(statuses)}
    if rule_id:
        query["rule_id"] = rule_id
    if semantic_hash:
        query["semantic_hash"] = semantic_hash
    limit = max(1, min(int(limit), MAX_RULE_LIMIT))
    try:
        docs = list(_rules(db).find(query, NO_ID).sort([("rule_id", ASCENDING), ("rule_version", DESCENDING)])
                    .limit(limit))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return [_normalize(d) for d in docs]


def latest_rule_version(rule_id: str, *, db: Optional[Database] = None) -> int:
    try:
        docs = list(_rules(db).find({"rule_id": rule_id}, {"_id": 0, "rule_version": 1})
                    .sort("rule_version", DESCENDING).limit(1))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return int(docs[0]["rule_version"]) if docs else 0


def update_rule(rule_id: str, rule_version: int, fields: dict, *,
                expected_statuses: Optional[Iterable[str]] = None, db: Optional[Database] = None) -> Optional[dict]:
    """Compare-and-set lifecycle update. Returns the updated doc, or None when the status guard didn't match."""
    data = _to_storage(fields)
    for key in ("rule_id", "rule_version"):
        data.pop(key, None)
    data["updated_at"] = _now()
    query: dict[str, Any] = {"rule_id": rule_id, "rule_version": int(rule_version)}
    if expected_statuses is not None:
        query["status"] = {"$in": list(expected_statuses)}
    try:
        # No projection: mongomock returns None for find_one_and_update with {"_id": 0}; _normalize drops _id.
        doc = _rules(db).find_one_and_update(query, {"$set": data}, return_document=ReturnDocument.AFTER)
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return _normalize(doc)


def add_duplicate_link(rule_id: str, rule_version: int, link: dict, *, db: Optional[Database] = None) -> None:
    """Record provenance of a duplicate extraction on the existing rule (evidence is never discarded)."""
    try:
        _rules(db).update_one({"rule_id": rule_id, "rule_version": int(rule_version)},
                              {"$push": {"duplicate_links": _to_storage(link)}})
    except PyMongoError as exc:
        raise PersistenceError() from exc


def count_rules_by_status(*, db: Optional[Database] = None) -> dict[str, int]:
    try:
        rows = list(_rules(db).aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return {r["_id"]: int(r["n"]) for r in rows if r.get("_id")}


# ---------------------------------------------------------------- policy sets + pointer
def insert_policy_set(doc: dict, *, db: Optional[Database] = None) -> Optional[dict]:
    """Insert an immutable set version. Returns None if that version number already exists (lost race)."""
    data = dict(doc)
    data.pop("_id", None)
    data.setdefault("set_id", f"pset_{uuid.uuid4().hex[:20]}")
    if isinstance(data.get("created_at"), dt.datetime):
        data["created_at"] = _as_utc(data["created_at"])
    coll = _sets(db)
    try:
        if coll.find_one({"policy_set_version": data["policy_set_version"]}, {"_id": 1}):
            return None
        coll.insert_one(dict(data))
    except DuplicateKeyError:
        return None
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return _normalize(dict(data))


def latest_policy_set_version(*, db: Optional[Database] = None) -> int:
    try:
        docs = list(_sets(db).find({}, {"_id": 0, "policy_set_version": 1})
                    .sort("policy_set_version", DESCENDING).limit(1))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return int(docs[0]["policy_set_version"]) if docs else 0


def delete_rule(rule_id: str, rule_version: int, *, db: Optional[Database] = None) -> None:
    """Compensation only (a candidate whose creation audit failed). Never used for reviewed rules."""
    try:
        _rules(db).delete_one({"rule_id": rule_id, "rule_version": int(rule_version),
                               "status": {"$in": ["candidate", "needs_review"]}})
    except PyMongoError as exc:
        raise PersistenceError() from exc


def delete_policy_set(set_id: str, *, db: Optional[Database] = None) -> None:
    try:
        _sets(db).delete_one({"set_id": set_id})
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_policy_set(policy_set_version: int, *, set_id: Optional[str] = None,
                   db: Optional[Database] = None) -> Optional[dict]:
    query: dict[str, Any] = {"set_id": set_id} if set_id else {"policy_set_version": int(policy_set_version)}
    try:
        return _normalize(_sets(db).find_one(query, NO_ID))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_pointer(*, db: Optional[Database] = None) -> dict:
    """``{"version": int, "set_id": str|None}``; version 0 when no policy set was ever activated."""
    try:
        doc = _pointer(db).find_one({"_id": POINTER_ID})
    except PyMongoError as exc:
        raise PersistenceError() from exc
    if not doc:
        return {"version": 0, "set_id": None}
    return {"version": int(doc.get("version") or 0), "set_id": doc.get("set_id")}


def cas_pointer(expected_version: int, new_version: int, set_id: Optional[str], *,
                expected_set_id: Optional[str] = None, db: Optional[Database] = None) -> bool:
    """Move the active pointer only if it still equals ``expected_version``. True on success."""
    query: dict[str, Any] = {"_id": POINTER_ID, "version": int(expected_version)}
    if expected_set_id is not None:
        query["set_id"] = expected_set_id
    update = {"$set": {"version": int(new_version), "set_id": set_id, "updated_at": _now()}}
    try:
        doc = _pointer(db).find_one_and_update(query, update, upsert=int(expected_version) == 0
                                               and expected_set_id is None,
                                               return_document=ReturnDocument.AFTER)
    except DuplicateKeyError:
        return False
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return bool(doc) and int(doc.get("version", -1)) == int(new_version) and doc.get("set_id") == set_id


# ---------------------------------------------------------------- audit events
def record_event(event_type: str, actor: str, *, db: Optional[Database] = None, **fields: Any) -> dict:
    """Append one audit event. Raises ``PersistenceError`` if Mongo fails (callers decide on rollback)."""
    if event_type not in RULE_EVENT_TYPES:
        raise ValueError(f"unknown rule event type: {event_type}")
    doc: dict[str, Any] = {k: v for k, v in fields.items() if k not in _FORBIDDEN_EVENT_FIELDS and k != "_id"}
    doc.update({"event_id": f"rev_{uuid.uuid4().hex[:20]}", "event_type": event_type,
                "actor": actor or "system", "created_at": _now(), "schema_version": SCHEMA_VERSION})
    try:
        _events(db).insert_one(dict(doc))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return doc


def list_events(*, rule_id: Optional[str] = None, event_type: Optional[str] = None,
                document_id: Optional[str] = None, limit: int = 200, db: Optional[Database] = None) -> list[dict]:
    """Most recent first."""
    query: dict[str, Any] = {}
    if rule_id:
        query["rule_id"] = rule_id
    if event_type:
        query["event_type"] = event_type
    if document_id:
        query["document_id"] = document_id
    limit = max(1, min(int(limit), MAX_EVENT_LIMIT))
    try:
        docs = list(_events(db).find(query, NO_ID).sort("created_at", DESCENDING).limit(limit))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    for d in docs:
        if isinstance(d.get("created_at"), dt.datetime):
            d["created_at"] = _as_utc(d["created_at"])
    return docs
