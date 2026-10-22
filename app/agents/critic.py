"""Critic / Final Review agent: read-only review of the workflow result. Never modifies records."""
from __future__ import annotations

from app.ai_security import gateway
from app.schemas import CheckResult, CriticReport, Evidence, WorkflowResult


def review(result: WorkflowResult) -> tuple[CriticReport, bool]:
    findings: list[CheckResult] = []

    if result.missing_fields:
        findings.append(CheckResult(
            name="missing_information", status="fail",
            detail=f"Mandatory fields missing: {', '.join(result.missing_fields)}. Clarification required.",
        ))

    if result.policy:
        for c in result.policy.checks:
            if c.status in ("fail", "warn", "uncertain"):
                findings.append(CheckResult(name=f"policy:{c.name}", status=c.status, detail=c.detail, evidence=c.evidence))
            if c.status != "uncertain" and not any(e.source.startswith("policy:") for e in c.evidence):
                findings.append(CheckResult(
                    name=f"citation:{c.name}", status="warn",
                    detail="Policy conclusion has no clause citation.",
                ))

    if result.vendors is not None:
        if result.vendors.selected is None:
            findings.append(CheckResult(name="vendor", status="fail", detail=result.vendors.explanation or "No eligible vendor."))
        elif result.vendors.replanned:
            findings.append(CheckResult(
                name="vendor_replanned", status="pass",
                detail=f"Conflict detected and resolved: {result.vendors.explanation}",
            ))

    if result.procurement:
        for c in result.procurement.checks:
            if c.status in ("fail", "warn", "uncertain"):
                findings.append(CheckResult(name=f"procurement:{c.name}", status=c.status, detail=c.detail, evidence=c.evidence))
        po = result.procurement.po
        if po and result.request and result.request.budget_limit is not None and po.total > result.request.budget_limit:
            findings.append(CheckResult(name="budget_consistency", status="fail",
                                        detail="Draft PO total exceeds the request budget."))
        if po and not po.requires_human_approval:
            findings.append(CheckResult(name="approval_gate", status="fail",
                                        detail="Draft PO must require human approval."))

    if result.reconciliation:
        r = result.reconciliation
        if r.status != "match":
            findings.append(CheckResult(
                name="invoice", status="fail",
                detail=f"Invoice {r.invoice_id} status '{r.status}' (variance INR {r.variance:,.2f}). {r.recommendation}",
                evidence=[e for c in r.checks if c.status == "fail" for e in c.evidence],
            ))

    statuses = {f.status for f in findings}
    if result.missing_fields:
        status = "needs_clarification"
    elif "fail" in statuses:
        status = "blocked"
    elif statuses & {"warn", "uncertain"}:
        status = "warning"
    else:
        status = "pass"

    issues = [f for f in findings if f.status != "pass"]
    fallback = (
        "No unresolved issues. Draft PO is ready for human approval."
        if not issues else
        f"{len(issues)} issue(s) need attention: " + "; ".join(f"{f.name} ({f.status})" for f in issues[:5]) + "."
    )
    summary = fallback
    llm_used = False
    resolved = [f for f in findings if f.status == "pass"]
    res = gateway.run_task(
        "critic_review_summary",
        "You are a strict, concise finance-operations reviewer. Summarize in 2-3 sentences. "
        "Only UNRESOLVED issues can explain a blocked/warning status; RESOLVED items are context only. "
        "Do not add facts that are not listed. Do not change the status. Write plain prose; "
        "do not repeat section labels or the words UNRESOLVED/RESOLVED.",
        f"Overall status: {status}\nUNRESOLVED issues:\n"
        + ("\n".join(f"- [{f.status}] {f.name}: {f.detail}" for f in issues) or "- none")
        + "\nRESOLVED (context only):\n"
        + ("\n".join(f"- {f.name}: {f.detail}" for f in resolved) or "- none"),
    )
    if res.ok and res.output is not None:
        summary, llm_used = res.output.text, True

    return CriticReport(status=status, findings=findings, summary=summary), llm_used


__all__ = ["review", "Evidence"]
