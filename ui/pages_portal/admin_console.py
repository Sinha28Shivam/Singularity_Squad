"""Admin console: ticket queue, workflow runs, decisions, notes and full agent results."""
from __future__ import annotations

from datetime import date
from typing import Optional
from urllib.parse import quote

import streamlit as st

from ui.lib.api import (
    EVENT_CATEGORY_LABELS, PO_DRAFT_LABEL, TICKET_STATUSES, api_get, api_post, badge, friendly_error,
)
from ui.lib.approval_render import APPROVAL_TICKET_STATUSES, render_ticket_approval_panel, ticket_approval
from ui.lib.render import (
    INVOICE_LABELS, format_inr, invoice_options, render_final_status, render_pipeline, render_result_tabs,
)

DEFAULT_TODAY = date(2026, 10, 17)
NOT_RUNNABLE = {"approved", "rejected", "processing", "changes_requested"}


def _tpath(ticket_id: str) -> str:
    return f"/api/admin/tickets/{quote(ticket_id, safe='')}"


def _flash(kind: str, text: str) -> None:
    st.session_state["admin_flash"] = (kind, text)


def _set_ticket(view: dict) -> None:
    """Store an AdminTicketView in the session (display cache only; the DB is the source of truth)."""
    tid = view.get("ticket_id")
    prev = st.session_state.get("admin_ticket") or {}
    if prev.get("ticket_id") != tid:
        st.session_state.pop("admin_saved_audit", None)
    st.session_state.pop("admin_recon", None)
    st.session_state["admin_ticket"] = view


def load_ticket(ticket_id: str) -> bool:
    try:
        data = api_get(_tpath(ticket_id))
    except Exception as exc:
        st.session_state.pop("admin_ticket", None)
        _flash("error", friendly_error(exc, ticket=True))
        return False
    if not isinstance(data, dict):
        _flash("error", "Unexpected response from backend.")
        return False
    data.setdefault("ticket_id", ticket_id)
    _set_ticket(data)
    _flash("success", f"Loaded ticket `{data['ticket_id']}` from the database.")
    return True


def load_audit(ticket_id: str) -> None:
    try:
        data = api_get(f"{_tpath(ticket_id)}/audit", params={"limit": 500})
    except Exception as exc:
        _flash("error", friendly_error(exc, ticket=True))
        return
    events = data.get("events") if isinstance(data, dict) else None
    events = sorted(events or [], key=lambda e: (e.get("sequence") is None, e.get("sequence") or 0))
    tid = (data.get("ticket_id") if isinstance(data, dict) else None) or ticket_id
    st.session_state["admin_saved_audit"] = {"ticket_id": tid, "events": events}
    _flash("success", f"Loaded {len(events)} saved audit event(s) for `{tid}` — see **6. Critic & Audit**.")


def ticket_result(view: dict) -> Optional[dict]:
    wr = view.get("workflow_result")
    if not isinstance(wr, dict) or not wr:
        return None
    wr = dict(wr)
    wr.setdefault("ticket_id", view.get("ticket_id"))
    # Show the stored flag as-is; the backend only saves persisted=True after every write succeeded.
    wr["persisted"] = bool(wr.get("persisted"))
    return wr


# --------------------------------------------------------------------------- queue
def queue_section() -> None:
    top1, top2 = st.columns([5, 1])
    top1.subheader("Ticket queue")
    if top2.button("🔄 Refresh", key="admin_refresh"):
        tid = (st.session_state.get("admin_ticket") or {}).get("ticket_id")
        if tid:
            load_ticket(tid)
        st.rerun()

    status_filter = st.selectbox(
        "Status filter", ["All"] + TICKET_STATUSES,
        format_func=lambda s: s if s == "All" else badge(s), key="admin_status_filter",
    )
    params = {"limit": 100}
    if status_filter != "All":
        params["status"] = status_filter
    try:
        data = api_get("/api/admin/tickets", params=params)
    except Exception as exc:
        st.error(friendly_error(exc))
        return
    data = data if isinstance(data, dict) else {}
    counts = data.get("counts") or {}
    shown = ["submitted", "processing", "reviewed", "needs_info", "pending_approval", "changes_requested",
             "approval_expired", "approved", "rejected"]
    cols = st.columns(len(shown) + 1)
    for col, s in zip(cols, shown):
        col.metric(badge(s), int(counts.get(s, 0) or 0))
    problems = int(counts.get("failed", 0) or 0) + int(counts.get("persistence_error", 0) or 0)
    cols[-1].metric("⚠️ failed", problems)

    tickets = [t for t in data.get("tickets") or [] if isinstance(t, dict) and t.get("ticket_id")]
    if not tickets:
        st.info("No tickets match this filter.")
    else:
        st.dataframe([
            {
                "Ticket": t.get("ticket_id"),
                "Title": t.get("title") or "—",
                "Requester": t.get("owner_name") or t.get("owner_username") or "—",
                "Event date": t.get("event_date") or "—",
                "Status": badge(t.get("status")),
                "Final status": t.get("final_status") or "—",
                "Runs": t.get("run_count", 0),
            }
            for t in tickets
        ], width="stretch", hide_index=True)

    labels = {t["ticket_id"]: f"{t['ticket_id']} · {t.get('title') or ''} · {badge(t.get('status'))}"
              for t in tickets}

    def _pick() -> None:
        tid = st.session_state.get("admin_pick")
        if tid:
            st.session_state["admin_ticket_id_input"] = tid
            load_ticket(tid)

    c1, c2, c3 = st.columns([3, 3, 1], vertical_alignment="bottom")
    c1.selectbox("Select a ticket", [None] + list(labels.keys()),
                 format_func=lambda t: "(select a ticket)" if t is None else labels.get(t, t),
                 key="admin_pick", on_change=_pick)
    c2.text_input("…or type a ticket ID", key="admin_ticket_id_input", placeholder="REQ-20261017-9F3A1C2B")
    typed = (st.session_state.get("admin_ticket_id_input") or "").strip()
    if c3.button("📂 Load ticket", disabled=not typed, key="admin_load_btn"):
        load_ticket(typed)


