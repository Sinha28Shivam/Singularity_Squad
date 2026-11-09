"""Read-only spend analytics over stored tickets, approval requests and simulated payments.

Only structured workflow fields are read (never ``raw_text``, owner contact details or notes). Money is summed
with ``Decimal`` and returned as 2dp strings. Nothing here writes to the database.
"""
from __future__ import annotations

import csv
import io
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from statistics import median
from typing import Any, Iterable, Optional

from app.db import get_database

MAX_TICKETS = 20_000
ACCEPTED_PAYMENT = "accepted_by_simulator"
CSV_COLUMNS = ["ticket_id", "category", "event_date", "vendor_id", "po_id", "po_total", "currency", "status",
               "invoice_status", "simulated_payment_status"]
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# Allow-list projection: PII (raw_text, owner_*, notes) is never loaded.
_TICKET_PROJECTION = {
    "_id": 0, "ticket_id": 1, "status": 1, "created_at": 1,
    "submission.event_category": 1, "submission.service": 1, "submission.event_date": 1,
    "submission.budget": 1, "submission.budget_inr": 1,
    "workflow_result.final_status": 1, "workflow_result.procurement": 1, "workflow_result.reconciliation": 1,
    "workflow_result.invoice_match": 1, "workflow_result.critic": 1, "workflow_result.policy.checks": 1,
}


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _utc(dt: Any) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _range_filter(field: str, start: Optional[date], end: Optional[date]) -> dict:
    cond: dict = {}
    if start:
        cond["$gte"] = datetime.combine(start, time.min, tzinfo=timezone.utc)
    if end:
        cond["$lt"] = datetime.combine(end + timedelta(days=1), time.min, tzinfo=timezone.utc)
    return {field: cond} if cond else {}


def _load_tickets(start: Optional[date], end: Optional[date], extra: Optional[dict] = None) -> list[dict]:
    query = {**_range_filter("created_at", start, end), **(extra or {})}
    cur = get_database().tickets.find(query, _TICKET_PROJECTION).sort("created_at", 1).limit(MAX_TICKETS)
    return list(cur)


def _wr(t: dict) -> dict:
    wr = t.get("workflow_result")
    return wr if isinstance(wr, dict) else {}


def _po(t: dict) -> Optional[dict]:
    po = (_wr(t).get("procurement") or {}).get("po")
    return po if isinstance(po, dict) else None


def _category(t: dict) -> str:
    sub = t.get("submission") or {}
    return str(sub.get("event_category") or sub.get("service") or "uncategorized")


def _budget(t: dict) -> Optional[Decimal]:
    sub = t.get("submission") or {}
    for key in ("budget", "budget_inr"):
        d = _dec(sub.get(key))
        if d is not None:
            return d
    return None


def invoice_status(t: dict) -> Optional[str]:
    wr = _wr(t)
    for key in ("reconciliation", "invoice_match"):
        block = wr.get(key)
        if isinstance(block, dict) and block.get("status"):
            return str(block["status"])
    return None


def block_codes(t: dict) -> list[str]:
    """Reason codes for a blocked workflow: failing critic findings, else failing policy/procurement checks."""
    wr = _wr(t)
    if wr.get("final_status") != "blocked":
        return []
    findings = (wr.get("critic") or {}).get("findings") or []
    codes = [f.get("name") for f in findings if isinstance(f, dict) and f.get("status") == "fail"]
    if not codes:
        checks = list((wr.get("policy") or {}).get("checks") or [])
        checks += list((wr.get("procurement") or {}).get("checks") or [])
        codes = [c.get("name") for c in checks if isinstance(c, dict) and c.get("status") == "fail"]
    codes = [str(c) for c in codes if c]
    return list(dict.fromkeys(codes)) or ["blocked_unspecified"]


