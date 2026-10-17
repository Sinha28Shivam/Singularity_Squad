"""Read-only renderers for Phase 2 approval requests (shared by the approver, admin and requester pages).

Nothing here decides anything: decisions are only submitted from ui/pages_portal/approvals.py and the
backend is the sole authority on who may decide and what was recorded.
"""
from __future__ import annotations

from typing import Optional

import streamlit as st

from ui.lib.api import (
    APPROVAL_ACTIONS, APPROVAL_SAFETY_NOTICE, PO_DRAFT_LABEL, RISK_BADGE, approval_badge,
)
from ui.lib.render import format_inr

ACTION_PAST = {"approve": "Approved", "reject": "Rejected", "request_changes": "Changes requested"}


def fmt_ts(value) -> str:
    if not value:
        return "—"
    text = str(value).replace("T", " ")
    return text[:19] + (" UTC" if text.endswith(("+00:00", "Z")) else "")


def fmt_amount(amount, currency: Optional[str] = "INR") -> str:
    if amount is None:
        return "—"
    if (currency or "INR").upper() == "INR":
        return format_inr(amount)
    return f"{currency} {float(amount):,.2f}"


def safety_notice(text: Optional[str] = None) -> None:
    st.info(f"🛡️ {text or APPROVAL_SAFETY_NOTICE}")


def decision_line(d: dict) -> str:
    verb = ACTION_PAST.get(d.get("action"), APPROVAL_ACTIONS.get(d.get("action"), str(d.get("action"))))
    line = (f"**{verb}** by `{d.get('actor_id', '?')}`"
            + (f" ({d['actor_role']})" if d.get("actor_role") else "")
            + f" at {fmt_ts(d.get('decided_at'))}")
    if d.get("request_version") is not None:
        line += f" · version {d['request_version']}"
    if d.get("comment"):
        line += f" — “{d['comment']}”"
    return line


def render_decisions(view: dict) -> None:
    decisions = [d for d in view.get("decisions") or [] if isinstance(d, dict)]
    if not decisions:
        st.caption("No decisions recorded yet.")
        return
    for d in decisions:
        st.markdown(f"- {decision_line(d)}")


def render_reasons(view: dict) -> None:
    codes = list(view.get("reason_codes") or [])
    reasons = list(view.get("reasons") or [])
    if not codes and not reasons:
        st.caption("No policy reasons recorded.")
        return
    for i in range(max(len(codes), len(reasons))):
        code = codes[i] if i < len(codes) else None
        text = reasons[i] if i < len(reasons) else ""
        st.markdown(f"- {f'`{code}` ' if code else ''}{text}")


def render_summary(view: dict) -> None:
    """Key facts of one approval request version."""
    rows = [
        ("Approval ID", view.get("approval_id")),
        ("Ticket ID", view.get("ticket_id")),
        ("Status", approval_badge(view.get("status"))),
        ("Request version", view.get("request_version")),
        ("Amount", fmt_amount(view.get("amount"), view.get("currency"))),
        ("Risk level", RISK_BADGE.get(view.get("risk_level"), view.get("risk_level"))),
        ("Requester", view.get("requester_id")),
        ("Required role", view.get("required_role")),
        ("Assigned approver", view.get("assigned_approver_id")),
        ("Policy version", view.get("policy_version")),
        ("Created", fmt_ts(view.get("created_at"))),
        ("Expires", fmt_ts(view.get("expires_at"))),
        ("Decided", fmt_ts(view.get("decided_at")) if view.get("decided_at") else None),
    ]
    st.dataframe([{"Field": k, "Value": "—" if v in (None, "") else str(v)} for k, v in rows],
                 width="stretch", hide_index=True)


