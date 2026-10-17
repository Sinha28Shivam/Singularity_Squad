"""Phase 3 rule registry: lifecycle, policy-set pointer, audit-must-succeed, idempotency, separation,
conflicts, effective dates, supersession and evidence validation (mongomock)."""
from __future__ import annotations

import datetime as dt

import pytest

from app.db.errors import PersistenceError
from app.policy_rules import evaluator, registry
from app.policy_rules.registry import RuleRegistryError
from app.policy_rules.schemas import EvidenceRef, PolicyRuleIn
from app.portal_schemas import UserPublic
from app.repositories import policy_chunk_repository as chunk_repo
from app.repositories import policy_rule_repository as repo
from tests.policy_rag_helpers import COMPANY_PAGES, ingest


@pytest.fixture(autouse=True)
def _rules_env(monkeypatch):
    monkeypatch.setenv("POLICY_RULES_ENABLED", "0")
    monkeypatch.setenv("POLICY_RULES_BOOTSTRAP", "0")


def reviewer(name: str = "rita", role: str = "approver") -> UserPublic:
    return UserPublic(username=name, name=name.title(), email=f"{name}@example.com", role=role)


def company_doc():
    res = ingest(COMPANY_PAGES, "company_procurement.pdf", logical_key="company-procurement")
    assert res.ok and res.status == "ready", res
    return res.document_id


def evidence_for(document_id: str, needle: str = "150 attendees") -> EvidenceRef:
    from app.repositories import policy_document_repository as doc_repo
    doc = doc_repo.get_document(document_id)
    for c in chunk_repo.list_chunks(document_ids=[document_id], include_embeddings=False):
        if needle in " ".join(c["text"].split()):
            return EvidenceRef(document_id=document_id, document_version=doc["version"],
                               content_hash=c["content_hash"], chunk_id=c["chunk_id"],
                               page_number=c["page_number"], section=c.get("section"), excerpt=needle)
    raise AssertionError(f"no chunk contains {needle!r}")


def manual_rule(document_id: str, *, rule_id: str = "RULE-LARGE-EVENT", threshold: int = 150,
                decision: str = "review_required", reason: str = "LARGE_EVENT_REVIEW", **extra) -> PolicyRuleIn:
    data = {
        "rule_id": rule_id, "name": "Large event review", "severity": "review" if decision != "blocked" else "block",
        "condition": {"field": "request.attendees", "op": "gt", "value": threshold, "value_type": "integer"},
        "outcome": {"decision": decision, "reason_code": reason, "message": "Large events need a risk assessment."},
        "evidence_refs": [evidence_for(document_id).model_dump()],
    }
    data.update(extra)
    return PolicyRuleIn.model_validate(data)


def counts():
    from app.db.mongo import get_database
    db = get_database()
    return (db.policy_sets.count_documents({}), repo.get_pointer()["version"])


# ------------------------------------------------------------------------------------------- lifecycle
def test_candidate_validate_approve_activates_new_policy_set():
    doc_id = company_doc()
    assert registry.active_version() == 0
    assert registry.active_policy_set() is None and registry.active_rules() == []
    cand = registry.create_candidate(manual_rule(doc_id), reviewer("alice"))
    assert cand["status"] == "candidate" and cand["source"] == "manual" and cand["created_by"] == "alice"
    assert registry.active_rules() == []  # nothing auto-activates
    v = registry.validate(cand["rule_id"], cand["rule_version"], reviewer("alice"))
    assert v["errors"] == [] and v["conflicts"] == [] and v["rule"]["status"] == "validated"
    out = registry.approve(cand["rule_id"], cand["rule_version"], reviewer("bob"), comment="ok")
    assert out["replayed"] is False and out["policy_set_version"] == 1
    assert out["rule"]["status"] == "approved" and out["rule"]["approved_by"] == "bob"
    assert registry.active_version() == 1
    pset = registry.active_policy_set()
    assert pset["rule_refs"] == ["RULE-LARGE-EVENT@1"]
    assert pset["rule_hashes"]["RULE-LARGE-EVENT@1"] == evaluator.canonical_rule_hash(registry.to_model(out["rule"]))
    assert [r.rule_id for r in registry.active_rules()] == ["RULE-LARGE-EVENT"]
    types = [e["event_type"] for e in repo.list_events(rule_id="RULE-LARGE-EVENT")]
    assert {"rule_candidate_created", "rule_validated", "rule_approved", "policy_set_activated"} <= set(types)


