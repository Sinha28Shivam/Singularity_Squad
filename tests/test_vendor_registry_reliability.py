"""Vendor candidate registry reliability: concurrency, outbox/audit repair, evidence, pagination, indexes."""
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError
from pymongo.errors import AutoReconnect, DuplicateKeyError

from app.db.mongo import get_database
from app.vendor_sourcing import repository, service
from app.vendor_sourcing.schemas import CandidateIn, CandidateView

N_THREADS = 8


def _submit(name="Reliable Caterers", **extra):
    body = CandidateIn(name=name, services=["lunch"], **extra)
    return service.submit_candidate(body, "admin")


def _race(fn, n=N_THREADS):
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        try:
            return ("ok", fn(i))
        except ValueError as exc:
            return ("conflict", str(exc))

    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(run, range(n)))


def _events(candidate_id):
    return list(get_database().vendor_candidate_events.find({"candidate_id": candidate_id}, {"_id": 0}))


# --- 1. decide_candidate fix ------------------------------------------------------------------------------

def test_decide_returns_updated_doc_without_internal_fields():
    cand = _submit()
    out = service.decide_candidate(cand["candidate_id"], "approve", "Documents reviewed", "rev1")
    assert out["status"] == "approved" and out["reviewed_by"] == "rev1"
    assert "_id" not in out and "pending_events" not in out and "version" not in out and "active" not in out
    assert out["audit_pending"] is False
    CandidateView.model_validate(out)
    assert service.decide_candidate("VCAND-MISSING", "approve", "Documents reviewed", "rev1") is None


def test_status_history_records_every_transition():
    cand = _submit()
    service.decide_candidate(cand["candidate_id"], "approve", "Documents reviewed", "rev1")
    out = service.decide_candidate(cand["candidate_id"], "suspend", "Expired insurance", "rev2")
    hist = out["status_history"]
    assert [(h["from_status"], h["to_status"]) for h in hist] == [
        (None, "pending_review"), ("pending_review", "approved"), ("approved", "suspended")]
    assert hist[-1]["actor"] == "rev2" and hist[-1]["reason"] == "Expired insurance"
    assert all(h["correlation_id"] for h in hist)


# --- 3. concurrency ---------------------------------------------------------------------------------------

def test_concurrent_duplicate_create_one_wins():
    results = _race(lambda i: _submit(name="Race  Caterers" if i % 2 else "race caterers"))
    assert sum(1 for r in results if r[0] == "ok") == 1, results
    assert sum(1 for r in results if r[0] == "conflict") == N_THREADS - 1
    assert get_database().vendor_candidates.count_documents({"normalized_name": "race caterers"}) == 1


def test_name_reusable_after_rejection_but_not_while_active():
    cand = _submit(name="Reusable Name")
    with pytest.raises(ValueError):
        _submit(name="REUSABLE name")
    service.decide_candidate(cand["candidate_id"], "reject", "Not compliant at all", "rev")
    again = _submit(name="Reusable Name")
    assert again["status"] == "pending_review"


def test_concurrent_decisions_exactly_one_wins():
    cand = _submit()
    decisions = ["approve", "reject", "suspend"]
    results = _race(lambda i: service.decide_candidate(cand["candidate_id"], decisions[i % 3],
                                                       "Concurrent review", f"rev{i}"))
    wins = [r for r in results if r[0] == "ok"]
    assert len(wins) == 1, results
    stored = service.get_candidate(cand["candidate_id"])
    assert stored["status"] == wins[0][1]["status"]
    assert len(stored["status_history"]) == 2
    evts = _events(cand["candidate_id"])
    assert sorted(e["event_id"] for e in evts) == [f"{cand['candidate_id']}:1", f"{cand['candidate_id']}:2"]


def test_unique_partial_index_enforced_by_database():
    repository.ensure_indexes()
    repository.ensure_indexes()  # idempotent
    coll = get_database().vendor_candidates
    names = {ix["name"]: ix for ix in coll.list_indexes()}
    ix = names["vendor_candidate_active_name_unique"]
    assert ix["unique"] and ix["partialFilterExpression"] == {"active": True}
    coll.insert_one({"candidate_id": "VCAND-A1", "normalized_name": "dup", "active": True})
    coll.insert_one({"candidate_id": "VCAND-A2", "normalized_name": "dup", "active": False})
    with pytest.raises(DuplicateKeyError):
        coll.insert_one({"candidate_id": "VCAND-A3", "normalized_name": "dup", "active": True})


def test_index_duplicate_mapped_to_conflict_when_guard_is_bypassed(monkeypatch):
    repository.ensure_indexes()
    _submit(name="Guard Bypass")
    real_find_one = type(get_database().vendor_candidates).find_one

    def find_one(self, filt=None, *a, **k):
        if isinstance(filt, dict) and "normalized_name" in filt:
            return None  # simulate the race window: check passes on both writers
        return real_find_one(self, filt, *a, **k)

    monkeypatch.setattr(type(get_database().vendor_candidates), "find_one", find_one)
    with pytest.raises(ValueError, match="already pending"):
        _submit(name="guard bypass")


