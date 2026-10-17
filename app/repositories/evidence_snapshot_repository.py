"""Append-only policy evidence snapshots (`policy_evidence_snapshots`), unique per (ticket_id, run_count)."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pymongo import DESCENDING
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.db.errors import PersistenceError
from app.repositories.ticket_repository import NO_ID, _as_utc, _resolve_db


class SnapshotConflictError(PersistenceError):
    def __init__(self) -> None:
        super().__init__("An evidence snapshot for this ticket run already exists.")


def _coll(db: Optional[Database]):
    return _resolve_db(db).policy_evidence_snapshots


def _normalize(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = _as_utc(doc["created_at"])
    return doc


def create_snapshot(snapshot, *, db: Optional[Database] = None) -> dict:
    """Insert a snapshot (EvidenceSnapshot model or JSON dict). Never overwrites an existing one."""
    data = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else dict(snapshot)
    data.pop("_id", None)
    if not data.get("ticket_id") or not data.get("snapshot_id"):
        raise ValueError("snapshot_id and ticket_id are required")
    created = data.get("created_at")
    if isinstance(created, str):
        created = datetime.fromisoformat(created.replace("Z", "+00:00"))
    data["created_at"] = _as_utc(created) if isinstance(created, datetime) else created
    data["run_count"] = int(data.get("run_count") or 0)
    coll = _coll(db)
    try:
        if coll.find_one({"ticket_id": data["ticket_id"], "run_count": data["run_count"]}, {"_id": 1}):
            raise SnapshotConflictError()
        coll.insert_one(data)
    except DuplicateKeyError as exc:
        raise SnapshotConflictError() from exc
    except PyMongoError as exc:
        raise PersistenceError() from exc
    data.pop("_id", None)
    return data


def get_for_run(ticket_id: str, run_count: int, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        return _normalize(_coll(db).find_one({"ticket_id": ticket_id, "run_count": int(run_count)}, NO_ID))
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_latest(ticket_id: str, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        docs = list(_coll(db).find({"ticket_id": ticket_id}, NO_ID)
                    .sort([("run_count", DESCENDING), ("created_at", DESCENDING)]).limit(1))
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return _normalize(docs[0]) if docs else None


def list_for_ticket(ticket_id: str, *, limit: int = 50, db: Optional[Database] = None) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    try:
        cursor = _coll(db).find({"ticket_id": ticket_id}, NO_ID).sort(
            [("run_count", DESCENDING), ("created_at", DESCENDING)]).limit(limit)
        return [_normalize(d) for d in cursor]
    except PyMongoError as exc:
        raise PersistenceError() from exc