def test_duplicate_approval_is_idempotent_replay():
    doc_id = company_doc()
    cand = registry.create_candidate(manual_rule(doc_id), reviewer("alice"))
    first = registry.approve(cand["rule_id"], 1, reviewer("bob"))
    again = registry.approve(cand["rule_id"], 1, reviewer("carol", "admin"))
    assert again["replayed"] is True and again["policy_set_version"] == first["policy_set_version"] == 1
    assert registry.active_version() == 1 and counts() == (1, 1)
    assert again["rule"]["approved_by"] == "bob"


def test_creator_of_manual_candidate_cannot_approve():
    doc_id = company_doc()
    cand = registry.create_candidate(manual_rule(doc_id), reviewer("alice"))
    with pytest.raises(RuleRegistryError) as e:
        registry.approve(cand["rule_id"], 1, reviewer("alice"))
    assert e.value.status_code == 403
    assert registry.get_rule(cand["rule_id"], 1)["status"] == "candidate" and registry.active_version() == 0


def test_requesters_and_service_actors_cannot_review():
    doc_id = company_doc()
    with pytest.raises(RuleRegistryError) as e:
        registry.create_candidate(manual_rule(doc_id), reviewer("ursula", "user"))
    assert e.value.status_code == 403
    cand = registry.create_candidate(manual_rule(doc_id), reviewer("alice"))
    for actor in (reviewer("ursula", "user"), "system:extractor", "system:bootstrap"):
        with pytest.raises(RuleRegistryError) as e:
            registry.approve(cand["rule_id"], 1, actor)
        assert e.value.status_code == 403
    with pytest.raises(RuleRegistryError) as e:
        registry.reject(cand["rule_id"], 1, reviewer("ursula", "user"), "no")
    assert e.value.status_code == 403
    assert registry.get_rule(cand["rule_id"], 1)["status"] == "candidate" and registry.active_version() == 0


def test_rejected_rule_never_activates():
    doc_id = company_doc()
    cand = registry.create_candidate(manual_rule(doc_id), reviewer("alice"))
    with pytest.raises(RuleRegistryError) as e:
        registry.reject(cand["rule_id"], 1, reviewer("bob"), "  ")
    assert e.value.status_code == 422
    rej = registry.reject(cand["rule_id"], 1, reviewer("bob"), "Not our policy")
    assert rej["status"] == "rejected" and rej["review_comment"] == "Not our policy"
    with pytest.raises(RuleRegistryError) as e:
        registry.approve(cand["rule_id"], 1, reviewer("bob"))
    assert e.value.status_code == 409
    assert registry.active_rules() == [] and registry.active_version() == 0
    assert registry.reject(cand["rule_id"], 1, reviewer("bob"), "again")["status"] == "rejected"  # idempotent


def test_rejecting_an_approved_rule_deactivates_it_with_a_new_set():
    doc_id = company_doc()
    cand = registry.create_candidate(manual_rule(doc_id), reviewer("alice"))
    registry.approve(cand["rule_id"], 1, reviewer("bob"))
    registry.reject(cand["rule_id"], 1, reviewer("bob"), "Withdrawn")
    assert registry.active_version() == 2 and registry.active_policy_set()["rule_refs"] == []
    assert registry.active_rules() == []


def test_new_version_supersedes_old_and_keeps_history():
    doc_id = company_doc()
    v1 = registry.create_candidate(manual_rule(doc_id), reviewer("alice"))
    registry.approve(v1["rule_id"], 1, reviewer("bob"))
    v2 = registry.create_candidate(manual_rule(doc_id, threshold=120), reviewer("alice"))
    assert v2["rule_version"] == 2 and v2["supersedes"] == "RULE-LARGE-EVENT@1"
    out = registry.approve(v2["rule_id"], 2, reviewer("bob"))
    assert out["policy_set_version"] == 2
    old = registry.get_rule("RULE-LARGE-EVENT", 1)
    assert old["status"] == "superseded" and old["superseded_by"] == "RULE-LARGE-EVENT@2"
    assert [f"{r.rule_id}@{r.rule_version}" for r in registry.active_rules()] == ["RULE-LARGE-EVENT@2"]
    assert repo.get_policy_set(1)["rule_refs"] == ["RULE-LARGE-EVENT@1"]  # history stays immutable
    with pytest.raises(RuleRegistryError) as e:
        registry.approve("RULE-LARGE-EVENT", 1, reviewer("bob"))
    assert e.value.status_code == 409


