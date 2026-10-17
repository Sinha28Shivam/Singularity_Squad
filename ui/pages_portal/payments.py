"""Payments (Simulation) page — SIMULATION ONLY. No bank is contacted and no money is ever transferred.

Admins (operations) can create a *simulated* payment instruction for the reconciled invoice of an approved
ticket after an explicit confirmation. Approvers see a read-only list of simulated payments. The backend
re-checks eligibility on every request and is the only source of truth; the UI never claims a payment was
made and never uses a success badge for a simulated payment.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional
from urllib.parse import quote

import streamlit as st

from ui.lib.api import (
    SIMULATION_BANNER, api_get, api_post, create_simulation, fetch_invoices, current_user, eligibility, friendly_error, get_payment,
    get_payment_events, list_payments, payment_error, payment_status_label, retry_payment,
)
from ui.lib.approval_render import fmt_ts
from ui.lib.render import format_inr

RETRYABLE = {"failed_simulated"}
NO_INVOICE = ("This ticket has no reconciled invoice — run the workflow with an invoice, or match the "
              "invoice against the approved PO below.")
INVOICE_LABELS = {"INV-5001": "INV-5001 — clean match", "INV-5002": "INV-5002 — rate + qty mismatch",
                  "INV-4987": "INV-4987 — duplicate"}
REPLAY_NOTICE = "Existing simulated payment returned (idempotent replay)"
READ_ONLY_NOTICE = ("Read-only: approvers can review simulated payments but cannot initiate them. "
                    "Initiation is restricted to operations admins.")
NOT_AVAILABLE = "Simulated payments are available to operations admins and approvers only."
RECEIPT_DISCLAIMER = (f"{SIMULATION_BANNER}. This is a simulated payment instruction for demo purposes only. "
                      "No bank was contacted and no money was transferred.")


def confirmation_text(amount) -> str:
    return (f"You are about to create a **simulated payment instruction** for {format_inr(amount)}. "
            "This is a demo only. No bank will be contacted and no money will be transferred.")


def fmt_money(amount, currency: Optional[str]) -> str:
    if amount is None:
        return "—"
    if (currency or "INR").upper() == "INR":
        return f"{format_inr(amount)} INR"
    return f"{amount} {currency}"


def banner() -> None:
    st.warning(f"**{SIMULATION_BANNER}**", icon="🧪")


def _flash(kind: str, text: str) -> None:
    st.session_state["pay_flash"] = (kind, text)


def _show_flash() -> None:
    msg = st.session_state.pop("pay_flash", None)
    if msg:
        kind, text = msg
        {"error": st.error, "warning": st.warning}.get(kind, st.info)(text)


def render_error(err: dict) -> None:
    head = f"**{err['error']}** — {err.get('message') or ''}" if err.get("error") else (err.get("message") or "")
    lines = [head] + [f"- `{r['code']}` — {r.get('message') or ''}" for r in err.get("reasons") or []]
    st.error("\n".join(lines))


def _idempotency_key(ticket_id: str, invoice_id: str) -> str:
    keys = st.session_state.setdefault("pay_idem", {})
    intent = f"{ticket_id}|{invoice_id}"
    if intent not in keys:
        keys[intent] = f"ui-{uuid.uuid4().hex}"
    return keys[intent]


# --------------------------------------------------------------------------- receipt
def render_events(events: list) -> None:
    st.markdown("**Event timeline**")
    if not events:
        st.caption("No events recorded yet.")
        return
    events = sorted(events, key=lambda e: str(e.get("at") or ""))
    st.dataframe([
        {
            "At": fmt_ts(e.get("at")),
            "Event": str(e.get("event_type") or "?").replace("_", " "),
            "Status": payment_status_label(e.get("status")) if e.get("status") else "—",
            "Actor": e.get("actor") or "—",
            "Reason codes": ", ".join(e.get("reason_codes") or []) or "—",
            "Detail": e.get("detail") or "—",
        }
        for e in events
    ], width="stretch", hide_index=True)


def receipt_json(payment: dict, events: list) -> str:
    doc = {
        "disclaimer": RECEIPT_DISCLAIMER,
        "mode": "SIMULATION",
        "money_transferred": False,
        "payment": payment,
        "events": events,
    }
    return json.dumps(doc, indent=2, default=str)


def render_receipt(payment: dict, events: list, *, allow_retry: bool) -> None:
    pid = payment.get("payment_id") or "?"
    status = payment.get("status")
    with st.container(border=True):
        st.subheader(f"🧾 Simulation receipt · {pid}")
        st.warning(f"**{SIMULATION_BANNER}** — {RECEIPT_DISCLAIMER}")
        rows = [
            ("Payment ID", pid),
            ("Provider reference (fake)", payment.get("provider_reference") or "— (none issued)"),
            ("Amount", fmt_money(payment.get("amount"), payment.get("currency"))),
            ("Currency", payment.get("currency") or "—"),
            ("Method", payment.get("method") or "SIMULATED_NEFT"),
            ("Ticket", payment.get("ticket_id") or "—"),
            ("Invoice (synthetic)", payment.get("invoice_id") or "—"),
            ("Purchase order (draft)", payment.get("purchase_order_id") or "—"),
            ("Simulator status", payment_status_label(status)),
            ("Failure code", payment.get("failure_code") or "—"),
            ("Attempts", str(payment.get("attempts") or 1)),
            ("Approval", f"{payment.get('approval_id') or '—'} (v{payment.get('approval_request_version') or '?'})"),
            ("Created", fmt_ts(payment.get("created_at"))),
            ("Last updated", fmt_ts(payment.get("updated_at"))),
            ("Money transferred", "No — simulation only"),
        ]
        st.dataframe([{"Field": k, "Value": str(v)} for k, v in rows], width="stretch", hide_index=True)
        st.info(f"Simulator status: **{payment_status_label(status)}**")
        if payment.get("audit_complete") is False:
            st.warning("Audit trail incomplete for this simulation — an admin should reconcile it.")
        render_events(events)
        c1, c2 = st.columns(2)
        c1.download_button("⬇️ Download simulation receipt (JSON)", data=receipt_json(payment, events),
                           file_name=f"{pid}-SIMULATED.json", mime="application/json", key=f"pay_dl_{pid}")
        if allow_retry and status in RETRYABLE:
            if c2.button("🔁 Retry simulation", key="pay_retry_btn",
                         help="Re-submits the same simulated instruction (same payment ID). No money is moved."):
                try:
                    data = retry_payment(pid)
                    p = data.get("payment") if isinstance(data, dict) else None
                    st.session_state["pay_receipt"] = p if isinstance(p, dict) else payment
                    _flash("info", "Retry submitted to the simulator — status reloaded from the server.")
                except Exception as exc:
                    st.session_state["pay_error"] = payment_error(exc)
                st.rerun()


def show_receipt(payment_id: str, fallback: Optional[dict], *, allow_retry: bool) -> None:
    """Reload the payment and its events from the server (source of truth) and render the receipt."""
    payment, events = fallback, []
    try:
        fresh = get_payment(payment_id)
        if isinstance(fresh, dict) and fresh.get("payment_id"):
            payment = fresh
    except Exception as exc:
        render_error(payment_error(exc))
    try:
        events = get_payment_events(payment_id)
    except Exception as exc:
        st.caption(f"Events unavailable: {payment_error(exc)['message']}")
    if isinstance(payment, dict):
        render_receipt(payment, events, allow_retry=allow_retry)


def payments_table(payments: list) -> None:
    st.dataframe([
        {
            "Payment": p.get("payment_id"),
            "Ticket": p.get("ticket_id") or "—",
            "Invoice": p.get("invoice_id") or "—",
            "PO": p.get("purchase_order_id") or "—",
            "Amount": fmt_money(p.get("amount"), p.get("currency")),
            "Status": payment_status_label(p.get("status")),
            "Created": fmt_ts(p.get("created_at")),
            "Mode": "SIMULATION",
        }
        for p in payments
    ], width="stretch", hide_index=True)


# --------------------------------------------------------------------------- eligibility
def render_eligibility(e: dict) -> None:
    st.subheader("Eligibility check")
    rows = [
        ("Ticket", e.get("ticket_id")),
        ("Invoice (synthetic)", e.get("invoice_id")),
        ("Purchase order (draft)", e.get("purchase_order_id")),
        ("Canonical amount", fmt_money(e.get("amount"), e.get("currency")) if e.get("amount") else None),
        ("Invoice / PO match", e.get("invoice_match_status")),
        ("Approval ID", e.get("approval_id")),
        ("Approval status", e.get("approval_status")),
        ("Approved by", e.get("approved_by")),
        ("Approval request version", e.get("approval_request_version")),
        ("Policy set version", e.get("policy_set_version")),
        ("Existing simulated payment", e.get("existing_payment_id")),
        ("Checked at", fmt_ts(e.get("checked_at"))),
    ]
    st.dataframe([{"Field": k, "Value": "—" if v in (None, "") else str(v)} for k, v in rows],
                 width="stretch", hide_index=True)
    reasons = [r for r in e.get("reasons") or [] if isinstance(r, dict)]
    if e.get("eligible"):
        st.info("✔️ **Eligible** — this invoice can be used for a simulated payment instruction.")
    else:
        lines = ["⛔ **Not eligible** — resolve these blockers first:"]
        lines += [f"- `{r.get('code') or '?'}` — {r.get('message') or ''}" for r in reasons]
        if not reasons:
            lines.append("- No reason supplied by the server.")
        st.error("\n".join(lines))


def _approved_tickets() -> list:
    try:
        data = api_get("/api/admin/tickets", params={"limit": 100, "status": "approved"})
    except Exception as exc:
        st.error(friendly_error(exc))
        return []
    rows = (data or {}).get("tickets") if isinstance(data, dict) else None
    return [t for t in rows or [] if isinstance(t, dict) and t.get("ticket_id") and t.get("status") == "approved"]


def _ticket_invoice(ticket_id: str) -> tuple[Optional[str], Optional[str]]:
    """Return (invoice_id, error) from the ticket's stored reconciliation."""
    try:
        view = api_get(f"/api/admin/tickets/{quote(ticket_id, safe='')}")
    except Exception as exc:
        return None, friendly_error(exc, ticket=True)
    wr = view.get("workflow_result") if isinstance(view, dict) else None
    recon = wr.get("reconciliation") if isinstance(wr, dict) else None
    if not isinstance(recon, dict) and isinstance(view, dict) and isinstance(view.get("invoice_match"), dict):
        recon = view["invoice_match"]  # invoice matched after approval
    inv = recon.get("invoice_id") if isinstance(recon, dict) else None
    return (inv, None) if inv else (None, NO_INVOICE)