# --------------------------------------------------------------------------- detail
def render_banner(view: dict, result: Optional[dict]) -> None:
    with st.container(border=True):
        c1, c2, c3 = st.columns([2, 2, 3])
        with c1:
            st.markdown("**🎫 Ticket ID**")
            st.code(view.get("ticket_id") or "?", language=None)
        with c2:
            st.markdown(f"**Status:** {badge(view.get('status'))}")
            st.caption(f"Runs: {view.get('run_count', 0)} · last run by {view.get('last_run_by') or '—'}")
        with c3:
            if result is None:
                st.info("Workflow not run yet — no stored result.")
            elif result.get("persisted"):
                st.success(f"💾 Stored result is persisted · final status **{result.get('final_status', '?')}**")
            else:
                st.warning("⚠️ Stored result is not marked persisted — a database write may have failed.")
        if view.get("error"):
            st.error(f"Last error: {view['error']}")


def render_requester(view: dict) -> None:
    sub = view.get("submission") or {}
    st.markdown(f"**Requester:** {view.get('owner_name') or '—'} (`{view.get('owner_username') or '?'}`)"
                + (f" · {view['owner_email']}" if view.get("owner_email") else ""))
    if sub:
        rows = [
            ("Title", sub.get("title")),
            ("Event category", EVENT_CATEGORY_LABELS.get(sub.get("event_category"), sub.get("event_category"))),
            ("Catering service", sub.get("service")),
            ("Event date", sub.get("event_date")),
            ("Attendees", sub.get("attendees")),
            ("Budget", format_inr(sub.get("budget_inr")) if sub.get("budget_inr") is not None else None),
            ("Location", sub.get("location")),
            ("Approved vendors only", "Yes" if sub.get("approved_vendor_only", True) else "No"),
            ("Notes", sub.get("notes")),
        ]
        st.dataframe([{"Field": k, "Value": "—" if v in (None, "") else str(v)} for k, v in rows],
                     width="stretch", hide_index=True)
    else:
        st.caption("No structured submission stored (legacy ticket).")
    if view.get("raw_text"):
        with st.expander("Request text sent to the agents"):
            st.markdown(view["raw_text"])


def actions_section(view: dict, result: Optional[dict]) -> None:
    tid = view["ticket_id"]
    status = view.get("status")
    run_count = int(view.get("run_count") or 0)

    st.subheader("Actions")
    a1, a2 = st.columns(2)
    with a1, st.container(border=True):
        st.markdown("**▶️ Workflow**")
        today = st.date_input("Demo date (today)", value=DEFAULT_TODAY, key="admin_today")
        invoice_id = st.selectbox("Invoice to check", [None] + invoice_options(),
                                  format_func=lambda x: INVOICE_LABELS.get(x, x), key="admin_invoice")
        can_run = status not in NOT_RUNNABLE
        label = "🔁 Re-run workflow" if run_count > 0 else "▶️ Run workflow"
        if st.button(label, type="primary", disabled=not can_run, key="admin_run_btn",
                     help=None if can_run else f"Not runnable while status is {status}."):
            try:
                with st.spinner("Agents working: Intake → Policy → Vendor → Procurement → Billing → Critic…"):
                    data = api_post(f"{_tpath(tid)}/run", {
                        "invoice_id": invoice_id, "today": today.isoformat() if today else None,
                    })
                if isinstance(data, dict):
                    data.setdefault("ticket_id", tid)
                    _set_ticket(data)
                _flash("success", f"Workflow finished — status **{(data or {}).get('status', '?')}**.")
            except Exception as exc:
                _flash("error", friendly_error(exc, ticket=True))
            st.rerun()

    with a2, st.container(border=True):
        st.markdown("**🧑‍⚖️ Decision**")
        st.caption("Approvals are decided by an eligible approver on the Approvals page — admins cannot approve.")
        if status in ("approved", "rejected"):
            d = view.get("decision") or {}
            if d:
                st.info(f"Final decision: **{status}** by {d.get('by', '?')} at {d.get('at', '?')}"
                        + (f" — “{d['note']}”" if d.get("note") else ""))
            else:
                st.info(f"Final status: **{status}** (see the approval history below).")
        elif status == "pending_approval":
            st.info("⏳🛡️ Waiting for an eligible approver. Re-running the workflow supersedes the pending request.")
        if status == "reviewed":
            # Admins may only close tickets that need no approval (e.g. hard-blocked / no draft PO).
            st.caption("This ticket needs no approval request (blocked or no draft PO). You can close it.")
            decision_note = st.text_area("Rejection note (optional)", key="admin_decision_note", max_chars=2000)
            if st.button("❌ Reject", key="admin_reject_btn", width="stretch"):
                _decide(tid, "reject", decision_note)
        st.caption(f"Draft PO: {PO_DRAFT_LABEL}.")


