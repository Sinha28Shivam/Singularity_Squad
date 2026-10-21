"""LangGraph PoC of the WorkplaceOps workflow (isolated experiment; the main app never imports this).

Graph: intake -> clarification_gate -(missing)-> critic
                                    -(complete)-> policy_analysis || vendor_analysis (same superstep)
       -> join_and_gate -(branch error)-> END (fail closed: run_graph raises AnalysisBranchError)
                        -(ok)-> draft_po -(invoice_id)-> invoice_reconciliation -> critic
                                         -(no invoice)-> critic
       critic -> approval_pending (interrupt(), human input is recorded as a note only) -> END

Business logic is the existing specialist agents; ``_Audit``, ``_worst`` and ``_finish`` come from
``app.orchestrator`` so the audit trail and Critic/approval-gate semantics stay identical. No node writes to a
database or contacts anything external, and no node can approve: approval stays in the backend approvals service.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from datetime import date
from typing import Any, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agents import billing, intake, policy, procurement, vendor_matcher
from app.orchestrator import AnalysisBranchError, _Audit, _finish, _worst
from app.schemas import AuditEntry, CheckResult, Evidence, WorkflowResult
from experiments.langgraph_poc.state import GraphInput, GraphOutput, WorkflowState

APPROVAL_NOTE = ("Human input recorded as an audit note only. Approval must be performed through the backend "
                 "approvals service; this graph never approves.")


def _entry(agent: str, action: str, status: str, detail: str,
           evidence: Optional[list[Evidence]] = None, llm_used: bool = False) -> AuditEntry:
    # Step numbers are assigned once, in state order, when the WorkflowResult is assembled.
    return AuditEntry(step=0, agent=agent, action=action, status=status, detail=detail,
                      evidence=evidence or [], llm_used=llm_used)


def _to_result(state: dict) -> WorkflowResult:
    """Assemble a WorkflowResult from graph state (no business logic here)."""
    result = WorkflowResult(
        request=state.get("request"), missing_fields=state.get("missing_fields") or [],
        clarification_questions=state.get("clarification_questions") or [],
        policy=state.get("policy"), vendors=state.get("vendors"), procurement=state.get("procurement"),
        reconciliation=state.get("reconciliation"), critic=state.get("critic"),
    )
    if state.get("final_status"):
        result.final_status = state["final_status"]
    result.human_approval_required = True
    if state.get("approver"):
        result.extra["approver"] = state["approver"]
    result.extra["orchestration"] = {"engine": "langgraph", "mode": "parallel"}
    if state.get("human_note") is not None:
        result.extra["human_note"] = state["human_note"]
    result.audit_trail = [e.model_copy(update={"step": i}) for i, e in enumerate(state.get("audit") or [], 1)]
    return result


# ---------------------------------------------------------------- nodes
def intake_node(state: WorkflowState) -> dict:
    today = state["today"]
    start = _entry("Orchestrator", "start", "info", f"Received request; demo date {today.isoformat()}.")
    req, missing, questions, llm_used = intake.extract_request(state["text"], today, state["request_id"])
    extracted = _entry(
        "Intake & Triage", "extract_request", "fail" if missing else "pass",
        f"Extracted service={req.service}, date={req.date}, attendees={req.attendees}, "
        f"budget={req.budget_limit} {req.currency}" + (f"; missing: {', '.join(missing)}" if missing else ""),
        llm_used=llm_used)
    return {"request": req, "missing_fields": missing, "clarification_questions": questions,
            "audit": [start, extracted]}


def clarification_gate(state: WorkflowState) -> dict:
    if state.get("missing_fields"):
        return {"audit": [_entry("Orchestrator", "clarify", "info",
                                 "Stopping: asking requester for missing mandatory fields.")]}
    return {"rules": policy.load_rules()}


def route_after_clarification(state: WorkflowState) -> Any:
    # Missing fields skip analysis/procurement entirely; Critic + approval gate still run (same as orchestrator).
    if state.get("missing_fields"):
        return "critic"
    return ["policy_analysis", "vendor_analysis"]


def _branch_error(branch: str, exc: BaseException) -> dict:
    return {"branch_errors": [{"branch": branch, "error_type": type(exc).__name__, "message": str(exc)}]}


def policy_analysis(state: WorkflowState) -> dict:
    try:
        return {"policy": policy.evaluate(state["request"].model_copy(deep=True), state["today"])}
    except Exception as exc:  # noqa: BLE001 - recorded in state; join_and_gate fails closed
        return _branch_error("policy", exc)


def vendor_analysis(state: WorkflowState) -> dict:
    try:
        return {"vendors": vendor_matcher.match_vendors(state["request"].model_copy(deep=True),
                                                        state["rules"].model_copy(deep=True))}
    except Exception as exc:  # noqa: BLE001 - recorded in state; join_and_gate fails closed
        return _branch_error("vendor", exc)


def join_and_gate(state: WorkflowState) -> dict:
    """Barrier for both branches. Fail closed on any branch error or missing output; else audit in fixed order."""
    errors = list(state.get("branch_errors") or [])
    for branch, key in (("policy", "policy"), ("vendor", "vendors")):
        if state.get(key) is None and not any(e["branch"] == branch for e in errors):
            errors.append({"branch": branch, "error_type": "MissingOutput", "message": "branch produced no output"})
    if errors:
        extra = errors[len(state.get("branch_errors") or []):]
        return {"branch_errors": extra} if extra else {}

    pol, vm = state["policy"], state["vendors"]
    audit = [_entry("Policy Advisor", "evaluate_policy", _worst(pol.checks),
                    "; ".join(f"{c.name}={c.status}" for c in pol.checks),
                    evidence=[Evidence(source=f"policy:{c.clause_id}", detail=c.title) for c in pol.clauses]),
             _entry("Planner & Vendor Matcher", "match_vendors", "pass" if vm.selected else "fail", vm.explanation,
                    evidence=[Evidence(source=f"data:vendors.json#{e.vendor_id}",
                                       detail=("eligible: " if e.eligible else "rejected: ") + "; ".join(e.reasons))
                              for e in vm.evaluations])]
    if vm.replanned:
        audit.append(_entry("Orchestrator", "replan", "warn",
                            "First-choice vendor infeasible; plan revised to an eligible alternative."))
    return {"policy_blocked": any(c.status == "fail" for c in pol.checks), "audit": audit}


def route_after_join(state: WorkflowState) -> str:
    return END if state.get("branch_errors") else "draft_po"


def draft_po(state: WorkflowState) -> dict:
    proc = procurement.build_draft_po(state["request"], state["vendors"].selected, state["rules"])
    if state.get("policy_blocked") and proc.po is not None:
        proc.po = None
        proc.checks.append(CheckResult(
            name="policy_gate", status="fail",
            detail="Draft PO withheld: one or more mandatory policy checks failed. Human review required.",
        ))
    detail = ((f"Draft {proc.po.po_id} total INR {proc.po.total:,.2f}; approver: {proc.po.required_approver}"
               if proc.po else "No draft PO created.") + " | " + " ; ".join(proc.calculation_steps))
    return {"procurement": proc, "audit": [_entry("Procurement", "build_draft_po", _worst(proc.checks), detail,
                                                  evidence=[e for c in proc.checks for e in c.evidence])]}


def route_after_po(state: WorkflowState) -> str:
    # The reconciliation node itself records the "no draft PO" skip so the audit matches the orchestrator.
    return "invoice_reconciliation" if state.get("invoice_id") else "critic"


def invoice_reconciliation(state: WorkflowState) -> dict:
    po = state["procurement"].po
    if po is None:
        return {"audit": [_entry("Billing Guardian", "reconcile", "uncertain",
                                 "Skipped: no draft PO to reconcile against.")]}
    inv = billing.get_invoice(state["invoice_id"])
    if inv is None:
        return {"audit": [_entry("Billing Guardian", "reconcile", "fail", f"Invoice {state['invoice_id']} not found.")]}
    audit = []
    inv, delivery, rebound = billing.bind_demo_documents(inv, billing.get_delivery(inv.delivery_id), po)
    if rebound:
        audit.append(_entry("Billing Guardian", "bind_demo_invoice", "info",
                            f"Synthetic invoice {inv.invoice_id} references placeholder "
                            f"{billing.DEMO_PO_PLACEHOLDER}; bound to {po.po_id} for this ticket."))
    rec = billing.reconcile(inv, po, delivery, billing.load_history(), state["rules"])
    audit.append(_entry("Billing Guardian", "reconcile", "pass" if rec.status == "match" else "fail",
                        f"{rec.invoice_id}: {rec.status}; expected INR {rec.expected_total:,.2f}, "
                        f"invoiced INR {rec.invoiced_total:,.2f}. {rec.recommendation}",
                        evidence=[e for c in rec.checks for e in c.evidence]))
    return {"reconciliation": rec, "audit": audit}


def critic_node(state: WorkflowState) -> dict:
    """Reuse the orchestrator's _finish: read-only Critic + final status + human_approval_gate audit entry."""
    finished = _finish(_to_result(state), _Audit())
    return {"critic": finished.critic, "final_status": finished.final_status,
            "approver": finished.extra["approver"], "human_approval_required": True,
            "audit": [e.model_copy(update={"step": 0}) for e in finished.audit_trail]}


