"""Login sessions (`sessions` collection). Only sha256(token) is stored, never the raw token."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.db.errors import PersistenceError
from app.db.mongo import get_database

NO_ID = {"_id": 0}


def _resolve_db(db: Optional[Database]) -> Database:
    return db if db is not None else get_database()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def create_session(token_hash: str, username: str, role: str, expires_at: datetime,
                   *, db: Optional[Database] = None) -> None:
    doc = {
        "token_hash": token_hash,
        "user": username,
        "role": role,
        "created_at": datetime.now(timezone.utc),
        "expires_at": _as_utc(expires_at),
    }
    try:
        _resolve_db(db).sessions.insert_one(doc)
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_session(token_hash: str, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        return _resolve_db(db).sessions.find_one({"token_hash": token_hash}, NO_ID)
    except PyMongoError as exc:
        raise PersistenceError() from exc


def delete_session(token_hash: str, *, db: Optional[Database] = None) -> None:
    try:
        _resolve_db(db).sessions.delete_one({"token_hash": token_hash})
    except PyMongoError as exc:
        raise PersistenceError() from exc
