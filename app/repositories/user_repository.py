"""User persistence (`users` collection). Usernames and emails are stored lowercase."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pymongo.database import Database
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.db.errors import PersistenceError
from app.db.mongo import get_database

SCHEMA_VERSION = 1
NO_ID = {"_id": 0}


def _resolve_db(db: Optional[Database]) -> Database:
    return db if db is not None else get_database()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_user(document: dict, *, db: Optional[Database] = None) -> bool:
    """Insert a user. Returns False if the username or email already exists (unique index)."""
    if not document.get("username") or not document.get("email"):
        raise ValueError("username and email are required")
    doc: dict[str, Any] = dict(document)
    doc.pop("_id", None)
    doc["username"] = doc["username"].strip().lower()
    doc["email"] = doc["email"].strip().lower()
    now = _now()
    doc.setdefault("created_at", now)
    doc["updated_at"] = now
    doc["schema_version"] = SCHEMA_VERSION
    try:
        _resolve_db(db).users.insert_one(doc)
    except DuplicateKeyError:
        return False
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return True


def get_user_by_username(username: str, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        return _resolve_db(db).users.find_one({"username": username.strip().lower()}, NO_ID)
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_user_by_email(email: str, *, db: Optional[Database] = None) -> Optional[dict]:
    try:
        return _resolve_db(db).users.find_one({"email": email.strip().lower()}, NO_ID)
    except PyMongoError as exc:
        raise PersistenceError() from exc


def upsert_admin(username: str, name: str, email: str, password_hash: str, salt: str,
                 *, db: Optional[Database] = None) -> None:
    """Create or refresh the admin account (role=admin, password re-hashed every call)."""
    now = _now()
    try:
        _resolve_db(db).users.update_one(
            {"username": username.strip().lower()},
            {
                "$set": {
                    "name": name,
                    "email": email.strip().lower(),
                    "role": "admin",
                    "password_hash": password_hash,
                    "salt": salt,
                    "updated_at": now,
                    "schema_version": SCHEMA_VERSION,
                },
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
    except DuplicateKeyError as exc:
        raise PersistenceError("Admin email is already used by another account.") from exc
    except PyMongoError as exc:
        raise PersistenceError() from exc


def upsert_approver(username: str, name: str, email: str, password_hash: str, salt: str,
                    *, db: Optional[Database] = None) -> bool:
    """Create or refresh a server-configured approver account.

    Only matches documents that are missing or already have role "approver", so an existing user or
    admin account is never converted. Returns False if the username belongs to another role.
    """
    now = _now()
    username = username.strip().lower()
    try:
        _resolve_db(db).users.update_one(
            {"username": username, "role": "approver"},
            {
                "$set": {"name": name, "email": email.strip().lower(), "password_hash": password_hash,
                         "salt": salt, "updated_at": now, "schema_version": SCHEMA_VERSION},
                "$setOnInsert": {"created_at": now, "role": "approver", "username": username},
            },
            upsert=True,
        )
    except DuplicateKeyError:
        return False  # username (other role) or email already taken
    except PyMongoError as exc:
        raise PersistenceError() from exc
    return True
