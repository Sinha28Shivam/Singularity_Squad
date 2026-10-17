"""Shared MongoDB client management. Never logs or returns the connection URI."""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, Optional

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.db.errors import PersistenceError

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_URI = "mongodb://localhost:27017/"
DEFAULT_DATABASE = "workplaceops"

_client: Optional[Any] = None
_lock = threading.Lock()


def persistence_mode() -> str:
    mode = os.getenv("WORKPLACEOPS_PERSISTENCE", "required").strip().lower()
    return "disabled" if mode == "disabled" else "required"


def get_client() -> MongoClient:
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                _client = MongoClient(
                    os.getenv("MONGODB_URI", DEFAULT_URI),
                    serverSelectionTimeoutMS=5000,
                    connectTimeoutMS=5000,
                    tz_aware=True,
                    appname="workplaceops-ai",
                )
    return _client


def set_client(client: Any) -> None:
    """Test hook: inject a client (e.g. mongomock.MongoClient(tz_aware=True))."""
    global _client
    with _lock:
        _client = client


def close_client() -> None:
    global _client
    with _lock:
        client, _client = _client, None
    if client is not None:
        try:
            client.close()
        except Exception:  # pragma: no cover - best effort
            logger.warning("Error while closing MongoDB client", exc_info=False)


def get_database() -> Database:
    return get_client()[os.getenv("MONGODB_DATABASE", DEFAULT_DATABASE)]


def ping_database() -> bool:
    try:
        get_client().admin.command("ping")
    except PyMongoError as exc:
        logger.warning("MongoDB ping failed: %s", type(exc).__name__)
        raise PersistenceError() from exc
    return True


def db_status() -> dict:
    mode = persistence_mode()
    if mode == "disabled":
        return {"database": "disabled", "persistence": mode}
    try:
        ping_database()
        state = "connected"
    except Exception:
        state = "unavailable"
    return {"database": state, "persistence": mode}