def test_effective_dates_filter_active_rules():
    doc_id = company_doc()
    future = registry.create_candidate(manual_rule(doc_id, rule_id="RULE-FUTURE", reason="FUTURE_REVIEW",
                                                   threshold=10, effective_from=dt.date(2030, 1, 1)),
                                       reviewer("alice"))
    registry.approve(future["rule_id"], 1, reviewer("bob"))
    assert registry.active_rules(dt.date(2029, 12, 31)) == []
    assert [r.rule_id for r in registry.active_rules(dt.date(2030, 1, 1))] == ["RULE-FUTURE"]
    window = registry.create_candidate(manual_rule(doc_id, rule_id="RULE-WINDOW", reason="WINDOW_REVIEW",
                                                   threshold=20, effective_from=dt.date(2026, 1, 1),
                                                   effective_until=dt.date(2099, 6, 30)), reviewer("alice"))
    registry.approve(window["rule_id"], 1, reviewer("bob"))
    assert "RULE-WINDOW" not in [r.rule_id for r in registry.active_rules(dt.date(2099, 7, 1))]
    assert "RULE-WINDOW" in [r.rule_id for r in registry.active_rules(dt.date(2099, 6, 30))]


def test_expired_candidate_cannot_be_approved():
    doc_id = company_doc()
    cand = registry.create_candidate(manual_rule(doc_id, effective_from=dt.date(2020, 1, 1),
                                                 effective_until=dt.date(2020, 12, 31)), reviewer("alice"))
    assert cand["status"] == "needs_review" and any("expired" in e for e in cand["validation_errors"])
    with pytest.raises(RuleRegistryError) as e:
        registry.approve(cand["rule_id"], 1, reviewer("bob"))
    assert e.value.status_code == 409 and registry.active_version() == 0


# ------------------------------------------------------------------------------------------- audit failure
def test_activation_fails_and_rolls_back_when_audit_write_fails(monkeypatch):
    doc_id = company_doc()
    v1 = registry.create_candidate(manual_rule(doc_id), reviewer("alice"))
    registry.approve(v1["rule_id"], 1, reviewer("bob"))
    v2 = registry.create_candidate(manual_rule(doc_id, threshold=99), reviewer("alice"))
    before = counts()
    real = repo.record_event

    def failing(event_type, actor, **kw):
        if event_type in ("rule_approved", "policy_set_activated"):
            raise PersistenceError()
        return real(event_type, actor, **kw)

    monkeypatch.setattr(repo, "record_event", failing)
    with pytest.raises(RuleRegistryError) as e:
        registry.approve(v2["rule_id"], 2, reviewer("bob"))
    assert e.value.status_code == 503
    assert counts() == before
    assert registry.get_rule("RULE-LARGE-EVENT", 2)["status"] == "candidate"
    assert registry.get_rule("RULE-LARGE-EVENT", 1)["status"] == "approved"
    assert [f"{r.rule_id}@{r.rule_version}" for r in registry.active_rules()] == ["RULE-LARGE-EVENT@1"]
    monkeypatch.setattr(repo, "record_event", real)
    assert any(e["event_type"] == "rule_activation_failed" for e in repo.list_events(rule_id="RULE-LARGE-EVENT"))
    assert registry.approve(v2["rule_id"], 2, reviewer("bob"))["policy_set_version"] == 2  # retry succeeds


def test_database_failure_during_activation_reports_503_without_partial_state(monkeypatch):
    doc_id = company_doc()
    cand = registry.create_candidate(manual_rule(doc_id), reviewer("alice"))

    def boom(*a, **k):
        raise PersistenceError()

    monkeypatch.setattr(repo, "cas_pointer", boom)
    with pytest.raises(PersistenceError):
        registry.approve(cand["rule_id"], 1, reviewer("bob"))
    assert registry.get_rule(cand["rule_id"], 1)["status"] == "candidate"
    assert repo.get_pointer()["version"] == 0


