"""Integration tests against a real local MongoDB. Skipped if no server answers within 2s."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from app.db import AuditConflictError, PersistenceError, ensure_indexes
from app.repositories import append_audit_entries, create_ticket, get_audit_entries, get_ticket

URI = "mongodb://localhost:27017"
NOW = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def db():
    client = MongoClient(URI, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000, tz_aware=True)
    try:
        client.admin.command("ping")
    except PyMongoError:
        client.close()
        pytest.skip("Local MongoDB not available")
    name = f"workplaceops_test_{uuid4().hex[:8]}"
    database = client[name]
    ensure_indexes(database)
    yield database
    client.drop_database(name)
    client.close()


def _entry(seq, detail="ok"):
    return {"step": seq, "agent": "Intake", "action": "parse", "status": "info", "detail": detail,
            "evidence": [], "llm_used": False, "timestamp": NOW.isoformat()}


def test_indexes_created(db):
    ensure_indexes(db)  # idempotent
    t = db.tickets.index_information()
    assert t["ticket_id_unique"]["unique"] and "updated_at_desc" in t
    p = db.purchase_orders.index_information()
    assert p["po_id_unique"]["unique"] and "ticket_id" in p
    a = db.agent_logs.index_information()
    assert a["ticket_id_sequence_unique"]["unique"]
    assert a["ticket_id_sequence_unique"]["key"] == [("ticket_id", 1), ("sequence", 1)]
    assert "ticket_id_timestamp" in a


def test_ticket_duplicate_key(db):
    create_ticket({"ticket_id": "REQ-INT-1", "created_at": NOW}, db=db)
    with pytest.raises(PersistenceError, match="collision"):
        create_ticket({"ticket_id": "REQ-INT-1"}, db=db)
    doc = get_ticket("REQ-INT-1", db=db)
    assert "_id" not in doc and doc["created_at"] == NOW


def test_audit_duplicate_behavior(db):
    assert append_audit_entries("REQ-INT-2", [_entry(2), _entry(1)], db=db) == 2
    assert append_audit_entries("REQ-INT-2", [_entry(1), _entry(3)], db=db) == 1
    with pytest.raises(AuditConflictError):
        append_audit_entries("REQ-INT-2", [_entry(2, detail="changed")], db=db)
    logs = get_audit_entries("REQ-INT-2", db=db)
    assert [e["sequence"] for e in logs] == [1, 2, 3]
    assert logs[1]["detail"] == "ok"
    assert logs[0]["timestamp"] == NOW.isoformat()
