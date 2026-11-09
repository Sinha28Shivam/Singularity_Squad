"""Deterministic, structured "why" explanations for workflow decisions (Phase 5 DecisionExplanation contract).

Built only from persisted records (workflow_result checks, the post-approval invoice match and its exception,
policy status). No LLM is involved, so an explanation can never contradict or change an outcome. Every factor
carries the evidence references of the check it was derived from.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

_PROBLEM = {"fail", "warn", "uncertain"}
_OUTCOME = {"pass": "pass", "warn": "warning", "fail": "fail", "uncertain": "warning"}


class ExplanationFactor(BaseModel):
    label: str
    status: str                     # pass | warn | fail | uncertain | info
    detail: str
    evidence: list[str] = Field(default_factory=list)


class DecisionExplanation(BaseModel):
    topic: str                      # policy | vendor | cost | invoice | final
    title: str
    outcome: str                    # pass | warning | fail | not_run | blocked | needs_clarification
    summary: str
    factors: list[ExplanationFactor] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    policy_set_version: Optional[int] = None


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _label(name: str) -> str:
    name = str(name or "check")
    for prefix, nice in (("line_item:", "Line item: "), ("unexpected_line:", "Unexpected line: ")):
        if name.startswith(prefix):
            return nice + name[len(prefix):]
    return name.replace("_", " ").capitalize()


def _factors(checks: Any, *, problems_first: bool = True, limit: int = 12) -> list[ExplanationFactor]:
    out = []
    for c in checks or []:
        if not isinstance(c, dict):
            continue
        refs = [str(e.get("source")) for e in c.get("evidence") or [] if isinstance(e, dict) and e.get("source")]
        out.append(ExplanationFactor(label=_label(c.get("name")), status=str(c.get("status") or "info"),
                                     detail=str(c.get("detail") or "")[:500], evidence=refs[:6]))
    if problems_first:
        out.sort(key=lambda f: 0 if f.status == "fail" else 1 if f.status in _PROBLEM else 2)
    return out[:limit]


def _worst(factors: list[ExplanationFactor]) -> str:
    statuses = {f.status for f in factors}
    if "fail" in statuses:
        return "fail"
    if statuses & {"warn", "uncertain"}:
        return "warning"
    return "pass" if statuses else "not_run"


def _money(value: Any, cur: str = "INR") -> str:
    try:
        return f"{cur} {float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _policy(wr: dict, policy_status: Optional[dict]) -> DecisionExplanation:
    policy = _d(wr.get("policy"))
    if not policy:
        return DecisionExplanation(topic="policy", title="Policy check", outcome="not_run",
                                   summary="The policy check didn't run.")
    factors = _factors(policy.get("checks"))
    ps = _d(policy_status)
    for code, reason in zip(ps.get("reason_codes") or [], ps.get("reasons") or []):
        factors.append(ExplanationFactor(label=f"Approval rule {code}", status="fail" if ps.get("blocked") else "info",
                                         detail=str(reason)[:500]))
    outcome = "blocked" if ps.get("blocked") else "needs_clarification" if ps.get("needs_clarification") \
        else _worst(factors)
    failed = [f.label for f in factors if f.status == "fail"]
    summary = (f"Blocked by: {', '.join(failed)}." if failed and outcome in {"fail", "blocked"}
               else str(policy.get("summary") or "All policy checks passed."))
    clauses = [c.get("clause_id") for c in policy.get("clauses") or [] if isinstance(c, dict) and c.get("clause_id")]
    lim = ["Rules are evaluated deterministically; retrieved policy text is shown for reference only."]
    if clauses:
        lim.append(f"Cited clauses: {', '.join(clauses[:8])}.")
    return DecisionExplanation(topic="policy", title="Policy check", outcome=outcome, summary=summary[:600],
                               factors=factors, limitations=lim,
                               policy_set_version=ps.get("policy_set_version"))


def _vendor(wr: dict) -> DecisionExplanation:
    vendors = _d(wr.get("vendors"))
    if not vendors:
        return DecisionExplanation(topic="vendor", title="Vendor choice", outcome="not_run",
                                   summary="Vendor matching didn't run.")
    sel = _d(vendors.get("selected"))
    factors = []
    for ev in vendors.get("evaluations") or []:
        if not isinstance(ev, dict):
            continue
        chosen = sel and ev.get("vendor_id") == sel.get("vendor_id")
        est = f" Estimated {_money(ev.get('estimated_total'))}." if ev.get("estimated_total") is not None else ""
        factors.append(ExplanationFactor(
            label=f"{ev.get('name') or ev.get('vendor_id')}{' (selected)' if chosen else ''}",
            status="pass" if ev.get("eligible") else "fail",
            detail=("; ".join(str(r) for r in ev.get("reasons") or []) or "—")[:500] + est,
            evidence=[f"vendor:{ev.get('vendor_id')}"] if ev.get("vendor_id") else []))
    factors.sort(key=lambda f: (0 if "(selected)" in f.label else 1, f.status != "pass"))
    if not sel:
        return DecisionExplanation(topic="vendor", title="Vendor choice", outcome="fail",
                                   summary="No eligible vendor was found.", factors=factors[:12])
    summary = f"Selected {sel.get('name') or sel.get('vendor_id')}."
    if vendors.get("replanned"):
        summary += " The preferred vendor was rejected, so the workflow re-planned."
    if vendors.get("explanation"):
        summary += f" {vendors['explanation']}"
    return DecisionExplanation(
        topic="vendor", title="Vendor choice", outcome="warning" if vendors.get("replanned") else "pass",
        summary=summary[:600], factors=factors[:12],
        limitations=["Vendors are filtered by hard eligibility rules (approved, service, capacity, date) and then "
                     "ranked by cost and rating. No vendor is contacted or awarded."])


def _cost(wr: dict) -> DecisionExplanation:
    proc = _d(wr.get("procurement"))
    if not proc:
        return DecisionExplanation(topic="cost", title="Cost & draft PO", outcome="not_run",
                                   summary="The cost check didn't run.")
    po = _d(proc.get("po"))
    factors = _factors(proc.get("checks"))
    steps = [str(s) for s in proc.get("calculation_steps") or []][:10]
    for i, s in enumerate(steps, 1):
        factors.append(ExplanationFactor(label=f"Calculation step {i}", status="info", detail=s[:500],
                                         evidence=["calc"]))
    summary = (f"Draft PO {po.get('po_id')} for {_money(po.get('total'), po.get('currency') or 'INR')} "
               f"with {po.get('vendor_name') or 'the selected vendor'}." if po
               else "No draft PO was created.")
    outcome = _worst([f for f in factors if f.status != "info"]) if po else "fail"
    return DecisionExplanation(topic="cost", title="Cost & draft PO", outcome=outcome, summary=summary,
                               factors=factors[:16],
                               limitations=["All arithmetic is computed deterministically (Decimal-safe); "
                                            "the PO stays a draft and is never sent."])


def _invoice(doc: dict, wr: dict) -> DecisionExplanation:
    recon = _d(wr.get("reconciliation"))
    match = _d(doc.get("invoice_match"))
    rec = recon or (match if match.get("post_approval") else {})
    if not rec:
        return DecisionExplanation(topic="invoice", title="Invoice check", outcome="not_run",
                                   summary="No invoice has been checked yet.")
    cur = rec.get("currency") or "INR"
    factors = _factors(rec.get("checks"))
    outcome = {"match": "pass", "mismatch": "fail", "duplicate": "fail"}.get(rec.get("status"), "warning")
    summary = (f"Invoice {rec.get('invoice_id')}: {str(rec.get('status')).replace('_', ' ')}. Invoiced "
               f"{_money(rec.get('invoiced_total'), cur)} vs draft PO "
               f"{_money(rec.get('expected_total', rec.get('po_total')), cur)}.")
    if rec.get("recommendation"):
        summary += f" {rec['recommendation']}"
    exc = _d(doc.get("invoice_exception"))
    if not recon and exc.get("invoice_id") == rec.get("invoice_id") and exc.get("state"):
        state = str(exc["state"]).replace("_", " ")
        factors.insert(0, ExplanationFactor(label=f"Invoice {state}", status="fail" if exc["state"] == "rejected"
                                            else "warn", detail=str(exc.get("reason") or "")[:500],
                                            evidence=[f"audit:invoice_exception_{exc['state']}"]))
        if outcome == "pass":
            outcome = "warning"
    return DecisionExplanation(
        topic="invoice", title="Invoice check", outcome=outcome, summary=summary[:600], factors=factors,
        limitations=["3-way match: invoice vs draft PO vs signed delivery record, plus the duplicate check "
                     "against payment history. Payment remains simulation-only."])


def _final(wr: dict) -> DecisionExplanation:
    critic = _d(wr.get("critic"))
    final = str(wr.get("final_status") or "needs_clarification")
    outcome = {"pass": "pass", "warning": "warning", "blocked": "blocked"}.get(final, "needs_clarification")
    factors = _factors(critic.get("findings"))
    for q in wr.get("clarification_questions") or []:
        factors.append(ExplanationFactor(label="Open question", status="warn", detail=str(q)[:500]))
    return DecisionExplanation(topic="final", title="Final review", outcome=outcome,
                               summary=str(critic.get("summary") or f"Final status: {final}.")[:600],
                               factors=factors[:16],
                               limitations=["A human approver makes the decision; this review only advises."])


def build_explanations(doc: dict, policy_status: Optional[dict] = None) -> list[DecisionExplanation]:
    """Explanations for a ticket document (admin view). Empty when the workflow hasn't run."""
    wr = _d(doc.get("workflow_result"))
    if not wr:
        return []
    return [_policy(wr, policy_status), _vendor(wr), _cost(wr), _invoice(doc, wr), _final(wr)]