def _invoice_match_form(ticket_id: str) -> None:
    """Approved ticket without an invoice: match a (synthetic) invoice against the approved PO, no re-approval."""
    st.caption("Match the vendor's invoice against the **approved** draft PO. The approval and PO are not "
               "changed, and no money moves.")
    try:
        options = [i.get("invoice_id") for i in fetch_invoices() if isinstance(i, dict) and i.get("invoice_id")]
    except Exception:  # noqa: BLE001
        options = []
    options = options or ["INV-5001", "INV-5002", "INV-4987"]
    inv = st.selectbox("Invoice to match", options, format_func=lambda x: INVOICE_LABELS.get(x, x),
                       key="pay_match_invoice")
    if st.button("🧾 Match invoice", key="pay_match_btn"):
        try:
            api_post(f"/api/admin/tickets/{quote(ticket_id, safe='')}/invoice-match", {"invoice_id": inv})
            _flash("info", f"Invoice {inv} matched against the approved PO. Check eligibility below.")
        except Exception as exc:  # noqa: BLE001
            _flash("error", friendly_error(exc, ticket=True))
        st.rerun()


def _reset_confirm_on_intent_change(intent: str) -> None:
    if st.session_state.get("pay_intent") != intent:
        st.session_state["pay_intent"] = intent
        st.session_state["pay_confirm_check"] = False
        st.session_state.pop("pay_error", None)
        receipt = st.session_state.get("pay_receipt")
        if isinstance(receipt, dict) and f"{receipt.get('ticket_id')}|{receipt.get('invoice_id')}" != intent:
            st.session_state.pop("pay_receipt", None)
            st.session_state.pop("pay_replayed", None)