def _hours(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if a is None or b is None or b < a:
        return None
    return (b - a).total_seconds() / 3600.0


def _median(values: Iterable[float]) -> Optional[float]:
    vals = list(values)
    return round(median(vals), 2) if vals else None


def _approval_times(ticket_ids: list[str]) -> dict[str, datetime]:
    """ticket_id -> earliest approved decision time."""
    out: dict[str, datetime] = {}
    if not ticket_ids:
        return out
    cur = get_database().approval_requests.find(
        {"ticket_id": {"$in": ticket_ids}, "status": "approved"}, {"_id": 0, "ticket_id": 1, "decided_at": 1})
    for a in cur:
        at = _utc(a.get("decided_at"))
        tid = a.get("ticket_id")
        if at and tid and (tid not in out or at < out[tid]):
            out[tid] = at
    return out


def _payments_for(ticket_ids: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    if not ticket_ids:
        return out
    cur = get_database().payment_simulations.find(
        {"ticket_id": {"$in": ticket_ids}},
        {"_id": 0, "ticket_id": 1, "status": 1, "created_at": 1, "updated_at": 1})
    for p in cur:
        out[p.get("ticket_id")].append(p)
    for plist in out.values():
        plist.sort(key=lambda p: _utc(p.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc))
    return out


def _currency(currencies: set[str]) -> str:
    if len(currencies) == 1:
        return next(iter(currencies))
    return "MIXED" if currencies else "INR"


def summary(start: Optional[date] = None, end: Optional[date] = None) -> dict:
    tickets = _load_tickets(start, end)
    by_status = Counter(str(t.get("status") or "unknown") for t in tickets)
    approved = [t for t in tickets if t.get("status") == "approved"]

    total = Decimal("0")
    currencies: set[str] = set()
    vendors: dict[str, dict] = {}
    cats: dict[str, dict] = {}
    months: dict[str, dict] = {}
    budget_rows: dict[str, dict] = {}
    for t in approved:
        po = _po(t)
        amt = _dec(po.get("total")) if po else None
        cat = _category(t)
        b = budget_rows.setdefault(cat, {"budget": Decimal("0"), "po": Decimal("0"), "has_budget": False})
        bud = _budget(t)
        if bud is not None:
            b["budget"] += bud
            b["has_budget"] = True
        if amt is None:
            continue
        total += amt
        b["po"] += amt
        if po.get("currency"):
            currencies.add(str(po["currency"]))
        vid = str(po.get("vendor_id") or "unknown")
        v = vendors.setdefault(vid, {"vendor_id": vid, "vendor_name": po.get("vendor_name") or vid,
                                     "total": Decimal("0"), "count": 0})
        v["total"] += amt
        v["count"] += 1
        c = cats.setdefault(cat, {"category": cat, "total": Decimal("0"), "count": 0})
        c["total"] += amt
        c["count"] += 1
        created = _utc(t.get("created_at"))
        month = created.strftime("%Y-%m") if created else "unknown"
        m = months.setdefault(month, {"month": month, "total": Decimal("0"), "count": 0})
        m["total"] += amt
        m["count"] += 1

    def ser(rows: Iterable[dict]) -> list[dict]:
        return [{**r, "total": money(r["total"])} for r in rows]

    with_recon = [s for s in (invoice_status(t) for t in tickets) if s is not None]
    exceptions = sum(1 for s in with_recon if s != "match")

    blocks = Counter(code for t in tickets for code in block_codes(t))

    pay_query = {"status": ACCEPTED_PAYMENT, **_range_filter("created_at", start, end)}
    pay_total = Decimal("0")
    pay_count = 0
    for p in get_database().payment_simulations.find(pay_query, {"_id": 0, "amount": 1}):
        pay_count += 1
        pay_total += _dec(p.get("amount")) or Decimal("0")

    approved_ids = [t["ticket_id"] for t in approved if t.get("ticket_id")]
    approvals = _approval_times(approved_ids)
    payments = _payments_for(approved_ids)
    submit_to_approve, approve_to_pay = [], []
    for t in approved:
        tid = t.get("ticket_id")
        appr_at = approvals.get(tid)
        h = _hours(_utc(t.get("created_at")), appr_at)
        if h is not None:
            submit_to_approve.append(h)
        accepted = [p for p in payments.get(tid, []) if p.get("status") == ACCEPTED_PAYMENT]
        if accepted:
            h = _hours(appr_at, _utc(accepted[0].get("created_at")))
            if h is not None:
                approve_to_pay.append(h)

    return {
        "from": start.isoformat() if start else None,
        "to": end.isoformat() if end else None,
        "tickets_by_status": dict(sorted(by_status.items())),
        "total_requests": len(tickets),
        "approved_count": len(approved),
        "draft_po_total": money(total),
        "currency": _currency(currencies),
        "spend_by_vendor": ser(sorted(vendors.values(), key=lambda r: (-r["total"], r["vendor_id"]))),
        "spend_by_category": ser(sorted(cats.values(), key=lambda r: (-r["total"], r["category"]))),
        "spend_by_month": ser(sorted(months.values(), key=lambda r: r["month"])),
        "budget_vs_actual": [
            {"category": cat, "budget_total": money(r["budget"]) if r["has_budget"] else None,
             "po_total": money(r["po"])}
            for cat, r in sorted(budget_rows.items())
        ],
        "invoice_exception_rate": round(exceptions / len(with_recon), 4) if with_recon else None,
        "invoice_exceptions": exceptions,
        "invoices_reconciled": len(with_recon),
        "top_policy_blocks": [{"code": c, "count": n}
                              for c, n in sorted(blocks.items(), key=lambda kv: (-kv[1], kv[0]))[:10]],
        "simulated_payments": {"count": pay_count, "total": money(pay_total)},
        "cycle_time_hours": {
            "submit_to_approve_median": _median(submit_to_approve),
            "approve_to_payment_sim_median": _median(approve_to_pay),
        },
    }


def csv_safe(value: Any) -> str:
    """Stringify a cell and neutralise spreadsheet formula injection."""
    s = "" if value is None else str(value)
    return "'" + s if s.startswith(_FORMULA_PREFIXES) else s


def export_rows(start: Optional[date] = None, end: Optional[date] = None) -> list[dict]:
    approved = _load_tickets(start, end, {"status": "approved"})
    payments = _payments_for([t["ticket_id"] for t in approved if t.get("ticket_id")])
    rows = []
    for t in approved:
        po = _po(t) or {}
        amt = _dec(po.get("total"))
        plist = payments.get(t.get("ticket_id"), [])
        accepted = [p for p in plist if p.get("status") == ACCEPTED_PAYMENT]
        pay = accepted[-1] if accepted else (plist[-1] if plist else None)
        rows.append({
            "ticket_id": t.get("ticket_id"),
            "category": _category(t),
            "event_date": po.get("event_date") or (t.get("submission") or {}).get("event_date"),
            "vendor_id": po.get("vendor_id"),
            "po_id": po.get("po_id"),
            "po_total": money(amt) if amt is not None else None,
            "currency": po.get("currency"),
            "status": t.get("status"),
            "invoice_status": invoice_status(t),
            "simulated_payment_status": pay.get("status") if pay else None,
        })
    return rows


def export_csv(start: Optional[date] = None, end: Optional[date] = None) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for row in export_rows(start, end):
        writer.writerow([csv_safe(row[c]) for c in CSV_COLUMNS])
    return buf.getvalue()
