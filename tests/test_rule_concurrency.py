"""Phase 3 activation concurrency: the policy-set pointer moves by compare-and-set, so concurrent approvals of
different rules never lose an update and duplicate approvals of the same rule produce one approval + replays.
Runs on mongomock and (if available) on a real local MongoDB with a throwaway database."""
from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.db.errors import PersistenceError
from app.policy_rules import registry
from app.policy_rules.registry import RuleRegistryError
from app.portal_schemas import UserPublic
from app.repositories import policy_rule_repository as repo


@pytest.fixture(autouse=True)
def _rules_env(monkeypatch):
    monkeypatch.setenv("POLICY_RULES_ENABLED", "0")
    monkeypatch.setenv("POLICY_RULES_BOOTSTRAP", "0")


def _reviewer(name):
    return UserPublic(username=name, name=name, email=f"{name}@example.com", role="approver")


def _legacy(rule_id: str, threshold: int) -> dict:
    """legacy_config rules need no document evidence, which keeps the race setup DB-light."""
    return {"rule_id": rule_id, "name": f"Rule {rule_id}", "severity": "review", "source": "legacy_config",
            "condition": {"field": "request.attendees", "op": "gte", "value": threshold, "value_type": "integer"},
            "outcome": {"decision": "review_required", "reason_code": f"R_{rule_id.replace('-', '_')}"[:64],
                        "message": "Review required."}}


def _race(fns):
    barrier = threading.Barrier(len(fns))

    def run(fn):
        barrier.wait()
        try:
            return ("ok", fn())
        except RuleRegistryError as exc:
            return ("err", exc.status_code)
        except PersistenceError:
            return ("err", 503)

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return list(pool.map(run, fns))


def _different_rules_race(n: int = 2):
    ids = [f"RULE-RACE-{i:03d}" for i in range(n)]
    for i, rid in enumerate(ids):
        registry.create_candidate(_legacy(rid, 100 + i), "system:seed-test")
    results = _race([lambda rid=rid, i=i: registry.approve(rid, 1, _reviewer(f"rev{i}")) for i, rid in enumerate(ids)])
    oks = [r[1] for r in results if r[0] == "ok"]
    errs = [r[1] for r in results if r[0] == "err"]
    assert all(code == 409 for code in errs), results
    # Losers (if any after CAS retries) must leave no partial state and can simply retry.
    for rid in ids:
        doc = registry.get_rule(rid, 1)
        if doc["status"] != "approved":
            assert doc["status"] == "candidate"
            oks.append(registry.approve(rid, 1, _reviewer("retry")))
    versions = sorted(o["policy_set_version"] for o in oks)
    assert versions == list(range(1, n + 1)), versions  # every approval got its own pointer increment
    assert registry.active_version() == n
    pset = registry.active_policy_set()
    assert sorted(pset["rule_refs"]) == sorted(f"{rid}@1" for rid in ids)  # no lost update
    assert sorted(r.rule_id for r in registry.active_rules()) == sorted(ids)
    # Pointer chain is consistent: every set links to the previous version.
    chain = [repo.get_policy_set(v) for v in range(1, n + 1)]
    assert [s["previous_version"] for s in chain] == [None] + list(range(1, n))


def _same_rule_race(n: int = 4):
    registry.create_candidate(_legacy("RULE-RACE-SAME", 77), "system:seed-test")
    results = _race([lambda i=i: registry.approve("RULE-RACE-SAME", 1, _reviewer(f"rev{i}")) for i in range(n)])
    oks = [r[1] for r in results if r[0] == "ok"]
    assert len(oks) == n, results
    assert sum(1 for o in oks if not o["replayed"]) == 1
    assert {o["policy_set_version"] for o in oks} == {1}
    assert registry.active_version() == 1
    approvals = [e for e in repo.list_events(rule_id="RULE-RACE-SAME") if e["event_type"] == "rule_approved"]
    assert len(approvals) == 1
    assert registry.get_rule("RULE-RACE-SAME", 1)["approved_by"] == approvals[0]["actor"]


def test_concurrent_different_rules_mongomock():
    _different_rules_race(2)


def test_concurrent_same_rule_mongomock():
    _same_rule_race()


def test_lost_cas_race_retries_then_409(monkeypatch):
    registry.create_candidate(_legacy("RULE-RACE-CAS", 10), "system:seed-test")
    monkeypatch.setattr(repo, "cas_pointer", lambda *a, **k: False)
    with pytest.raises(RuleRegistryError) as e:
        registry.approve("RULE-RACE-CAS", 1, _reviewer("bob"))
    assert e.value.status_code == 409
    monkeypatch.undo()
    assert registry.get_rule("RULE-RACE-CAS", 1)["status"] == "candidate"
    from app.db.mongo import get_database
    assert get_database().policy_sets.count_documents({}) == 0 and registry.active_version() == 0


# ---------------------------------------------------------------------------------------------- real Mongo
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
    db_name = f"workplaceops_test_rules_{uuid.uuid4().hex[:10]}"
    monkeypatch.setenv("MONGODB_DATABASE", db_name)
    monkeypatch.setenv("WORKPLACEOPS_PERSISTENCE", "required")
    set_client(client)
    from app.db import ensure_indexes
    ensure_indexes()
    try:
        yield client[db_name]
    finally:
        client.drop_database(db_name)
        close_client()


@pytest.mark.real_mongo
def test_concurrent_different_rules_real_mongo(real_db):
    _different_rules_race(2)
    names = set(real_db.policy_rules.index_information())
    assert {"rule_version_unique", "rule_status"} <= names
    assert "policy_set_version_unique" in set(real_db.policy_sets.index_information())
    assert "rule_event_time" in set(real_db.policy_rule_events.index_information())


@pytest.mark.real_mongo
def test_concurrent_many_rules_real_mongo(real_db):
    _different_rules_race(4)


@pytest.mark.real_mongo
def test_concurrent_same_rule_real_mongo(real_db):
    _same_rule_race()
