"""Manager / Orchestrator: owns workflow state, delegates to specialist agents, records the audit trail."""
from __future__ import annotations

from datetime import date
from typing import Optional

from app.agents import billing, critic, intake, policy, procurement, vendor_matcher
from app.schemas import AuditEntry, CheckResult, Evidence, WorkflowResult


class _Audit:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def log(self, agent: str, action: str, status: str, detail: str,
            evidence: Optional[list[Evidence]] = None, llm_used: bool = False) -> None:
        self.entries.append(AuditEntry(
            step=len(self.entries) + 1, agent=agent, action=action, status=status,
            detail=detail, evidence=evidence or [], llm_used=llm_used,
        ))


def _worst(checks: list[CheckResult]) -> str:
    order = ["fail", "uncertain", "warn", "pass"]
    for s in order:
        if any(c.status == s for c in checks):
            return s
    return "pass"


def run_workflow(text: str, today: Optional[date] = None, invoice_id: Optional[str] = None,
                 request_id: str = "REQ-1001") -> WorkflowResult:
    today = today or date.today()
    audit = _Audit()
    result = WorkflowResult()
    audit.log("Orchestrator", "start", "info", f"Received request; demo date {today.isoformat()}.")

    # 1-2. Intake & clarification
    req, missing, questions, llm_used = intake.extract_request(text, today, request_id)
    result.request, result.missing_fields, result.clarification_questions = req, missing, questions
    audit.log("Intake & Triage", "extract_request", "fail" if missing else "pass",
              f"Extracted service={req.service}, date={req.date}, attendees={req.attendees}, "
              f"budget={req.budget_limit} {req.currency}" + (f"; missing: {', '.join(missing)}" if missing else ""),
              llm_used=llm_used)
    if missing:
        audit.log("Orchestrator", "clarify", "info", "Stopping: asking requester for missing mandatory fields.")
        return _finish(result, audit)

    # 3. Policy check
    rules = policy.load_rules()
    pol = policy.evaluate(req, today)
    result.policy = pol
    audit.log("Policy Advisor", "evaluate_policy", _worst(pol.checks),
              "; ".join(f"{c.name}={c.status}" for c in pol.checks),
              evidence=[Evidence(source=f"policy:{c.clause_id}", detail=c.title) for c in pol.clauses])
    policy_blocked = any(c.status == "fail" for c in pol.checks)

    # 4. Planning & vendor matching (re-plans to an alternative when the first choice is infeasible)
    vm = vendor_matcher.match_vendors(req, rules)
    result.vendors = vm
    audit.log("Planner & Vendor Matcher", "match_vendors", "pass" if vm.selected else "fail",
              vm.explanation,
              evidence=[Evidence(source=f"data:vendors.json#{e.vendor_id}",
                                 detail=("eligible: " if e.eligible else "rejected: ") + "; ".join(e.reasons))
                        for e in vm.evaluations])
    if vm.replanned:
        audit.log("Orchestrator", "replan", "warn", "First-choice vendor infeasible; plan revised to an eligible alternative.")

    # 5-7. Costing, budget & approval, draft PO
    proc = procurement.build_draft_po(req, vm.selected, rules)
    if policy_blocked and proc.po is not None:
        proc.po = None
        proc.checks.append(CheckResult(
            name="policy_gate", status="fail",
            detail="Draft PO withheld: one or more mandatory policy checks failed. Human review required.",
        ))
    result.procurement = proc
    audit.log("Procurement", "build_draft_po", _worst(proc.checks),
              (f"Draft {proc.po.po_id} total INR {proc.po.total:,.2f}; approver: {proc.po.required_approver}"
               if proc.po else "No draft PO created.") + " | " + " ; ".join(proc.calculation_steps),
              evidence=[e for c in proc.checks for e in c.evidence])

    # 8. Invoice reconciliation
    if invoice_id:
        if proc.po is None:
            audit.log("Billing Guardian", "reconcile", "uncertain", "Skipped: no draft PO to reconcile against.")
        else:
            inv = billing.get_invoice(invoice_id)
            if inv is None:
                audit.log("Billing Guardian", "reconcile", "fail", f"Invoice {invoice_id} not found.")
            else:
                inv, delivery, rebound = billing.bind_demo_documents(
                    inv, billing.get_delivery(inv.delivery_id), proc.po)
                if rebound:
                    audit.log("Billing Guardian", "bind_demo_invoice", "info",
                              f"Synthetic invoice {inv.invoice_id} references placeholder "
                              f"{billing.DEMO_PO_PLACEHOLDER}; bound to {proc.po.po_id} for this ticket.")
                rec = billing.reconcile(inv, proc.po, delivery, billing.load_history(), rules)
                result.reconciliation = rec
                audit.log("Billing Guardian", "reconcile", "pass" if rec.status == "match" else "fail",
                          f"{rec.invoice_id}: {rec.status}; expected INR {rec.expected_total:,.2f}, "
                          f"invoiced INR {rec.invoiced_total:,.2f}. {rec.recommendation}",
                          evidence=[e for c in rec.checks for e in c.evidence])

    return _finish(result, audit)


def _finish(result: WorkflowResult, audit: _Audit) -> WorkflowResult:
    # 9. Critic review (read-only)
    report, llm_used = critic.review(result)
    result.critic = report
    result.final_status = report.status
    audit.log("Critic", "final_review", {"pass": "pass", "warning": "warn"}.get(report.status, "fail"),
              report.summary, llm_used=llm_used)
    # 10. Human approval gate
    approver = result.procurement.po.required_approver if result.procurement and result.procurement.po else "Requester / Operations lead"
    result.human_approval_required = True
    result.extra["approver"] = approver
    audit.log("Orchestrator", "human_approval_gate", "info",
              f"Awaiting human decision by {approver}. No purchase, PO dispatch or payment executed.")
    result.audit_trail = audit.entries
    return result