# --- 2. outbox / audit repair -----------------------------------------------------------------------------

def _break_audit(monkeypatch):
    def boom(event):
        raise AutoReconnect("audit store down")
    monkeypatch.setattr(repository, "_insert_event", boom)


def test_audit_failure_commits_state_and_repair_delivers_exactly_once(monkeypatch):
    real = repository._insert_event
    _break_audit(monkeypatch)
    cand = _submit(name="Outbox Vendor")
    assert cand["status"] == "pending_review" and cand["audit_pending"] is True
    decided = service.decide_candidate(cand["candidate_id"], "approve", "Documents reviewed", "rev")
    assert decided["status"] == "approved" and decided["audit_pending"] is True
    assert _events(cand["candidate_id"]) == []
    raw = get_database().vendor_candidates.find_one({"candidate_id": cand["candidate_id"]})
    assert [e["event_id"] for e in raw["pending_events"]] == [f"{cand['candidate_id']}:1", f"{cand['candidate_id']}:2"]

    monkeypatch.setattr(repository, "_insert_event", real)
    report = service.repair_pending_events()
    assert report["repaired_candidates"] == 1 and report["delivered_events"] == 2
    again = service.repair_pending_events()
    assert again["scanned"] == 0
    evts = sorted(_events(cand["candidate_id"]), key=lambda e: e["seq"])
    assert [e["event_type"] for e in evts] == ["candidate_submitted", "candidate_approved"]
    assert service.get_candidate(cand["candidate_id"])["audit_pending"] is False


def test_redelivery_of_already_delivered_event_is_noop(monkeypatch):
    cand = _submit(name="Replay Vendor")
    first = _events(cand["candidate_id"])[0]
    first.pop("_id", None)
    # Simulate a crash after delivery but before the pull: event re-queued on the doc.
    get_database().vendor_candidates.update_one({"candidate_id": cand["candidate_id"]},
                                                {"$push": {"pending_events": first}})
    assert service.repair_pending_events()["delivered_events"] == 1
    assert len(_events(cand["candidate_id"])) == 1


def test_read_repairs_pending_events_best_effort(monkeypatch):
    real = repository._insert_event
    _break_audit(monkeypatch)
    cand = _submit(name="Read Repair Vendor")
    assert service.get_candidate(cand["candidate_id"])["audit_pending"] is True  # still failing: no raise
    monkeypatch.setattr(repository, "_insert_event", real)
    assert service.get_candidate(cand["candidate_id"])["audit_pending"] is False
    assert len(_events(cand["candidate_id"])) == 1
    assert service.candidate_events(cand["candidate_id"])[0]["event_type"] == "candidate_submitted"


def test_structured_audit_metadata():
    cand = _submit(name="Audit Meta Vendor")
    service.decide_candidate(cand["candidate_id"], "reject", "Missing GST proof", "rev9",
                             correlation_id="req-123")
    evts = sorted(_events(cand["candidate_id"]), key=lambda e: e["seq"])
    ev = evts[-1]
    for key in ("actor", "action", "entity_id", "from_status", "to_status", "reason", "created_at",
                "correlation_id", "event_id", "event_type"):
        assert key in ev, key
    assert ev["actor"] == "rev9" and ev["action"] == "reject" and ev["entity_id"] == cand["candidate_id"]
    assert ev["from_status"] == "pending_review" and ev["to_status"] == "rejected"
    assert ev["reason"] == "Missing GST proof" and ev["correlation_id"] == "req-123"
    assert evts[0]["from_status"] is None and evts[0]["to_status"] == "pending_review"
    assert evts[0]["correlation_id"].startswith("corr-")  # generated when not supplied


def test_unsafe_correlation_id_is_replaced():
    cand = _submit(name="Corr Vendor")
    service.decide_candidate(cand["candidate_id"], "approve", "Documents reviewed", "rev",
                             correlation_id="bad id $where")
    ev = sorted(_events(cand["candidate_id"]), key=lambda e: e["seq"])[-1]
    assert ev["correlation_id"].startswith("corr-")


def test_persistence_failure_on_state_write_raises(monkeypatch):
    from app.db.errors import PersistenceError
    coll_type = type(get_database().vendor_candidates)

    def boom(self, *a, **k):
        raise AutoReconnect("down")

    monkeypatch.setattr(coll_type, "insert_one", boom)
    with pytest.raises(PersistenceError):
        _submit(name="Down Vendor")


# --- 4. compliance evidence -------------------------------------------------------------------------------