def test_stale_expected_policy_set_version_is_rejected():
    doc_id = company_doc()
    a = registry.create_candidate(manual_rule(doc_id, rule_id="RULE-AAA", reason="AAA_REVIEW"), reviewer("alice"))
    b = registry.create_candidate(manual_rule(doc_id, rule_id="RULE-BBB", reason="BBB_REVIEW", threshold=300),
                                  reviewer("alice"))
    registry.approve(a["rule_id"], 1, reviewer("bob"), expected_policy_set_version=0)
    with pytest.raises(RuleRegistryError) as e:
        registry.approve(b["rule_id"], 1, reviewer("bob"), expected_policy_set_version=0)
    assert e.value.status_code == 409
    assert registry.approve(b["rule_id"], 1, reviewer("bob"), expected_policy_set_version=1)["policy_set_version"] == 2


# ------------------------------------------------------------------------------------------- conflicts
def test_contradictory_rule_is_blocked_until_resolved():
    doc_id = company_doc()
    a = registry.create_candidate(manual_rule(doc_id, rule_id="RULE-AAA"), reviewer("alice"))
    registry.approve(a["rule_id"], 1, reviewer("bob"))
    b = registry.create_candidate(manual_rule(doc_id, rule_id="RULE-BBB", decision="blocked"), reviewer("alice"))
    assert b["status"] == "needs_review" and any("RULE-AAA@1" in c for c in b["conflict_refs"])
    with pytest.raises(RuleRegistryError) as e:
        registry.approve(b["rule_id"], 1, reviewer("bob"))
    assert e.value.status_code == 409 and e.value.conflicts
    assert registry.active_version() == 1
    # Explicit supersession resolves the conflict.
    c = registry.create_candidate(manual_rule(doc_id, rule_id="RULE-CCC", decision="blocked",
                                              supersedes="RULE-AAA@1"), reviewer("alice"))
    assert c["conflict_refs"] == []
    registry.approve(c["rule_id"], 1, reviewer("bob"))
    assert registry.get_rule("RULE-AAA", 1)["status"] == "superseded"
    assert [r.rule_id for r in registry.active_rules()] == ["RULE-CCC"]


def test_detect_conflicts_generic_cases():
    doc_id = company_doc()
    base = registry.create_candidate(manual_rule(doc_id, rule_id="RULE-AAA"), reviewer("alice"))
    approved = registry.approve(base["rule_id"], 1, reviewer("bob"))["rule"]
    me = registry.to_model({**approved, "rule_id": "RULE-ZZZ", "status": "candidate",
                            "outcome": {**approved["outcome"], "decision": "pass"}})
    found = registry.detect_conflicts(me, [approved])
    assert any("same reason_code" in f for f in found)
    dangling = registry.to_model({**approved, "rule_id": "RULE-YYY", "status": "candidate",
                                  "supersedes": "RULE-NOPE@4"})
    assert any("does not exist" in f for f in registry.detect_conflicts(dangling, []))
    money_a = registry.to_model({**approved, "condition": {"field": "po.total", "op": "gt", "value": "100",
                                                           "value_type": "money", "currency": "INR"}})
    money_b = registry.to_model({**approved, "rule_id": "RULE-MMM", "status": "candidate",
                                 "condition": {"field": "po.total", "op": "gt", "value": "100",
                                               "value_type": "money", "currency": "USD"}})
    assert any("currency" in f for f in registry.detect_conflicts(money_b, [money_a]))


def test_invalid_rule_is_needs_review_and_not_approvable():
    doc_id = company_doc()
    data = manual_rule(doc_id, rule_id="RULE-BAD").model_dump(by_alias=True)
    data["condition"] = {"field": "request.unknown_fact", "op": "gt", "value": 1, "value_type": "integer"}
    cand = registry.create_candidate(PolicyRuleIn.model_validate(data), reviewer("alice"))
    assert cand["status"] == "needs_review" and any("unknown field" in e for e in cand["validation_errors"])
    with pytest.raises(RuleRegistryError) as e:
        registry.approve(cand["rule_id"], 1, reviewer("bob"))
    assert e.value.status_code == 409 and e.value.errors
    v = registry.validate(cand["rule_id"], 1, reviewer("bob"))
    assert v["rule"]["status"] == "needs_review" and v["errors"]