def render_snapshot(view: dict) -> None:
    """The canonical fields covered by the workflow snapshot hash (what is being approved)."""
    snap = view.get("review_snapshot")
    if not isinstance(snap, dict) or not snap:
        st.caption("No review snapshot available.")
        return
    req = snap.get("request") or {}
    po = snap.get("po") or {}
    rows = [
        ("Ticket", snap.get("ticket_id")),
        ("Workflow run", snap.get("run_count")),
        ("Workflow final status", snap.get("final_status")),
        ("Event date", req.get("date")),
        ("Attendees", req.get("attendees")),
        ("Service", req.get("service")),
        ("Location", req.get("location")),
        ("Budget limit", fmt_amount(req.get("budget_limit"), po.get("currency") or "INR")
         if req.get("budget_limit") is not None else None),
        ("Draft PO", f"{po.get('po_id')} · {PO_DRAFT_LABEL}" if po.get("po_id") else None),
        ("Vendor", f"{po.get('vendor_name') or '?'} ({po.get('vendor_id') or '?'})" if po else None),
        ("Subtotal", fmt_amount(po.get("subtotal"), po.get("currency")) if po.get("subtotal") is not None else None),
        ("Tax", fmt_amount(po.get("tax"), po.get("currency")) if po.get("tax") is not None else None),
        ("Total", fmt_amount(po.get("total"), po.get("currency")) if po.get("total") is not None else None),
        ("Required approver (policy)", po.get("required_approver")),
    ]
    st.dataframe([{"Field": k, "Value": "—" if v in (None, "") else str(v)} for k, v in rows],
                 width="stretch", hide_index=True)
    items = po.get("line_items") or []
    if items:
        st.dataframe([
            {"Description": li.get("description"), "Qty": li.get("quantity"),
             "Unit price": fmt_amount(li.get("unit_price"), po.get("currency")),
             "Line total": fmt_amount(li.get("line_total"), po.get("currency"))}
            for li in items if isinstance(li, dict)
        ], width="stretch", hide_index=True)
    if view.get("workflow_snapshot_hash"):
        st.caption(f"Snapshot hash `{str(view['workflow_snapshot_hash'])[:16]}…` — a decision only applies "
                   "to exactly this version.")


APPROVAL_TICKET_STATUSES = {"pending_approval", "changes_requested", "approval_expired", "approved", "rejected"}


def ticket_approval(view: dict) -> tuple[Optional[dict], Optional[str]]:
    """Return (TicketApprovalView | None, error message | None) for a ticket view.

    Uses the ``approval`` field embedded in the ticket view when the backend provides it, otherwise
    fetches ``GET /api/tickets/{id}/approval`` for statuses that involve an approval request.
    """
    from urllib.parse import quote

    from ui.lib.api import ApiError, api_get, friendly_error

    if "approval" in view:
        emb = view["approval"]
        if not isinstance(emb, dict):
            return None, None
        if emb.get("approval_id") and "current" not in emb and "history" not in emb:
            emb = {"ticket_id": view.get("ticket_id"), "current": emb, "history": [emb]}
        return emb, None
    tid = view.get("ticket_id")
    if not tid or view.get("status") not in APPROVAL_TICKET_STATUSES:
        return None, None
    try:
        data = api_get(f"/api/tickets/{quote(str(tid), safe='')}/approval")
    except ApiError as exc:
        if exc.status_code == 404:
            return None, None
        return None, friendly_error(exc)
    except Exception as exc:
        return None, friendly_error(exc)
    return (data if isinstance(data, dict) else None), None


def render_ticket_approval_panel(tav: Optional[dict], *, show_reconcile_hint: bool = False) -> Optional[dict]:
    """Approval panel for a ticket (TicketApprovalView). Read-only. Returns the current request, if any."""
    st.subheader("🛡️ Approval")
    if not isinstance(tav, dict):
        st.caption("No approval request for this ticket.")
        safety_notice()
        return None
    current = tav.get("current") if isinstance(tav.get("current"), dict) else None
    history = [h for h in tav.get("history") or [] if isinstance(h, dict)]
    if current is None and history:
        current = history[-1]
    if current is None:
        st.caption("No approval request for this ticket.")
        safety_notice(tav.get("safety_notice"))
        return None
    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        c1.markdown(f"**Approval status:** {approval_badge(current.get('status'))}")
        c1.caption(f"`{current.get('approval_id', '?')}` · version {current.get('request_version', '?')}")
        c2.metric("Amount", fmt_amount(current.get("amount"), current.get("currency")))
        c3.markdown(f"**Risk:** {RISK_BADGE.get(current.get('risk_level'), current.get('risk_level') or '—')}")
        c3.caption(f"Requested {fmt_ts(current.get('created_at'))} · expires {fmt_ts(current.get('expires_at'))}")
        st.markdown("**Why approval is required**")
        render_reasons(current)
        if current.get("required_role"):
            st.caption(f"To be decided by role: {current['required_role']}"
                       + (f" · assigned to {current['assigned_approver_id']}"
                          if current.get("assigned_approver_id") else ""))
        if show_reconcile_hint and current.get("reconciliation_required"):
            st.warning("⚠️ The decision is recorded but follow-up updates are incomplete — reconciliation required.")
    st.markdown("**Decision history**")
    versions = history or [current]
    for v in versions:
        st.markdown(f"**Version {v.get('request_version', '?')}** · {approval_badge(v.get('status'))} · "
                    f"requested {fmt_ts(v.get('created_at'))}")
        render_decisions(v)
    safety_notice(tav.get("safety_notice") or current.get("safety_notice"))
    return current
