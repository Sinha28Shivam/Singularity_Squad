"""Stage 1 parallel analysis: parity of the parallel and sequential paths, failure semantics, isolation,
determinism and thread hygiene (spec section 5 test matrix)."""
import os

os.environ["WORKPLACEOPS_OFFLINE"] = "1"

import threading
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.agents import policy, vendor_matcher
from app.data_loader import load_json
from app.db import get_database
from app.main import app
from app.orchestrator import (THREAD_NAME_PREFIX, AnalysisBranchError, _analyze_in_parallel, analyze,
                              parallel_analysis_enabled, run_workflow)

TODAY = date(2026, 10, 17)
S = load_json("sample_requests.json")
MODES = ["sequential", "parallel"]


@pytest.fixture(params=MODES)
def mode(request, monkeypatch):
    monkeypatch.setenv("WORKPLACEOPS_PARALLEL_ANALYSIS", "1" if request.param == "parallel" else "0")
    return request.param


def _set_mode(monkeypatch, mode_name):
    monkeypatch.setenv("WORKPLACEOPS_PARALLEL_ANALYSIS", "1" if mode_name == "parallel" else "0")


def business(r):
    """Business-relevant outputs that must be identical in both modes (timestamps/timings excluded)."""
    po = r.procurement.po if r.procurement else None
    return {
        "vendor_id": r.vendors.selected.vendor_id if r.vendors and r.vendors.selected else None,
        "replanned": r.vendors.replanned if r.vendors else None,
        "po_total": po.total if po else None,
        "po_id": po.po_id if po else None,
        "final_status": r.final_status,
        "reconciliation": r.reconciliation.status if r.reconciliation else None,
        "missing_fields": list(r.missing_fields),
        "policy_checks": [(c.name, c.status) for c in r.policy.checks] if r.policy else None,
        "procurement_checks": [(c.name, c.status) for c in r.procurement.checks] if r.procurement else None,
        "audit": [(e.step, e.agent, e.action, e.status, e.detail) for e in r.audit_trail],
    }


def run_both(monkeypatch, *args):
    out = {}
    for m in MODES:
        _set_mode(monkeypatch, m)
        out[m] = run_workflow(*args)
    assert business(out["sequential"]) == business(out["parallel"])
    return out["parallel"]


def analysis_threads():
    return [t for t in threading.enumerate() if t.name.startswith(THREAD_NAME_PREFIX)]


# ---- configuration -------------------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(None, True), ("1", True), ("yes", True), ("0", False),
                                            ("false", False), ("NO", False), (" off ", False)])
def test_parallel_analysis_enabled_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("WORKPLACEOPS_PARALLEL_ANALYSIS", raising=False)
    else:
        monkeypatch.setenv("WORKPLACEOPS_PARALLEL_ANALYSIS", value)
    assert parallel_analysis_enabled() is expected


def test_private_alias_kept():
    assert _analyze_in_parallel is analyze


# ---- scenario matrix, both modes -----------------------------------------------------------------------

def test_happy_path(mode):
    r = run_workflow(S["happy_path"], TODAY)
    assert r.vendors.selected.vendor_id == "VEN-002"
    assert r.vendors.replanned
    assert r.procurement.po.total == 76125
    assert r.final_status == "pass"
    orch = r.extra["orchestration"]
    assert orch["engine"] == "orchestrator" and orch["mode"] == mode
    assert set(orch["analysis_ms"]) == {"policy", "vendor", "total"}
    assert all(v >= 0 for v in orch["analysis_ms"].values())


def test_happy_path_parity(monkeypatch):
    run_both(monkeypatch, S["happy_path"], TODAY)


