"""Concurrent decisions: exactly one conditional transition wins; everyone else gets 409."""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.approvals import service
from app.approvals.errors import ApprovalError
from app.portal_schemas import UserPublic
from app.repositories import approval_repository as repo
from app.repositories.audit_repository import get_audit_entries
from tests.test_approval_service import body, make_approval

N_THREADS = 8


def _approvers(n):
    return [UserPublic(username=f"appr{i}", name=f"Approver {i}", email=f"appr{i}@example.com", role="approver")
            for i in range(n)]


def _race(fn, n):
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        try:
            return ("ok", fn(i))
        except ApprovalError as exc:
            return ("err", exc.status_code)

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def _assert_one_winner(doc, results, expect_replays=False):
    wins = [r for r in results if r[0] == "ok" and not r[1][1]]
    replays = [r for r in results if r[0] == "ok" and r[1][1]]
    errors = [r[1] for r in results if r[0] == "err"]
    assert len(wins) == 1, results
    if expect_replays:
        assert len(wins) + len(replays) == len(results), results
    else:
        assert replays == [] and errors == [409] * (len(results) - 1), results
    stored = repo.get_approval(doc["approval_id"])
    assert stored["status"] == "approved" and len(stored["decisions"]) == 1
    acts = [e["action"] for e in get_audit_entries(doc["ticket_id"])]
    assert acts.count("approval_approved") == 1
    seqs = [e["sequence"] for e in get_audit_entries(doc["ticket_id"])]
    assert len(seqs) == len(set(seqs))


def _different_approvers_race():
    _, doc = make_approval()
    actors = _approvers(N_THREADS)
    results = _race(lambda i: service.decide(doc["approval_id"], actors[i], body(doc, key=uuid.uuid4().hex)),
                    N_THREADS)
    _assert_one_winner(doc, results)


def _duplicate_click_race():
    _, doc = make_approval()
    actor = _approvers(1)[0]
    b = body(doc, comment="ok", key="double-click-key")
    results = _race(lambda i: service.decide(doc["approval_id"], actor, b), N_THREADS)
    _assert_one_winner(doc, results, expect_replays=True)


def test_concurrent_approvers_mongomock():
    _different_approvers_race()


def test_duplicate_clicks_mongomock():
    _duplicate_click_race()


# ---------- real MongoDB ----------
@pytest.fixture
def real_db(monkeypatch):
    pymongo = pytest.importorskip("pymongo")
    from app.db.mongo import close_client, set_client
    uri = os.getenv("MONGODB_TEST_URI", "mongodb://localhost:27017/")
    client = pymongo.MongoClient(uri, serverSelectionTimeoutMS=1500, tz_aware=True)
    try:
        client.admin.command("ping")
    except Exception:
        client.close()
        pytest.skip("MongoDB not available on localhost:27017")
    db_name = f"workplaceops_test_{uuid.uuid4().hex[:10]}"
    monkeypatch.setenv("MONGODB_DATABASE", db_name)
    monkeypatch.setenv("WORKPLACEOPS_PERSISTENCE", "required")
    set_client(client)
    try:
        yield client[db_name]
    finally:
        client.drop_database(db_name)
        close_client()


@pytest.mark.real_mongo
def test_concurrent_approvers_real_mongo(real_db):
    _different_approvers_race()
    names = set(real_db.approval_requests.index_information())
    assert {"approval_id_unique", "ticket_version_unique", "status_created", "requester_created",
            "one_pending_per_ticket"} <= names


@pytest.mark.real_mongo
def test_duplicate_clicks_real_mongo(real_db):
    _duplicate_click_race()
