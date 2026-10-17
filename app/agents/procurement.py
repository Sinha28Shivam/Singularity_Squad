"""Procurement agent: deterministic cost calculation and draft PO creation (no LLM)."""
from __future__ import annotations

import re

from app.schemas import (
    CheckResult,
    DraftPO,
    EventRequest,
    Evidence,
    LineItem,
    PolicyRules,
    ProcurementResult,
    Vendor,
)


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def _qty(q: float) -> str:
    return str(int(q)) if float(q).is_integer() else str(q)


def calculate_cost(
    vendor: Vendor, attendees: int, service: str | None = None
) -> tuple[list[LineItem], float, float, float, list[str]]:
    """Return (line_items, subtotal, tax, total, calculation_steps)."""
    svc = service or (vendor.services[0] if vendor.services else "catering")
    title = svc.replace("_", " ").title()
    items: list[LineItem] = [
        LineItem(
            description=title,
            quantity=attendees,
            unit_price=_r(vendor.rate_per_person),
            line_total=_r(attendees * vendor.rate_per_person),
        )
    ]
    if vendor.delivery_setup_fee:
        items.append(
            LineItem(
                description="Delivery & setup",
                quantity=1,
                unit_price=_r(vendor.delivery_setup_fee),
                line_total=_r(vendor.delivery_setup_fee),
            )
        )
    steps = [
        f"{li.description}: {_qty(li.quantity)} x {_fmt(li.unit_price)} = {_fmt(li.line_total)}"
        for li in items
    ]
    subtotal = _r(sum(li.line_total for li in items))
    tax = _r(subtotal * vendor.gst_rate)
    total = _r(subtotal + tax)
    steps.append(f"Subtotal: {' + '.join(_fmt(li.line_total) for li in items)} = {_fmt(subtotal)}")
    steps.append(f"GST @ {vendor.gst_rate * 100:g}%: {_fmt(subtotal)} x {vendor.gst_rate:g} = {_fmt(tax)}")
    steps.append(f"Total: {_fmt(subtotal)} + {_fmt(tax)} = {_fmt(total)}")
    return items, subtotal, tax, total, steps


def required_approver(total: float, rules: PolicyRules) -> str:
    """Approver tier per PROC-4.1 (tiers are inclusive upper bounds)."""
    for tier in rules.approval_tiers:
        if tier.max_amount is None or total <= tier.max_amount:
            return tier.approver
    return rules.approval_tiers[-1].approver


def _po_id(request_id: str) -> str:
    # Unique ticket IDs (REQ-20261017-9F3A1C) must map 1:1 to PO IDs, so keep the whole suffix.
    if request_id and request_id.startswith("REQ-") and len(request_id) > 4:
        return "DRAFT-PO-" + request_id[4:]
    m = re.search(r"(\d+)\s*$", request_id or "")
    return "DRAFT-PO-" + (m.group(1) if m else (request_id or "0000"))


def build_draft_po(req: EventRequest, vendor: Vendor | None, rules: PolicyRules) -> ProcurementResult:
    checks: list[CheckResult] = []
    if vendor is None:
        checks.append(
            CheckResult(
                name="vendor_selected",
                status="fail",
                detail="No eligible vendor selected; draft PO cannot be created.",
                evidence=[Evidence(source="policy:PROC-3.2", detail="Vendor selection required")],
            )
        )
        return ProcurementResult(po=None, checks=checks, calculation_steps=[])

    checks.append(
        CheckResult(
            name="vendor_selected",
            status="pass",
            detail=f"Selected vendor {vendor.name} ({vendor.vendor_id}).",
            evidence=[Evidence(source=f"data:vendors.json#{vendor.vendor_id}", detail=vendor.name)],
        )
    )
    attendees = req.attendees or 0
    items, subtotal, tax, total, steps = calculate_cost(vendor, attendees, req.service)

    budget = req.budget_limit
    budget_ok = True
    if budget is None:
        checks.append(
            CheckResult(
                name="budget",
                status="warn",
                detail=f"No budget limit provided; estimated total {req.currency} {_fmt(total)}.",
                evidence=[Evidence(source="policy:EVT-3.3", detail="Budget ceiling not specified")],
            )
        )
    elif total <= budget:
        checks.append(
            CheckResult(
                name="budget",
                status="pass",
                detail=f"Total {req.currency} {_fmt(total)} is within budget {req.currency} {_fmt(budget)} "
                f"(headroom {_fmt(_r(budget - total))}).",
                evidence=[
                    Evidence(source="policy:EVT-3.3", detail="Total must not exceed approved budget"),
                    Evidence(source="calc", detail=f"{_fmt(total)} <= {_fmt(budget)}"),
                ],
            )
        )
    else:
        budget_ok = False
        over = _r(total - budget)
        checks.append(
            CheckResult(
                name="budget",
                status="fail",
                detail=f"Total {req.currency} {_fmt(total)} exceeds budget {req.currency} {_fmt(budget)} "
                f"by {req.currency} {_fmt(over)}; no draft PO created.",
                evidence=[
                    Evidence(source="policy:EVT-3.3", detail="Total must not exceed approved budget"),
                    Evidence(source="calc", detail=f"{_fmt(total)} - {_fmt(budget)} = {_fmt(over)} overage"),
                ],
            )
        )

    approver = required_approver(total, rules)
    checks.append(
        CheckResult(
            name="approval_tier",
            status="pass",
            detail=f"Total {req.currency} {_fmt(total)} requires approval by {approver}.",
            evidence=[Evidence(source="policy:PROC-4.1", detail=f"Approver: {approver}")],
        )
    )

    po = None
    if budget_ok:
        if req.date is None:
            checks.append(
                CheckResult(name="event_date", status="fail", detail="Event date missing; no draft PO created.")
            )
        else:
            po = DraftPO(
                po_id=_po_id(req.request_id),
                request_id=req.request_id,
                vendor_id=vendor.vendor_id,
                vendor_name=vendor.name,
                event_date=req.date,
                line_items=items,
                subtotal=subtotal,
                tax=tax,
                total=total,
                currency=req.currency,
                required_approver=approver,
                requires_human_approval=True,
            )
    return ProcurementResult(po=po, checks=checks, calculation_steps=steps)