def _simulate(e: dict) -> None:
    body = {
        "ticket_id": e["ticket_id"],
        "invoice_id": e["invoice_id"],
        "purchase_order_id": e.get("purchase_order_id"),
        "amount": e.get("amount"),
        "currency": e.get("currency"),
        "simulation_only": True,
        "confirmed_by_user": True,
        "idempotency_key": _idempotency_key(e["ticket_id"], e["invoice_id"]),
    }
    try:
        data = create_simulation(body)
    except Exception as exc:
        st.session_state["pay_error"] = payment_error(exc)
        return
    payment = data.get("payment") if isinstance(data, dict) else None
    if not isinstance(payment, dict) or not payment.get("payment_id"):
        st.session_state["pay_error"] = {"error": None, "message": "Unexpected response from backend.", "reasons": []}
        return
    st.session_state.pop("pay_error", None)
    st.session_state["pay_receipt"] = payment
    st.session_state["pay_replayed"] = bool(data.get("replayed"))
    _flash("info", REPLAY_NOTICE if data.get("replayed") else
           (data.get("message") or "Simulated payment instruction created. No money was transferred."))


def admin_view() -> None:
    st.caption("Create a simulated payment instruction for the reconciled invoice of an approved ticket. "
               "The server re-checks eligibility on every request.")
    tickets = _approved_tickets()
    if not tickets:
        st.info("No approved tickets yet — a ticket must be approved by an eligible approver first.")
    labels = {t["ticket_id"]: f"{t['ticket_id']} · {t.get('title') or ''}" for t in tickets}
    tid = st.selectbox("Approved ticket", [None] + list(labels.keys()),
                       format_func=lambda t: "(select an approved ticket)" if t is None else labels.get(t, t),
                       key="pay_ticket_pick")
    _show_flash()
    if tid:
        invoice_id, err = _ticket_invoice(tid)
        if err:
            st.warning(err)
            if err == NO_INVOICE:
                _invoice_match_form(tid)
        else:
            intent = f"{tid}|{invoice_id}"
            _reset_confirm_on_intent_change(intent)
            st.markdown(f"**Invoice from the ticket's reconciliation:** `{invoice_id}`")
            try:
                e = eligibility(tid, invoice_id)
            except Exception as exc:
                render_error(payment_error(exc))
                e = None
            if isinstance(e, dict):
                e.setdefault("ticket_id", tid)
                e.setdefault("invoice_id", invoice_id)
                render_eligibility(e)
                _confirm_section(e)
            if st.session_state.get("pay_error"):
                render_error(st.session_state["pay_error"])
            receipt = st.session_state.get("pay_receipt")
            if isinstance(receipt, dict) and receipt.get("payment_id"):
                if st.session_state.get("pay_replayed"):
                    st.info(REPLAY_NOTICE)
                show_receipt(receipt["payment_id"], receipt, allow_retry=True)
            elif isinstance(e, dict) and e.get("existing_payment_id"):
                if st.button("🧾 Show existing simulated payment", key="pay_show_existing_btn"):
                    st.session_state["pay_receipt"] = {"payment_id": e["existing_payment_id"],
                                                       "ticket_id": tid, "invoice_id": invoice_id}
                    st.session_state["pay_replayed"] = False
                    st.rerun()
    st.divider()
    with st.expander("All simulated payments"):
        _all_payments()


