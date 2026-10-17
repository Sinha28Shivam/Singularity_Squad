import os

os.environ["WORKPLACEOPS_OFFLINE"] = "1"

from datetime import date, datetime, timedelta, timezone
from unittest import mock

import mongomock
import pytest
from pymongo.errors import ServerSelectionTimeoutError

from app.data_loader import load_json
from app.db import AuditConflictError, PersistenceError, close_client, ensure_indexes, get_database
from app.db.mongo import set_client
from app.orchestrator import run_workflow
from app.repositories import (
    append_audit_entries,
    create_ticket,
    get_audit_entries,
    get_draft_pos_for_ticket,
    get_ticket,
    list_recent_tickets,
    mark_ticket_status,
    update_ticket_result,
    upsert_draft_po,
)
from app.schemas import AuditEntry, Evidence

TODAY = date(2026, 10, 17)
NOW = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    set_client(mongomock.MongoClient(tz_aware=True))
    database = get_database()
    ensure_indexes(database)
    yield database
    close_client()


@pytest.fixture(scope="module")
def workflow_result():
    return run_workflow(load_json("sample_requests.json")["happy_path"], TODAY, request_id="REQ-TEST-1")


def _entry(step, detail="ok", **kw):
    return AuditEntry(step=step, agent="Intake", action="parse", status="info", detail=detail,
                      evidence=[Evidence(source="calc", detail="x")], timestamp=NOW.isoformat(), **kw)


def test_full_workflow_result_round_trips(db, workflow_result):
    create_ticket({"ticket_id": "REQ-TEST-1", "status": "received", "raw_text": "lunch", "created_at": NOW}, db=db)
    payload = workflow_result.model_dump(mode="json")
    update_ticket_result("REQ-TEST-1", payload, payload["final_status"], NOW + timedelta(seconds=2), db=db)
    doc = get_ticket("REQ-TEST-1", db=db)
    assert "_id" not in doc
    assert doc["schema_version"] == 1
    assert doc["workflow_result"] == payload
    assert doc["updated_at"].tzinfo is not None
    assert doc["workflow_result"]["procurement"]["po"]["event_date"] == payload["procurement"]["po"]["event_date"]

    upsert_draft_po("REQ-TEST-1", workflow_result.procurement.po, NOW, db=db)
    pos = get_draft_pos_for_ticket("REQ-TEST-1", db=db)
    assert len(pos) == 1 and pos[0]["total"] == workflow_result.procurement.po.total and "_id" not in pos[0]

    n = append_audit_entries("REQ-TEST-1", workflow_result.audit_trail, db=db)
    assert n == len(workflow_result.audit_trail)
    logs = get_audit_entries("REQ-TEST-1", db=db)
    assert [e["sequence"] for e in logs] == [a.step for a in workflow_result.audit_trail]
    assert all(isinstance(e["timestamp"], str) and "_id" not in e for e in logs)


def test_unknown_ticket_returns_none(db):
    assert get_ticket("REQ-NOPE", db=db) is None


def test_update_unknown_ticket_raises(db):
    with pytest.raises(PersistenceError):
        update_ticket_result("REQ-NOPE", {}, "pass", NOW, db=db)


def test_create_ticket_requires_id_and_is_unique(db):
    with pytest.raises(ValueError):
        create_ticket({"raw_text": "x"}, db=db)
    create_ticket({"ticket_id": "REQ-A"}, db=db)
    with pytest.raises(PersistenceError, match="collision"):
        create_ticket({"ticket_id": "REQ-A"}, db=db)


def test_mark_ticket_status(db):
    create_ticket({"ticket_id": "REQ-A"}, db=db)
    mark_ticket_status("REQ-A", "persistence_error", "audit write failed", db=db)
    doc = get_ticket("REQ-A", db=db)
    assert doc["status"] == "persistence_error" and doc["error"] == "audit write failed"


def test_audit_sorted_by_sequence_when_inserted_out_of_order(db):
    append_audit_entries("REQ-A", [_entry(3), _entry(1)], db=db)
    append_audit_entries("REQ-A", [_entry(2)], db=db)
    assert [e["sequence"] for e in get_audit_entries("REQ-A", db=db)] == [1, 2, 3]