def _decide(tid: str, decision: str, note: str) -> None:
    try:
        data = api_post(f"{_tpath(tid)}/decision", {"decision": decision, "note": (note or "").strip() or None})
        if isinstance(data, dict):
            data.setdefault("ticket_id", tid)
            _set_ticket(data)
        _flash("success", f"Ticket {tid} rejected.")
    except Exception as exc:
        _flash("error", friendly_error(exc, ticket=True))
    st.rerun()


def approval_section(view: dict) -> None:
    tav, err = ticket_approval(view)
    if err:
        st.subheader("🛡️ Approval")
        st.warning(f"Approval details unavailable: {err}")
        return
    if tav is None and view.get("status") not in APPROVAL_TICKET_STATUSES:
        return
    current = render_ticket_approval_panel(tav, show_reconcile_hint=True)
    if current and current.get("reconciliation_required") and current.get("approval_id"):
        if st.button("🧩 Reconcile", key="admin_reconcile_btn",
                     help="Re-apply the follow-up updates for the recorded decision (idempotent)."):
            aid = current["approval_id"]
            try:
                api_post(f"/api/admin/approvals/{quote(aid, safe='')}/reconcile", None, timeout=30)
                _flash("success", f"Reconciliation finished for {aid} — reloaded from the server.")
            except Exception as exc:
                _flash("error", friendly_error(exc))
            load_ticket_quiet(view["ticket_id"])
            st.rerun()


def load_ticket_quiet(ticket_id: str) -> None:
    msg = st.session_state.get("admin_flash")
    if load_ticket(ticket_id) and msg is not None:
        st.session_state["admin_flash"] = msg


def notes_section(view: dict) -> None:
    tid = view["ticket_id"]
    st.subheader("💬 Notes")
    notes = view.get("notes") or []
    if not notes:
        st.caption("No notes yet.")
    for n in notes:
        who = {"admin": "🛠️ Admin", "approver": "🛡️ Approver"}.get(n.get("role"), "🙋 Requester")
        st.markdown(f"**{who} · {n.get('author', '?')}** · {n.get('created_at', '')}\n\n{n.get('text', '')}")
    with st.form(f"admin_note_form_{tid}", clear_on_submit=True):
        text = st.text_area("Add a note (visible to the requester)", key=f"admin_note_{tid}", max_chars=2000)
        add = st.form_submit_button("Add note")
    if add:
        if not text.strip():
            st.error("Note cannot be empty.")
            return
        try:
            data = api_post(f"{_tpath(tid)}/notes", {"text": text.strip()})
            if isinstance(data, dict):
                data.setdefault("ticket_id", tid)
                _set_ticket(data)
            _flash("success", "Note added.")
        except Exception as exc:
            _flash("error", friendly_error(exc, ticket=True))
        st.rerun()


def detail_section(view: dict) -> None:
    tid = view["ticket_id"]
    result = ticket_result(view)
    st.divider()
    st.header(f"Ticket {tid}")
    render_banner(view, result)
    render_requester(view)
    actions_section(view, result)
    approval_section(view)
    notes_section(view)

    st.divider()
    if st.button("📜 Load saved audit", key="admin_audit_btn"):
        load_audit(tid)
        st.rerun()
    saved = st.session_state.get("admin_saved_audit")
    if saved and saved.get("ticket_id") != tid:
        saved = None
    if result is None:
        st.info("No workflow result yet — run the workflow to see the agent pipeline.")
        if saved:
            from ui.lib.render import render_saved_audit
            render_saved_audit(saved)
        return
    st.subheader("Agent pipeline")
    render_pipeline(result)
    st.write("")
    render_final_status(result)
    render_result_tabs(result, saved_audit=saved, recon_key="admin_recon")
    if result.get("disclaimer"):
        st.caption(result["disclaimer"])


def render() -> None:
    st.title("🛠️ Admin console")
    st.caption("Review requests and run the agent workflow. Draft POs that need sign-off go to an eligible "
               "approver; admins can close (reject) tickets that need no approval.")
    queue_section()
    msg = st.session_state.pop("admin_flash", None)
    if msg:
        kind, text = msg
        {"error": st.error, "warning": st.warning}.get(kind, st.success)(text)
    view = st.session_state.get("admin_ticket")
    if isinstance(view, dict) and view.get("ticket_id"):
        detail_section(view)
    else:
        st.info("Select a ticket from the queue (or type its ID) to see details.")