def approval_pending(state: WorkflowState) -> dict:
    """Pause for a human. Resuming records the input as a note; status and approval flags never change."""
    value = interrupt({"final_status": state.get("final_status"), "approver": state.get("approver"),
                       "message": "Awaiting human decision. " + APPROVAL_NOTE})
    note = str(value)[:500]
    return {"human_note": note, "human_approval_required": True,
            "audit": [_entry("Human", "human_note", "info", f"Input: {note}. {APPROVAL_NOTE}")]}


# ---------------------------------------------------------------- graph
_CHECKPOINT_TYPES = [("app.schemas", n) for n in (
    "AuditEntry", "CriticReport", "EventRequest", "PolicyReport", "PolicyRules", "ProcurementResult",
    "ReconciliationReport", "VendorMatchResult")]


def new_checkpointer() -> MemorySaver:
    """In-memory checkpointer that explicitly allow-lists only our Pydantic schemas for deserialization."""
    return MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=_CHECKPOINT_TYPES))


def build_graph(checkpointer: Any = None):
    g = StateGraph(WorkflowState, input_schema=GraphInput, output_schema=GraphOutput)
    g.add_node("intake", intake_node)
    g.add_node("clarification_gate", clarification_gate)
    g.add_node("policy_analysis", policy_analysis)
    g.add_node("vendor_analysis", vendor_analysis)
    g.add_node("join_and_gate", join_and_gate)
    g.add_node("draft_po", draft_po)
    g.add_node("invoice_reconciliation", invoice_reconciliation)
    g.add_node("critic", critic_node)
    g.add_node("approval_pending", approval_pending)

    g.add_edge(START, "intake")
    g.add_edge("intake", "clarification_gate")
    g.add_conditional_edges("clarification_gate", route_after_clarification,
                            ["critic", "policy_analysis", "vendor_analysis"])
    g.add_edge(["policy_analysis", "vendor_analysis"], "join_and_gate")  # waits for both branches
    g.add_conditional_edges("join_and_gate", route_after_join, ["draft_po", END])
    g.add_conditional_edges("draft_po", route_after_po, ["invoice_reconciliation", "critic"])
    g.add_edge("invoice_reconciliation", "critic")
    g.add_edge("critic", "approval_pending")
    g.add_edge("approval_pending", END)
    return g.compile(checkpointer=checkpointer if checkpointer is not None else new_checkpointer())


