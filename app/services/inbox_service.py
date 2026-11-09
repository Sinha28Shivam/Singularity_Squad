"""Role-specific pending-actions inbox, derived on read from authoritative stored state (no new writes).

Items are a *view* of what each role can act on next; the backend still validates every action when it is
taken. ``stalled`` flags items waiting longer than ``INBOX_STALL_HOURS`` (default 24h).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel

from app.portal_schemas import UserPublic
from app.repositories import list_approvals, list_tickets, list_tickets_for_owner
from app.repositories.payment_repository import list_simulations
from app.services.ticket_service import _require_persistence, invoice_status


class InboxItem(BaseModel):
    kind: str            # run_workflow | review_result | decide_approval | match_invoice | resolve_invoice | ...
    priority: int        # 1 = highest
    ticket_id: str
    title: Optional[str] = None
    label: str
    detail: str
    link: str
    since: Optional[datetime] = None
    stalled: bool = False


def _stall_hours() -> float:
    try:
        return max(1.0, float(os.getenv("INBOX_STALL_HOURS", "24")))
    except ValueError:
        return 24.0


def _utc(value) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _item(doc: dict, kind: str, priority: int, label: str, detail: str, link: str, now: datetime,
          since=None) -> InboxItem:
    since = _utc(since) or _utc(doc.get("updated_at")) or _utc(doc.get("created_at"))
    stalled = bool(since and (now - since).total_seconds() > _stall_hours() * 3600)
    return InboxItem(kind=kind, priority=priority, ticket_id=str(doc.get("ticket_id")),
                     title=(doc.get("submission") or {}).get("title"), label=label, detail=detail, link=link,
                     since=since, stalled=stalled)


_INVOICE_TODO = {
    "needs_invoice": (3, "match_invoice", "Match the vendor invoice",
                      "Approved request with no invoice yet. Match it against the approved draft PO."),
    "mismatch": (1, "resolve_invoice", "Resolve the invoice mismatch",
                 "The invoice did not match. Dispute it or match a corrected invoice."),
    "duplicate": (1, "resolve_invoice", "Reject the duplicate invoice",
                  "This invoice already appears in payment history."),
    "on_hold": (2, "resolve_invoice", "Invoice on hold", "Release the hold or dispute the invoice."),
    "disputed": (2, "await_vendor", "Waiting on the vendor",
                 "The invoice is in dispute. Match the corrected invoice when it arrives."),
    "rejected": (2, "resolve_invoice", "Match a corrected invoice", "The previous invoice was rejected."),
    "matched": (2, "simulate_payment", "Ready to simulate payment",
                "Invoice matched. Create the simulated payment instruction (no money moves)."),
}


def _admin_items(now: datetime) -> list[InboxItem]:
    items: list[InboxItem] = []
    for doc in list_tickets("submitted", 200):
        items.append(_item(doc, "run_workflow", 2, "Run the workflow", "A new request is waiting for its checks.",
                           f"/admin/requests/{doc['ticket_id']}", now))
    for status, label in (("reviewed", "Review the result"), ("failed", "Retry the failed run"),
                          ("persistence_error", "Retry the run (save failed)"),
                          ("approval_expired", "Re-run: approval expired")):
        for doc in list_tickets(status, 200):
            items.append(_item(doc, "review_result" if status == "reviewed" else "retry_workflow",
                               1 if status != "reviewed" else 2, label,
                               "The completed checks need an operator decision." if status == "reviewed"
                               else "The workflow needs to be run again.", f"/admin/requests/{doc['ticket_id']}", now))
    paid = {p.get("ticket_id"): p for p in list_simulations(None, 200)}
    for doc in list_tickets("approved", 200):
        p = paid.get(doc.get("ticket_id"))
        if p is not None:
            if p.get("status") == "failed_simulated":
                items.append(_item(doc, "retry_payment", 1, "Retry the simulated payment",
                                   "The simulator reported a transient failure.", "/admin/payments", now,
                                   since=p.get("updated_at")))
            continue
        state = invoice_status(doc)
        if state in _INVOICE_TODO:
            prio, kind, label, detail = _INVOICE_TODO[state]
            link = "/admin/payments" if state == "matched" else f"/admin/requests/{doc['ticket_id']}"
            items.append(_item(doc, kind, prio, label, detail, link, now))
    return items


def _approver_items(user: UserPublic, now: datetime) -> list[InboxItem]:
    items = []
    for a in list_approvals("pending", 200):
        if a.get("requester_id") == user.username:
            continue  # nobody can approve their own request
        doc = {"ticket_id": a.get("ticket_id"), "created_at": a.get("created_at")}
        exp = _utc(a.get("expires_at"))
        detail = (f"{a.get('currency') or 'INR'} {float(a.get('amount') or 0):,.2f} · risk {a.get('risk_level') or '—'}"
                  + (f" · expires {exp:%d %b %H:%M} UTC" if exp else ""))
        expiring = bool(exp and (exp - now).total_seconds() < 12 * 3600)
        items.append(_item(doc, "decide_approval", 1 if expiring else 2, "Decide the approval request", detail,
                           f"/approvals/{a.get('approval_id')}", now, since=a.get("created_at")))
    return items


def _requester_items(user: UserPublic, now: datetime) -> list[InboxItem]:
    items = []
    for doc in list_tickets_for_owner(user.username, 200):
        if doc.get("status") == "needs_info":
            items.append(_item(doc, "provide_information", 1, "Provide more information",
                               "Your request is missing details needed to continue.", f"/requests/{doc['ticket_id']}", now))
        elif doc.get("status") == "changes_requested":
            items.append(_item(doc, "resubmit_request", 1, "Update and resubmit",
                               "The approver asked for changes.", f"/requests/{doc['ticket_id']}", now))
    return items


def inbox_for(user: UserPublic, now: Optional[datetime] = None) -> dict:
    _require_persistence()
    now = now or datetime.now(timezone.utc)
    if user.role == "admin":
        items = _admin_items(now)
    elif user.role == "approver":
        items = _approver_items(user, now)
    else:
        items = _requester_items(user, now)
    items.sort(key=lambda i: (i.priority, not i.stalled, i.since or now))
    counts: dict[str, int] = {}
    for i in items:
        counts[i.kind] = counts.get(i.kind, 0) + 1
    return {"role": user.role, "items": [i.model_dump(mode="json") for i in items[:200]], "total": len(items),
            "stalled": sum(1 for i in items if i.stalled), "counts": counts,
            "stall_hours": _stall_hours(), "generated_at": now.isoformat()}