def test_missing_fields_stop_before_branches(mode, monkeypatch):
    calls = {"policy": 0, "vendor": 0}
    orig_p, orig_v = policy.evaluate, vendor_matcher.match_vendors

    def p(*a):
        calls["policy"] += 1
        return orig_p(*a)

    def v(*a):
        calls["vendor"] += 1
        return orig_v(*a)

    monkeypatch.setattr(policy, "evaluate", p)
    monkeypatch.setattr(vendor_matcher, "match_vendors", v)
    r = run_workflow(S["missing_info"], TODAY)
    assert r.final_status == "needs_clarification"
    assert r.missing_fields and r.policy is None and r.vendors is None
    assert calls == {"policy": 0, "vendor": 0}
    assert "orchestration" not in r.extra


def test_missing_fields_parity(monkeypatch):
    run_both(monkeypatch, S["missing_info"], TODAY)


def test_policy_hard_failure_withholds_po(mode, monkeypatch):
    r = run_workflow(S["too_soon"], TODAY)
    assert r.final_status == "blocked"
    assert r.procurement.po is None
    assert any(c.status == "fail" for c in r.policy.checks)
    run_both(monkeypatch, S["too_soon"], TODAY)


def test_no_eligible_vendor_no_po(mode, monkeypatch):
    monkeypatch.setattr(vendor_matcher, "load_vendors", lambda: [])
    r = run_workflow(S["happy_path"], TODAY)
    assert r.vendors.selected is None
    assert r.procurement.po is None
    assert r.final_status != "pass"
    run_both(monkeypatch, S["happy_path"], TODAY)


def test_over_budget(mode, monkeypatch):
    r = run_workflow(S["over_budget"], TODAY)
    assert r.procurement.po is None
    assert r.final_status != "pass"
    run_both(monkeypatch, S["over_budget"], TODAY)


@pytest.mark.parametrize("invoice_id,status,final", [("INV-5001", "match", "pass"),
                                                     ("INV-5002", "mismatch", "blocked"),
                                                     ("INV-4987", "duplicate", "blocked")])
def test_invoice_reconciliation(mode, monkeypatch, invoice_id, status, final):
    r = run_workflow(S["happy_path"], TODAY, invoice_id)
    assert r.reconciliation.status == status
    assert r.final_status == final
    run_both(monkeypatch, S["happy_path"], TODAY, invoice_id)


def test_disabled_mode_runs_sequentially_in_caller_thread(monkeypatch):
    _set_mode(monkeypatch, "sequential")
    seen = []
    orig_p, orig_v = policy.evaluate, vendor_matcher.match_vendors
    monkeypatch.setattr(policy, "evaluate", lambda *a: (seen.append(threading.current_thread()), orig_p(*a))[1])
    monkeypatch.setattr(vendor_matcher, "match_vendors",
                        lambda *a: (seen.append(threading.current_thread()), orig_v(*a))[1])
    r = run_workflow(S["happy_path"], TODAY)
    assert seen == [threading.current_thread()] * 2
    assert r.extra["orchestration"]["mode"] == "sequential"
    assert r.procurement.po.total == 76125


def test_explicit_parallel_argument_overrides_env(monkeypatch):
    _set_mode(monkeypatch, "sequential")
    names = []
    orig_p = policy.evaluate
    monkeypatch.setattr(policy, "evaluate", lambda *a: (names.append(threading.current_thread().name), orig_p(*a))[1])
    rules = policy.load_rules()
    r = run_workflow(S["happy_path"], TODAY)
    analyze(r.request, TODAY, rules, parallel=True)
    assert names[0] == threading.current_thread().name
    assert names[1].startswith(THREAD_NAME_PREFIX)


def test_repeated_runs_stable(mode):
    first = business(run_workflow(S["happy_path"], TODAY, "INV-5001"))
    for _ in range(10):
        assert business(run_workflow(S["happy_path"], TODAY, "INV-5001")) == first


def test_audit_action_sequence_unchanged(mode):
    r = run_workflow(S["happy_path"], TODAY, "INV-5001")
    actions = [e.action for e in r.audit_trail]
    assert actions == ["start", "extract_request", "evaluate_policy", "match_vendors", "replan", "build_draft_po",
                       "reconcile", "final_review", "human_approval_gate"]
    assert [e.step for e in r.audit_trail] == list(range(1, len(actions) + 1))