def _inputs(text: str, today: Optional[date], invoice_id: Optional[str], request_id: str) -> dict:
    return {"text": text, "today": today or date.today(), "invoice_id": invoice_id, "request_id": request_id}


def _raise_on_branch_error(state: dict) -> None:
    errors = state.get("branch_errors") or []
    for branch in ("policy", "vendor"):  # deterministic: policy reported first, like the orchestrator
        for e in errors:
            if e["branch"] == branch:
                raise AnalysisBranchError(branch, RuntimeError(f"{e['error_type']}: {e['message']}"))


def _state(graph, thread_id: str) -> dict:
    return dict(graph.get_state({"configurable": {"thread_id": thread_id}}).values)


def run_graph(text: str, today: Optional[date] = None, invoice_id: Optional[str] = None,
              request_id: str = "REQ-1001") -> WorkflowResult:
    """Run to the approval pause and return the pending WorkflowResult (parity with run_workflow)."""
    graph = build_graph()
    thread_id = str(uuid.uuid4())
    graph.invoke(_inputs(text, today, invoice_id, request_id), {"configurable": {"thread_id": thread_id}})
    state = _state(graph, thread_id)
    _raise_on_branch_error(state)
    return _to_result(state)


_DEMO_GRAPH = None


def _demo_graph():
    global _DEMO_GRAPH
    if _DEMO_GRAPH is None:
        _DEMO_GRAPH = build_graph(new_checkpointer())
    return _DEMO_GRAPH