# ------------------------------------------------------------------------------------------- evidence
def test_fabricated_evidence_refs_are_rejected_for_manual_rules():
    doc_id = company_doc()
    good = evidence_for(doc_id)
    for bad in (good.model_copy(update={"chunk_id": "chk_doesnotexist000000"}),
                good.model_copy(update={"content_hash": "0" * 64}),
                good.model_copy(update={"page_number": 2}),
                good.model_copy(update={"excerpt": "events over 10 attendees are banned"}),
                good.model_copy(update={"document_id": "doc_0000000000000000"})):
        rule = manual_rule(doc_id).model_copy(update={"evidence_refs": [bad]})
        with pytest.raises(RuleRegistryError) as e:
            registry.create_candidate(rule, reviewer("alice"))
        assert e.value.status_code == 422, bad
    assert repo.list_rules() == []


def test_evidence_from_superseded_document_blocks_approval():
    from app.repositories import policy_document_repository as doc_repo
    doc_id = company_doc()
    cand = registry.create_candidate(manual_rule(doc_id), reviewer("alice"))
    doc_repo.update_document(doc_id, {"status": "superseded"})
    with pytest.raises(RuleRegistryError) as e:
        registry.approve(cand["rule_id"], 1, reviewer("bob"))
    assert e.value.status_code == 409 and any("not authoritative" in x for x in e.value.errors)
    assert registry.active_version() == 0


def test_legacy_config_rule_may_have_no_evidence_but_extracted_must():
    _, seeds = evaluator.load_seed_bundle()
    doc = registry.create_candidate(seeds[0], registry.BOOTSTRAP_ACTOR)
    assert doc["status"] == "candidate" and doc["evidence_refs"] == []
    extracted = seeds[1].model_copy(update={"source": "extracted", "rule_id": "RULE-EXTRACTED-X"})
    doc2 = registry.create_candidate(extracted, registry.EXTRACTOR_ACTOR)
    assert doc2["status"] == "needs_review" and any("evidence" in e for e in doc2["validation_errors"])


def test_candidate_creation_rolls_back_when_audit_fails(monkeypatch):
    doc_id = company_doc()

    def boom(*a, **k):
        raise PersistenceError()

    monkeypatch.setattr(repo, "record_event", boom)
    with pytest.raises(PersistenceError):
        registry.create_candidate(manual_rule(doc_id), reviewer("alice"))
    monkeypatch.undo()
    assert repo.list_rules() == []


# ------------------------------------------------------------------------------------------- bootstrap
def test_bootstrap_from_seed_is_idempotent_and_audited():
    n = registry.bootstrap_from_seed()
    _, seeds = evaluator.load_seed_bundle()
    assert n == len(seeds) and registry.active_version() == len(seeds)
    assert sorted(r.rule_id for r in registry.active_rules()) == sorted(s.rule_id for s in seeds)
    assert all(r.approved_by == registry.BOOTSTRAP_ACTOR for r in registry.active_rules())
    assert registry.bootstrap_from_seed() == 0 and registry.active_version() == len(seeds)
    assert any(e["event_type"] == "rule_bootstrap_completed" for e in repo.list_events())


def test_bootstrap_respects_reviewer_rejection():
    registry.bootstrap_from_seed()
    version = registry.active_version()
    registry.reject("RULE-SAFETY-REVIEW", 1, reviewer("bob"), "We handle this elsewhere")
    assert registry.bootstrap_from_seed() == 0
    assert "RULE-SAFETY-REVIEW" not in [r.rule_id for r in registry.active_rules()]
    assert registry.active_version() == version + 1


def test_main_bootstrap_hook_respects_env(monkeypatch):
    from app import main
    main._bootstrap_policy_rules()  # POLICY_RULES_BOOTSTRAP=0 from fixture
    assert registry.active_version() == 0
    monkeypatch.setenv("POLICY_RULES_BOOTSTRAP", "1")
    main._bootstrap_policy_rules()
    assert registry.active_version() > 0
    monkeypatch.setattr(registry, "bootstrap_from_seed", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    main._bootstrap_policy_rules()  # failures only log
