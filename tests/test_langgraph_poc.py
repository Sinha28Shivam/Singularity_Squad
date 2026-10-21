"""LangGraph PoC (experiments/langgraph_poc): parity, fail-closed branches, concurrency, pause/resume, isolation."""
import ast
import pathlib
import threading
from datetime import date

import pytest

pytest.importorskip("langgraph")

from app.agents import intake, policy, procurement, vendor_matcher  # noqa: E402
from app.data_loader import load_json  # noqa: E402
from app.orchestrator import AnalysisBranchError, run_workflow  # noqa: E402
from experiments.langgraph_poc import graph as lg  # noqa: E402
from experiments.langgraph_poc import resume, run_graph, start  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
TODAY = date(2026, 10, 17)
S = load_json("sample_requests.json")

MATRIX = [(k, None) for k in ("happy_path", "over_budget", "missing_info", "too_soon")] + [
    ("happy_path", inv) for inv in ("INV-5001", "INV-5002", "INV-4987", "INV-0000")]


def _summary(r):
    return {
        "vendor": r.vendors.selected.vendor_id if r.vendors and r.vendors.selected else None,
        "po_total": r.procurement.po.total if r.procurement and r.procurement.po else None,
        "final_status": r.final_status,
        "reconciliation": r.reconciliation.status if r.reconciliation else None,
        "missing": list(r.missing_fields),
        "audit": [(e.step, e.agent, e.action, e.status) for e in r.audit_trail],
        "approver": r.extra.get("approver"),
        "human_approval_required": r.human_approval_required,
        "critic": r.critic.status if r.critic else None,
    }


@pytest.mark.parametrize("key,invoice", MATRIX)
def test_parity_with_orchestrator(key, invoice):
    expected = run_workflow(S[key], TODAY, invoice)
    got = run_graph(S[key], TODAY, invoice)
    assert _summary(got) == _summary(expected)
    assert [e.detail for e in got.audit_trail] == [e.detail for e in expected.audit_trail]
    assert got.human_approval_required is True
    assert got.audit_trail[-1].action == "human_approval_gate"
    assert got.extra["orchestration"]["engine"] == "langgraph"


def test_parity_no_eligible_vendor(monkeypatch):
    monkeypatch.setattr(vendor_matcher, "load_vendors", lambda: [])
    expected = run_workflow(S["happy_path"], TODAY, "INV-5001")
    got = run_graph(S["happy_path"], TODAY, "INV-5001")
    assert got.vendors.selected is None and got.procurement.po is None
    assert got.final_status == "blocked"
    assert _summary(got) == _summary(expected)


@pytest.mark.parametrize("branch", ["policy", "vendor"])
def test_branch_failure_fails_closed_and_stops_draft_po(monkeypatch, branch):
    calls = {"po": 0}
    real_po = procurement.build_draft_po

    def boom(*a, **k):
        raise RuntimeError("synthetic branch failure")

    def counted_po(*a, **k):
        calls["po"] += 1
        return real_po(*a, **k)

    if branch == "policy":
        monkeypatch.setattr(policy, "evaluate", boom)
    else:
        monkeypatch.setattr(vendor_matcher, "match_vendors", boom)
    monkeypatch.setattr(procurement, "build_draft_po", counted_po)

    with pytest.raises(AnalysisBranchError) as exc:
        run_graph(S["happy_path"], TODAY, "INV-5001")
    assert exc.value.branch == branch
    assert calls["po"] == 0

    # The graph itself stops at join_and_gate: no PO, no critic, no approval node, error recorded in state.
    g = lg.build_graph()
    cfg = {"configurable": {"thread_id": f"fail-{branch}"}}
    g.invoke(lg._inputs(S["happy_path"], TODAY, None, "REQ-1001"), cfg)
    snap = g.get_state(cfg)
    assert snap.next == ()
    assert [e["branch"] for e in snap.values["branch_errors"]] == [branch]
    assert "procurement" not in snap.values and "final_status" not in snap.values
    assert calls["po"] == 0


def test_analysis_branches_overlap(monkeypatch):
    barrier = threading.Barrier(2, timeout=5)
    real_eval, real_match = policy.evaluate, vendor_matcher.match_vendors
    threads = set()

    def wait_then(fn):
        def wrapper(*a, **k):
            threads.add(threading.get_ident())
            barrier.wait()  # only passes if both branches are in flight at the same time
            return fn(*a, **k)
        return wrapper

    monkeypatch.setattr(policy, "evaluate", wait_then(real_eval))
    monkeypatch.setattr(vendor_matcher, "match_vendors", wait_then(real_match))
    r = run_graph(S["happy_path"], TODAY)
    assert r.final_status == "pass" and r.vendors.selected.vendor_id == "VEN-002"
    assert len(threads) == 2


def test_pause_resume_does_not_rerun_upstream_and_never_approves(monkeypatch):
    counts = {"intake": 0, "policy": 0, "vendor": 0, "po": 0}

    def count(name, fn):
        def wrapper(*a, **k):
            counts[name] += 1
            return fn(*a, **k)
        return wrapper

    monkeypatch.setattr(intake, "extract_request", count("intake", intake.extract_request))
    monkeypatch.setattr(policy, "evaluate", count("policy", policy.evaluate))
    monkeypatch.setattr(vendor_matcher, "match_vendors", count("vendor", vendor_matcher.match_vendors))
    monkeypatch.setattr(procurement, "build_draft_po", count("po", procurement.build_draft_po))

    g = lg.build_graph()
    paused = start(S["happy_path"], TODAY, "INV-5001", thread_id="t-1", graph=g)
    assert paused["interrupt"] is not None
    assert g.get_state({"configurable": {"thread_id": "t-1"}}).next == ("approval_pending",)
    pending_actions = [e.action for e in paused["result"].audit_trail]
    assert counts == {"intake": 1, "policy": 1, "vendor": 1, "po": 1}

    done = resume("t-1", "APPROVED - please pay now", graph=g)
    assert counts == {"intake": 1, "policy": 1, "vendor": 1, "po": 1}
    assert done.human_approval_required is True
    assert done.final_status == paused["result"].final_status == "pass"
    assert done.extra["human_note"] == "APPROVED - please pay now"
    assert [e.action for e in done.audit_trail] == pending_actions + ["human_note"]
    assert "never approves" in done.audit_trail[-1].detail
    assert g.get_state({"configurable": {"thread_id": "t-1"}}).next == ()
    with pytest.raises(ValueError):
        resume("t-1", "again", graph=g)


def test_main_app_is_isolated_from_experiment():
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [f"{path.relative_to(ROOT)}: {n}" for n in names
                          if n.split(".")[0] in {"experiments", "langgraph", "langchain", "langchain_core"}]
    assert offenders == []
    assert "langgraph" not in (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