def start(text: str, today: Optional[date] = None, invoice_id: Optional[str] = None,
          request_id: str = "REQ-1001", thread_id: Optional[str] = None, graph: Any = None) -> dict:
    """Run until the human pause. Returns {thread_id, result, interrupt} (interrupt is None if not paused)."""
    graph = graph or _demo_graph()
    thread_id = thread_id or str(uuid.uuid4())
    out = graph.invoke(_inputs(text, today, invoice_id, request_id), {"configurable": {"thread_id": thread_id}})
    state = _state(graph, thread_id)
    _raise_on_branch_error(state)
    pending = out.get("__interrupt__") if isinstance(out, dict) else None
    return {"thread_id": thread_id, "result": _to_result(state),
            "interrupt": pending[0].value if pending else None}


def resume(thread_id: str, value: Any, graph: Any = None) -> WorkflowResult:
    """Resume a paused thread. The value is stored as an audit note; the result stays pending human approval."""
    graph = graph or _demo_graph()
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    if "approval_pending" not in (snapshot.next or ()):
        raise ValueError(f"thread {thread_id} is not paused at approval_pending")
    graph.invoke(Command(resume=value), config)
    return _to_result(_state(graph, thread_id))


# ---------------------------------------------------------------- benchmark
def _bench(reps: int, sleep_ms: float) -> dict:
    import os
    from app import orchestrator
    from app.data_loader import load_json

    os.environ["WORKPLACEOPS_OFFLINE"] = "1"
    os.environ.setdefault("POLICY_RAG_ENABLED", "0")
    os.environ.setdefault("POLICY_RULES_ENABLED", "0")
    os.environ["WORKPLACEOPS_PARALLEL_ANALYSIS"] = "1"
    text, today = load_json("sample_requests.json")["happy_path"], date(2026, 10, 17)
    originals = (policy.evaluate, vendor_matcher.match_vendors)
    if sleep_ms:
        def slow(fn):
            def wrapper(*a, **k):
                time.sleep(sleep_ms / 1000.0)
                return fn(*a, **k)
            return wrapper
        policy.evaluate, vendor_matcher.match_vendors = slow(originals[0]), slow(originals[1])
    try:
        out = {}
        for name, fn in (("orchestrator_parallel", orchestrator.run_workflow), ("langgraph", run_graph)):
            fn(text, today)  # warm-up
            samples = []
            for _ in range(reps):
                t0 = time.perf_counter()
                fn(text, today)
                samples.append((time.perf_counter() - t0) * 1000.0)
            samples.sort()
            out[name] = {"median_ms": round(statistics.median(samples), 2),
                         "mean_ms": round(statistics.mean(samples), 2),
                         "p95_ms": round(samples[int(0.95 * (len(samples) - 1))], 2)}
        return {"reps": reps, "sleep_ms": sleep_ms, **out}
    finally:
        policy.evaluate, vendor_matcher.match_vendors = originals


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Benchmark run_graph vs run_workflow (happy_path, offline).")
    ap.add_argument("--reps", type=int, default=30)
    args = ap.parse_args()
    print(json.dumps([_bench(args.reps, 0), _bench(args.reps, 150)], indent=2))