# ---- branch failures -----------------------------------------------------------------------------------

@pytest.mark.parametrize("failing", ["policy", "vendor"])
def test_branch_failure_raises_after_both_finish(mode, monkeypatch, failing):
    finished = {"policy": False, "vendor": False}
    orig_p, orig_v = policy.evaluate, vendor_matcher.match_vendors

    def p(*a):
        if failing == "policy":
            finished["policy"] = True
            raise ValueError("policy boom")
        out = orig_p(*a)
        finished["policy"] = True
        return out

    def v(*a):
        if failing == "vendor":
            finished["vendor"] = True
            raise ValueError("vendor boom")
        out = orig_v(*a)
        finished["vendor"] = True
        return out

    monkeypatch.setattr(policy, "evaluate", p)
    monkeypatch.setattr(vendor_matcher, "match_vendors", v)
    with pytest.raises(AnalysisBranchError) as ei:
        run_workflow(S["happy_path"], TODAY)
    assert ei.value.branch == failing
    assert isinstance(ei.value.cause, ValueError)
    assert ei.value.__cause__ is ei.value.cause
    assert finished == {"policy": True, "vendor": True}
    assert not analysis_threads()


def test_both_branches_fail_policy_reported_first(mode, monkeypatch):
    def boom(*a):
        raise RuntimeError("x")

    monkeypatch.setattr(policy, "evaluate", boom)
    monkeypatch.setattr(vendor_matcher, "match_vendors", boom)
    for _ in range(5):
        with pytest.raises(AnalysisBranchError) as ei:
            run_workflow(S["happy_path"], TODAY)
        assert ei.value.branch == "policy"


# ---- concurrency, isolation, thread hygiene ------------------------------------------------------------

def test_branches_overlap_in_parallel_mode(monkeypatch):
    """A barrier proves both analyses are active at the same time (fails fast if run sequentially)."""
    _set_mode(monkeypatch, "parallel")
    rendezvous = threading.Barrier(2, timeout=3)
    orig_p, orig_v = policy.evaluate, vendor_matcher.match_vendors

    def p(req, today):
        rendezvous.wait()
        return orig_p(req, today)

    def v(req, rules):
        rendezvous.wait()
        return orig_v(req, rules)

    monkeypatch.setattr(policy, "evaluate", p)
    monkeypatch.setattr(vendor_matcher, "match_vendors", v)
    r = run_workflow(S["happy_path"], TODAY)
    assert r.procurement.po.total == 76125
    actions = [e.action for e in r.audit_trail]
    assert actions.index("evaluate_policy") < actions.index("match_vendors")


def test_sequential_mode_does_not_overlap(monkeypatch):
    _set_mode(monkeypatch, "sequential")
    rendezvous = threading.Barrier(2, timeout=0.2)
    orig_p = policy.evaluate

    def p(req, today):
        rendezvous.wait()
        return orig_p(req, today)

    monkeypatch.setattr(policy, "evaluate", p)
    with pytest.raises(AnalysisBranchError) as ei:
        run_workflow(S["happy_path"], TODAY)
    assert isinstance(ei.value.cause, threading.BrokenBarrierError)


def test_branch_mutation_is_isolated(mode, monkeypatch):
    baseline = business(run_workflow(S["happy_path"], TODAY))
    orig_p, orig_v = policy.evaluate, vendor_matcher.match_vendors
    seen = []

    def p(req, today):
        out = orig_p(req, today)
        seen.append(req)
        req.attendees = 9999
        req.budget_limit = 1
        return out

    def v(req, rules):
        seen.append(req)
        out = orig_v(req, rules)
        req.service = "mutated"
        rules.per_head_catering_cap = 1.0
        return out

    monkeypatch.setattr(policy, "evaluate", p)
    monkeypatch.setattr(vendor_matcher, "match_vendors", v)
    r = run_workflow(S["happy_path"], TODAY)
    assert len(seen) == 2 and seen[0] is not seen[1]  # each branch got its own copy
    assert r.request.attendees == 100 and r.request.service == "lunch"
    assert business(r) == baseline