def test_duplicate_identical_audit_ignored(db):
    assert append_audit_entries("REQ-A", [_entry(1), _entry(2)], db=db) == 2
    assert append_audit_entries("REQ-A", [_entry(1), _entry(2), _entry(3)], db=db) == 1
    assert len(get_audit_entries("REQ-A", db=db)) == 3


def test_conflicting_audit_raises(db):
    append_audit_entries("REQ-A", [_entry(1)], db=db)
    with pytest.raises(AuditConflictError):
        append_audit_entries("REQ-A", [_entry(1, detail="different")], db=db)
    assert get_audit_entries("REQ-A", db=db)[0]["detail"] == "ok"


def test_audit_limit_clamped(db):
    append_audit_entries("REQ-A", [_entry(i) for i in range(1, 6)], db=db)
    assert len(get_audit_entries("REQ-A", limit=0, db=db)) == 1
    assert len(get_audit_entries("REQ-A", limit=10_000, db=db)) == 5


def test_po_upsert_twice_single_document(db, workflow_result):
    po = workflow_result.procurement.po
    upsert_draft_po("REQ-TEST-1", po, NOW, db=db)
    upsert_draft_po("REQ-TEST-1", po.model_dump(mode="json"), NOW + timedelta(hours=1), db=db)
    docs = list(db.purchase_orders.find({"po_id": po.po_id}))
    assert len(docs) == 1
    assert docs[0]["created_at"] == NOW
    assert docs[0]["schema_version"] == 1


def test_non_draft_po_rejected(db, workflow_result):
    data = workflow_result.procurement.po.model_dump(mode="json")
    data["status"] = "approved"
    with pytest.raises(ValueError):
        upsert_draft_po("REQ-TEST-1", data, NOW, db=db)
    assert db.purchase_orders.count_documents({}) == 0


def test_ensure_indexes_idempotent(db):
    ensure_indexes(db)
    ensure_indexes(db)
    info = db.tickets.index_information()
    assert info["ticket_id_unique"]["unique"]
    assert db.agent_logs.index_information()["ticket_id_sequence_unique"]["unique"]


def test_list_recent_tickets_clamps_and_sorts(db):
    for i in range(105):
        create_ticket({"ticket_id": f"REQ-{i}", "raw_text": "x" * 300, "status": "pass",
                       "created_at": NOW + timedelta(seconds=i)}, db=db)
    assert len(list_recent_tickets(limit=0, db=db)) == 1
    rows = list_recent_tickets(limit=1000, db=db)
    assert len(rows) == 100
    assert rows[0]["ticket_id"] == "REQ-104"
    assert len(rows[0]["raw_text"]) == 120
    assert "_id" not in rows[0] and "final_status" in rows[0]


def test_server_errors_become_persistence_error():
    err = ServerSelectionTimeoutError("mongodb://user:secret@host")
    broken = mock.MagicMock()
    for coll in (broken.tickets, broken.purchase_orders, broken.agent_logs):
        for op in ("insert_one", "insert_many", "find_one", "find", "update_one", "create_index"):
            getattr(coll, op).side_effect = err
    calls = [
        lambda: create_ticket({"ticket_id": "REQ-A"}, db=broken),
        lambda: get_ticket("REQ-A", db=broken),
        lambda: update_ticket_result("REQ-A", {}, "pass", NOW, db=broken),
        lambda: mark_ticket_status("REQ-A", "failed", db=broken),
        lambda: list_recent_tickets(db=broken),
        lambda: append_audit_entries("REQ-A", [_entry(1)], db=broken),
        lambda: get_audit_entries("REQ-A", db=broken),
        lambda: get_draft_pos_for_ticket("REQ-A", db=broken),
        lambda: ensure_indexes(broken),
    ]
    for call in calls:
        with pytest.raises(PersistenceError) as exc_info:
            call()
        assert "secret" not in str(exc_info.value)