def test_evidence_metadata_stored_unverified():
    cand = _submit(name="Evidence Vendor", compliance_evidence=[
        {"document_type": "FSSAI licence", "reference": "  FSSAI-123  ", "expires_on": "2027-03-31"},
        {"document_type": "GST certificate", "reference": "GSTIN-XYZ"}])
    ev = cand["compliance_evidence"]
    assert ev[0] == {"document_type": "FSSAI licence", "reference": "FSSAI-123", "expires_on": "2027-03-31",
                     "verified": False, "verified_by": None, "verified_at": None}
    assert ev[1]["expires_on"] is None
    view = CandidateView.model_validate(service.get_candidate(cand["candidate_id"]))
    assert str(view.compliance_evidence[0].expires_on) == "2027-03-31"


@pytest.mark.parametrize("item", [
    {"document_type": "GST", "reference": "X", "verified": True},
    {"document_type": "GST", "reference": "X", "expires_on": "31/03/2027"},
    {"document_type": "GST", "reference": "X", "expires_on": "2027-02-30"},
    {"document_type": "GST", "reference": "   "},
    {"document_type": "GST", "reference": "X", "verified_by": "me"},
])
def test_evidence_validation_rejects_bad_metadata(item):
    with pytest.raises(ValidationError):
        CandidateIn(name="Bad Evidence", services=["lunch"], compliance_evidence=[item])


# --- 5. pagination ----------------------------------------------------------------------------------------

def test_pagination_bounds_and_next_offset():
    for i in range(7):
        _submit(name=f"Page Vendor {i}")
    page = service.list_candidates(limit=3, offset=0)
    assert page["total"] == 7 and len(page["items"]) == 3 and page["next_offset"] == 3
    ids = [c["candidate_id"] for c in page["items"]]
    page2 = service.list_candidates(limit=3, offset=3)
    page3 = service.list_candidates(limit=3, offset=6)
    assert page3["next_offset"] is None and len(page3["items"]) == 1
    all_ids = ids + [c["candidate_id"] for c in page2["items"] + page3["items"]]
    assert len(set(all_ids)) == 7
    clamped = service.list_candidates(limit=10_000, offset=-5)
    assert clamped["limit"] == 200 and clamped["offset"] == 0
    assert service.list_candidates(limit=0)["limit"] == 1
    assert service.list_candidates(offset=10**9)["offset"] == 10_000


def test_service_filter_is_exact_and_case_insensitive():
    service.submit_candidate(CandidateIn(name="Svc A", services=["Lunch"]), "admin")
    service.submit_candidate(CandidateIn(name="Svc B", services=["lunchbox"]), "admin")
    names = [c["name"] for c in service.list_candidates(service="lunch")["items"]]
    assert names == ["Svc A"]


def test_query_regex_is_escaped():
    _submit(name="Dot Vendor")
    assert service.list_candidates(query=".*")["total"] == 0


# --- real MongoDB -----------------------------------------------------------------------------------------

MONGO_URI = os.getenv("WORKPLACEOPS_TEST_MONGODB_URI") or "mongodb://localhost:27017/"


def _mongo_available() -> bool:
    try:
        import pymongo
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        try:
            client.admin.command("ping")
            return True
        finally:
            client.close()
    except Exception:
        return False


@pytest.mark.real_mongo
def test_real_mongo_partial_index_concurrency_and_restart(monkeypatch):
    if not _mongo_available():
        pytest.skip("Real MongoDB not reachable (set WORKPLACEOPS_TEST_MONGODB_URI); skipping.")
    import pymongo
    from app.db.mongo import close_client, set_client

    db_name = "workplaceops_test_vendor_" + uuid.uuid4().hex[:10]
    monkeypatch.setenv("MONGODB_DATABASE", db_name)
    monkeypatch.setenv("WORKPLACEOPS_PERSISTENCE", "required")
    admin_client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        set_client(pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=True))
        repository.ensure_indexes()
        idx = {i["name"]: i for i in admin_client[db_name].vendor_candidates.list_indexes()}
        assert idx["vendor_candidate_active_name_unique"]["partialFilterExpression"] == {"active": True}

        results = _race(lambda i: _submit(name="Real Race Vendor"))
        assert sum(1 for r in results if r[0] == "ok") == 1, results
        cid = next(r[1]["candidate_id"] for r in results if r[0] == "ok")
        dec = _race(lambda i: service.decide_candidate(cid, "approve" if i % 2 else "reject",
                                                       "Concurrent review", f"rev{i}"))
        assert sum(1 for r in dec if r[0] == "ok") == 1, dec

        close_client()  # "restart": brand new client
        set_client(pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tz_aware=True))
        stored = service.get_candidate(cid)
        assert stored["status"] in {"approved", "rejected"} and len(stored["status_history"]) == 2
        assert [e["seq"] for e in service.candidate_events(cid)] == [1, 2]
        assert admin_client[db_name].vendor_candidates.count_documents({"normalized_name": "real race vendor"}) == 1
    finally:
        close_client()
        admin_client.drop_database(db_name)
        admin_client.close()