def test_analyze_does_not_mutate_caller_inputs(mode, monkeypatch):
    rules = policy.load_rules()
    req = run_workflow(S["happy_path"], TODAY).request
    req_before, rules_before = req.model_dump(), rules.model_dump()

    def v(req_, rules_):
        req_.attendees = 1
        rules_.per_head_catering_cap = 1.0
        return vendor_matcher.VendorMatchResult(selected=None, evaluations=[], replanned=False, explanation="x")

    monkeypatch.setattr(vendor_matcher, "match_vendors", v)
    analyze(req, TODAY, rules)
    assert req.model_dump() == req_before and rules.model_dump() == rules_before


def test_no_leaked_threads(mode):
    for _ in range(3):
        run_workflow(S["happy_path"], TODAY, "INV-5001")
    assert not analysis_threads()


# ---- portal path ---------------------------------------------------------------------------------------

HAPPY = {"title": "Quarterly team lunch", "event_category": "team_lunch", "service": "lunch",
         "event_date": "2026-10-23", "attendees": 100, "budget_inr": 100000, "location": "Bengaluru",
         "approved_vendor_only": True}


@pytest.fixture
def c():
    with TestClient(app) as client:
        yield client


def _submit(c, owner):
    r = c.post("/api/my/tickets", json=HAPPY, headers=owner)
    assert r.status_code == 201, r.text
    return r.json()["ticket_id"]


@pytest.mark.parametrize("invoice_id", [None, "INV-5001", "INV-5002"])
def test_portal_parity(c, monkeypatch, admin_headers, user_headers_factory, invoice_id):
    owner = user_headers_factory("alice")
    outcomes = {}
    for m in MODES:
        _set_mode(monkeypatch, m)
        tid = _submit(c, owner)
        body = {"today": TODAY.isoformat()}
        if invoice_id:
            body["invoice_id"] = invoice_id
        r = c.post(f"/api/admin/tickets/{tid}/run", json=body, headers=admin_headers)
        assert r.status_code == 200, r.text
        run = r.json()
        cur = (run.get("approval") or {}).get("current") or {}
        wr = run["workflow_result"]
        assert wr["extra"]["orchestration"]["mode"] == m
        outcomes[m] = {"status": run["status"], "final_status": wr["final_status"],
                       "reason_codes": cur.get("reason_codes"), "approval_status": cur.get("status"),
                       "po_total": (wr["procurement"]["po"] or {}).get("total"),
                       "actions": [e["action"] for e in wr["audit_trail"]]}
    assert outcomes["sequential"] == outcomes["parallel"]


@pytest.mark.parametrize("failing", ["policy", "vendor"])
def test_portal_branch_failure_marks_ticket_failed(c, mode, monkeypatch, admin_headers, user_headers_factory,
                                                   failing):
    owner = user_headers_factory("alice")
    tid = _submit(c, owner)

    def boom(*a):
        raise RuntimeError("branch down")

    monkeypatch.setattr(policy if failing == "policy" else vendor_matcher,
                        "evaluate" if failing == "policy" else "match_vendors", boom)
    r = c.post(f"/api/admin/tickets/{tid}/run", json={"today": TODAY.isoformat()}, headers=admin_headers)
    assert r.status_code == 500, r.text
    db = get_database()
    ticket = db.tickets.find_one({"ticket_id": tid}, {"_id": 0})
    assert ticket["status"] == "failed"
    assert "AnalysisBranchError" in (ticket.get("error") or "")
    assert db.approval_requests.count_documents({"ticket_id": tid}) == 0
    assert not analysis_threads()