def _confirm_section(e: dict) -> None:
    if not e.get("eligible"):
        return
    st.subheader("Confirm simulation")
    st.markdown(confirmation_text(e.get("amount")))
    confirmed = st.checkbox("I understand this is a simulation and no money will be transferred.",
                            key="pay_confirm_check")
    st.button("Simulate Payment", key="pay_simulate_btn", type="secondary", disabled=not confirmed,
              on_click=_on_simulate, args=(dict(e),))


def _on_simulate(e: dict) -> None:
    # Runs as a widget callback, so the confirmation checkbox can be reset safely.
    _simulate(e)
    st.session_state["pay_confirm_check"] = False


def _all_payments() -> list:
    try:
        payments = list_payments()
    except Exception as exc:
        render_error(payment_error(exc))
        return []
    if not payments:
        st.caption("No simulated payments recorded yet.")
    else:
        payments_table(payments)
    return payments


def approver_view() -> None:
    st.info(READ_ONLY_NOTICE)
    payments = _all_payments()
    ids = [p["payment_id"] for p in payments if p.get("payment_id")]
    if not ids:
        return
    pick = st.selectbox("View simulation receipt", [None] + ids,
                        format_func=lambda p: "(select a simulated payment)" if p is None else p,
                        key="pay_view_pick")
    if pick:
        fallback = next((p for p in payments if p.get("payment_id") == pick), None)
        show_receipt(pick, fallback, allow_retry=False)


def render() -> None:
    st.title("💸 Payments (Simulation)")
    banner()
    role = (current_user() or {}).get("role")
    if role == "admin":
        admin_view()
    elif role == "approver":
        approver_view()
    else:
        st.error(NOT_AVAILABLE)
