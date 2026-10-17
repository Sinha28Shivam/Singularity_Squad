"""Billing agent: deterministic three-way invoice reconciliation (no LLM, no payment execution)."""
from __future__ import annotations

from app.data_loader import load_json
from app.schemas import CheckResult, DeliveryRecord, DraftPO, Evidence, Invoice, PolicyRules, ReconciliationReport


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def _fmt(x: float) -> str:
    return f"{x:,.2f}"


def _q(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else str(x)


def load_invoices() -> list[Invoice]:
    return [Invoice(**i) for i in load_json("invoices.json")]


def load_deliveries() -> list[DeliveryRecord]:
    return [DeliveryRecord(**d) for d in load_json("deliveries.json")]


def load_history() -> list[dict]:
    return list(load_json("invoice_history.json"))


def get_invoice(invoice_id: str) -> Invoice | None:
    return next((i for i in load_invoices() if i.invoice_id == invoice_id), None)


def get_delivery(delivery_id: str) -> DeliveryRecord | None:
    return next((d for d in load_deliveries() if d.delivery_id == delivery_id), None)


def _key(s: str) -> str:
    return s.strip().lower()


def reconcile(
    invoice: Invoice,
    po: DraftPO,
    delivery: DeliveryRecord | None,
    history: list[dict] | None,
    rules: PolicyRules,
) -> ReconciliationReport:
    checks: list[CheckResult] = []
    inv_src = f"data:invoices.json#{invoice.invoice_id}"
    cur = invoice.currency
    dispute: list[str] = []

    # Duplicate check (FIN-5.4)
    history = load_history() if history is None else history
    dup = next(
        (h for h in history if h.get("invoice_id") == invoice.invoice_id and h.get("vendor_id") == invoice.vendor_id),
        None,
    )
    if dup:
        checks.append(CheckResult(
            name="duplicate_check", status="fail",
            detail=f"Invoice {invoice.invoice_id} from {invoice.vendor_id} already recorded in history "
                   f"(PO {dup.get('po_id')}, status {dup.get('status')}).",
            evidence=[Evidence(source="policy:FIN-5.4", detail="Duplicate invoices must be rejected"),
                      Evidence(source=f"data:invoice_history.json#{invoice.invoice_id}", detail=str(dup))],
        ))
    else:
        checks.append(CheckResult(
            name="duplicate_check", status="pass",
            detail=f"Invoice {invoice.invoice_id} not found in payment history.",
            evidence=[Evidence(source="policy:FIN-5.4", detail="No prior invoice with same id and vendor")],
        ))

    # Vendor / PO match (FIN-5.1)
    vp_ok = invoice.vendor_id == po.vendor_id and invoice.po_id == po.po_id
    checks.append(CheckResult(
        name="vendor_po_match", status="pass" if vp_ok else "fail",
        detail=f"Invoice vendor {invoice.vendor_id} / PO {invoice.po_id} vs PO vendor {po.vendor_id} / PO {po.po_id}.",
        evidence=[Evidence(source="policy:FIN-5.1", detail="Invoice must match PO and delivery"),
                  Evidence(source=inv_src, detail=f"vendor {invoice.vendor_id}, po {invoice.po_id}")],
    ))
    if not vp_ok:
        dispute.append("vendor/PO reference mismatch")

    # Line items vs PO (FIN-5.1 / FIN-5.3)
    inv_lines = {_key(li.description): li for li in invoice.line_items}
    po_keys = set()
    overcharge_total = 0.0
    for pl in po.line_items:
        k = _key(pl.description)
        po_keys.add(k)
        il = inv_lines.get(k)
        name = f"line_item:{pl.description}"
        if il is None:
            checks.append(CheckResult(name=name, status="warn", detail=f"PO line '{pl.description}' not billed.",
                                      evidence=[Evidence(source="policy:FIN-5.1", detail="Line not on invoice")]))
            continue
        problems = []
        if abs(il.quantity - pl.quantity) > 1e-9:
            problems.append(f"quantity {_q(il.quantity)} vs PO {_q(pl.quantity)}")
        if abs(il.unit_price - pl.unit_price) > 0.005:
            diff = _r((il.unit_price - pl.unit_price) * il.quantity)
            overcharge_total += diff
            problems.append(f"unit price {_fmt(il.unit_price)} vs PO {_fmt(pl.unit_price)} "
                            f"({cur} {_fmt(diff)} difference)")
        checks.append(CheckResult(
            name=name, status="fail" if problems else "pass",
            detail=(f"{pl.description}: " + "; ".join(problems)) if problems
            else f"{pl.description}: {_q(il.quantity)} x {_fmt(il.unit_price)} matches PO.",
            evidence=[Evidence(source="policy:FIN-5.3" if problems else "policy:FIN-5.1",
                               detail="Unit prices must match PO"),
                      Evidence(source=inv_src, detail=f"{il.description} {_q(il.quantity)} x {_fmt(il.unit_price)}"),
                      Evidence(source=f"po:{po.po_id}", detail=f"{pl.description} {_q(pl.quantity)} x {_fmt(pl.unit_price)}")],
        ))
    overcharge_total = _r(overcharge_total)
    if overcharge_total:
        dispute.append(f"{cur} {_fmt(overcharge_total)} unit-price overcharge")
    if any(c.status == "fail" and c.name.startswith("line_item:") and "quantity" in c.detail for c in checks):
        dispute.append("quantity mismatch vs PO")
    extras = [li for k, li in inv_lines.items() if k not in po_keys]
    for li in extras:
        checks.append(CheckResult(
            name=f"unexpected_line:{li.description}", status="fail",
            detail=f"Invoice line '{li.description}' ({cur} {_fmt(li.line_total)}) is not on the PO.",
            evidence=[Evidence(source="policy:FIN-5.1", detail="Only PO lines may be billed"),
                      Evidence(source=inv_src, detail=li.description)],
        ))
    if extras:
        dispute.append(f"{len(extras)} unexpected invoice line(s)")

    # Arithmetic check
    arith_issues = []
    for li in invoice.line_items:
        exp = _r(li.quantity * li.unit_price)
        if abs(exp - li.line_total) > 0.01:
            arith_issues.append(f"{li.description}: {_q(li.quantity)} x {_fmt(li.unit_price)} = {_fmt(exp)}, "
                                f"billed {_fmt(li.line_total)}")
    sub = _r(sum(li.line_total for li in invoice.line_items))
    if abs(sub - invoice.subtotal) > 0.01:
        arith_issues.append(f"subtotal recomputed {_fmt(sub)} vs billed {_fmt(invoice.subtotal)}")
    tot = _r(invoice.subtotal + invoice.tax)
    if abs(tot - invoice.total) > 0.01:
        arith_issues.append(f"total recomputed {_fmt(tot)} vs billed {_fmt(invoice.total)}")
    checks.append(CheckResult(
        name="arithmetic_check", status="fail" if arith_issues else "pass",
        detail="; ".join(arith_issues) if arith_issues
        else f"Line totals, subtotal {_fmt(sub)} and total {_fmt(tot)} recomputed correctly.",
        evidence=[Evidence(source="calc", detail=f"subtotal {_fmt(sub)}; subtotal + tax = {_fmt(tot)}")],
    ))
    if arith_issues:
        dispute.append("invoice arithmetic errors")

    # Delivery check (FIN-5.2)
    short_units = 0.0
    if delivery is None:
        checks.append(CheckResult(
            name="delivery_check", status="fail",
            detail=f"No delivery record found for {invoice.delivery_id}.",
            evidence=[Evidence(source="policy:FIN-5.2", detail="Signed delivery evidence required")],
        ))
        dispute.append("missing delivery evidence")
    else:
        delivered = {_key(k): v for k, v in delivery.items.items()}
        issues = []
        for li in invoice.line_items:
            d = delivered.get(_key(li.description), 0.0)
            if li.quantity > d:
                short_units += li.quantity - d
                issues.append(f"{li.description}: billed {_q(li.quantity)} vs delivered {_q(d)}")
        ref_ok = delivery.po_id == invoice.po_id and delivery.vendor_id == invoice.vendor_id
        if not ref_ok:
            issues.append(f"delivery {delivery.delivery_id} references PO {delivery.po_id}/vendor {delivery.vendor_id}")
        checks.append(CheckResult(
            name="delivery_check", status="fail" if issues else "pass",
            detail="; ".join(issues) if issues
            else f"All billed quantities delivered ({delivery.delivery_id}, signed by {delivery.signed_by}).",
            evidence=[Evidence(source="policy:FIN-5.2", detail="Billed qty must not exceed delivered qty"),
                      Evidence(source=f"data:deliveries.json#{delivery.delivery_id}",
                               detail=f"{delivery.items}; {delivery.notes}")],
        ))
        if short_units:
            dispute.append(f"{_q(short_units)} undelivered unit(s)")
        if not ref_ok:
            dispute.append("delivery reference mismatch")

    # Total variance (FIN-5.3)
    expected = _r(po.total)
    invoiced = _r(invoice.total)
    variance = _r(invoiced - expected)
    tol = rules.invoice_amount_tolerance
    checks.append(CheckResult(
        name="total_variance", status="pass" if abs(variance) <= tol else "fail",
        detail=f"Invoiced {cur} {_fmt(invoiced)} vs PO {cur} {_fmt(expected)}: variance {_fmt(variance)} "
               f"(tolerance {_fmt(tol)}).",
        evidence=[Evidence(source="policy:FIN-5.3", detail=f"Tolerance {cur} {_fmt(tol)}"),
                  Evidence(source="calc", detail=f"{_fmt(invoiced)} - {_fmt(expected)} = {_fmt(variance)}")],
    ))

    if dup:
        status = "duplicate"
        rec = (f"Reject invoice {invoice.invoice_id}: duplicate of previously {dup.get('status', 'recorded')} "
               f"invoice (FIN-5.4). Do not pay; notify vendor and Finance.")
    elif any(c.status == "fail" for c in checks):
        status = "mismatch"
        items = dispute or [f"{cur} {_fmt(variance)} total variance"]
        rec = f"Hold payment; dispute {', '.join(items)} with vendor. Escalate to Finance."
    else:
        status = "match"
        rec = "Ready for human payment approval (no payment executed by system)."

    return ReconciliationReport(
        invoice_id=invoice.invoice_id, status=status, checks=checks,
        expected_total=expected, invoiced_total=invoiced, variance=variance, recommendation=rec,
    )
