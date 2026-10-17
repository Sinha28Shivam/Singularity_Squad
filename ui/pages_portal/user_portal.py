"""Requester portal: submit event requests and follow their status. Never shows agent internals."""
from __future__ import annotations

from datetime import date
from typing import Any, Optional
from urllib.parse import quote

import streamlit as st

from ui.lib.api import (
    EVENT_CATEGORY_LABELS, SERVICES, USER_EDITABLE_STATUSES, api_get, api_post, api_put, badge,
    current_user, friendly_error,
)
from ui.lib.api import PO_DRAFT_LABEL
from ui.lib.approval_render import render_ticket_approval_panel, ticket_approval
from ui.lib.render import format_inr

DEFAULT_EVENT_DATE = date(2026, 10, 23)
CATEGORIES = list(EVENT_CATEGORY_LABELS.keys())


def _parse_date(value: Any, default: date) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return default


def submission_form(form_key: str, defaults: Optional[dict] = None, submit_label: str = "Submit request"
                    ) -> Optional[dict]:
    """Render a TicketSubmission form; return the JSON payload when submitted and valid."""
    d = defaults or {}
    with st.form(form_key):
        title = st.text_input("Title", value=d.get("title") or "", max_chars=120, key=f"{form_key}_title",
                              placeholder="Quarterly team lunch")
        c1, c2 = st.columns(2)
        cat = d.get("event_category") if d.get("event_category") in CATEGORIES else "team_lunch"
        category = c1.selectbox("Event category", CATEGORIES, index=CATEGORIES.index(cat),
                                format_func=lambda k: EVENT_CATEGORY_LABELS.get(k, k), key=f"{form_key}_category")
        svc = d.get("service") if d.get("service") in SERVICES else "lunch"
        service = c2.selectbox("Catering service", SERVICES, index=SERVICES.index(svc),
                               format_func=str.capitalize, key=f"{form_key}_service")
        c3, c4, c5 = st.columns(3)
        event_date = c3.date_input("Event date", value=_parse_date(d.get("event_date"), DEFAULT_EVENT_DATE),
                                   key=f"{form_key}_date")
        attendees = c4.number_input("Attendees", min_value=1, max_value=5000, step=1,
                                    value=int(d.get("attendees") or 100), key=f"{form_key}_attendees")
        budget = c5.number_input("Budget (INR)", min_value=0.0, max_value=10_000_000.0, step=1000.0,
                                 value=float(d.get("budget_inr") or 100000.0), key=f"{form_key}_budget")
        location = st.text_input("Location", value=d.get("location") or "", max_chars=120,
                                 key=f"{form_key}_location", placeholder="Bengaluru office")
        approved_only = st.checkbox("Approved vendors only", value=bool(d.get("approved_vendor_only", True)),
                                    key=f"{form_key}_approved")
        notes = st.text_area("Notes / anything else", value=d.get("notes") or "", max_chars=2000,
                             key=f"{form_key}_notes")
        submitted = st.form_submit_button(submit_label, type="primary")
    if not submitted:
        return None
    errors = []
    if len(title.strip()) < 3:
        errors.append("Title must be at least 3 characters.")
    if not budget or float(budget) <= 0:
        errors.append("Budget must be greater than zero.")
    if not event_date:
        errors.append("Please pick an event date.")
    for e in errors:
        st.error(e)
    if errors:
        return None
    return {
        "title": title.strip(),
        "event_category": category,
        "service": service,
        "event_date": event_date.isoformat(),
        "attendees": int(attendees),
        "budget_inr": float(budget),
        "location": location.strip() or None,
        "approved_vendor_only": bool(approved_only),
        "notes": notes.strip() or None,
    }


def _ticket_path(ticket_id: str) -> str:
    return f"/api/my/tickets/{quote(ticket_id, safe='')}"


def new_request_section() -> None:
    payload = submission_form("new_ticket_form")
    if payload is not None:
        try:
            view = api_post("/api/my/tickets", payload)
        except Exception as exc:
            st.error(friendly_error(exc))
            return
        tid = (view or {}).get("ticket_id")
        st.success(f"Request submitted — ticket **{tid}** · status **{(view or {}).get('status', 'submitted')}**. "
                   "An operations admin will review it.")
        if tid:
            st.session_state["my_selected_ticket"] = tid


def render_user_ticket(view: dict) -> None:
    tid = view.get("ticket_id")
    status = view.get("status")
    with st.container(border=True):
        c1, c2 = st.columns([2, 3])
        c1.markdown("**🎫 Ticket ID**")
        c1.code(tid or "?", language=None)
        c2.markdown(f"**Status:** {badge(status)}")
        if view.get("outcome"):
            c2.info(view["outcome"])

    questions = view.get("clarification_questions") or []
    if questions:
        st.subheader("❔ We need a bit more information")
        for q in questions:
            st.markdown(f"- {q}")

    po = view.get("po_summary")
    if isinstance(po, dict) and po:
        st.subheader("🧾 Draft purchase order")
        m1, m2, m3 = st.columns(3)
        m1.metric("PO", po.get("po_id") or "—")
        m2.metric("Vendor", po.get("vendor_name") or "—")
        m3.metric("Total", format_inr(po.get("total")))
        approval = po.get("approval_status")
        st.caption(f"{PO_DRAFT_LABEL}. PO status: {po.get('status', 'draft')}"
                   + (f" · internal approval: {approval}" if approval else ""))

    tav, tav_error = ticket_approval(view)
    if tav_error:
        st.warning(f"Approval details unavailable: {tav_error}")
    elif tav is not None or status in ("pending_approval", "changes_requested", "approval_expired"):
        render_ticket_approval_panel(tav)

    sub = view.get("submission") or {}
    st.subheader("Submitted details")
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

    decision = view.get("decision")
    if isinstance(decision, dict) and decision:
        verb = "Approved" if decision.get("decision") == "approve" else "Rejected"
        msg = f"**{verb}** by {decision.get('by', 'admin')} at {decision.get('at', '?')}"
        if verb == "Approved":
            msg += " — internal authorization only; no order placed, nothing paid"
        if decision.get("note"):
            msg += f" — “{decision['note']}”"
        (st.success if verb == "Approved" else st.error)(msg)

    render_notes(view.get("notes") or [])
    with st.form(f"user_note_form_{tid}", clear_on_submit=True):
        text = st.text_area("Add a note for the operations team", key=f"user_note_{tid}", max_chars=2000)
        add = st.form_submit_button("Add note")
    if add:
        if not text.strip():
            st.error("Note cannot be empty.")
        else:
            try:
                api_post(f"{_ticket_path(tid)}/notes", {"text": text.strip()})
                st.session_state["my_flash"] = "Note added."
                st.rerun()
            except Exception as exc:
                st.error(friendly_error(exc))

    if status in USER_EDITABLE_STATUSES:
        st.subheader("✏️ Edit & resubmit")
        if status == "needs_info":
            st.caption("Update the details to answer the questions above, then resubmit.")
        elif status == "changes_requested":
            st.caption("The approver asked for changes (see the comments above). Update the details and resubmit "
                       "— an admin will re-run the workflow and a new approval version will be created.")
        elif status == "approval_expired":
            st.caption("The approval request expired without a decision. You can update and resubmit the "
                       "request; an admin can also re-run it as is.")
        payload = submission_form(f"edit_ticket_form_{tid}", defaults=sub, submit_label="Resubmit request")
        if payload is not None:
            try:
                api_put(_ticket_path(tid), payload)
                st.session_state["my_flash"] = f"Ticket {tid} resubmitted."
                st.rerun()
            except Exception as exc:
                st.error(friendly_error(exc))


def render_notes(notes: list[dict]) -> None:
    st.subheader("💬 Notes")
    if not notes:
        st.caption("No notes yet.")
        return
    for n in notes:
        who = {"admin": "🛠️ Admin", "approver": "🛡️ Approver"}.get(n.get("role"), "🙋 Requester")
        st.markdown(f"**{who} · {n.get('author', '?')}** · {n.get('created_at', '')}\n\n{n.get('text', '')}")


def my_requests_section() -> None:
    top1, top2 = st.columns([5, 1])
    top1.subheader("My requests")
    if top2.button("🔄 Refresh", key="my_refresh"):
        st.rerun()
    try:
        data = api_get("/api/my/tickets", params={"limit": 50})
    except Exception as exc:
        st.error(friendly_error(exc))
        return
    tickets = [t for t in (data or {}).get("tickets") or [] if isinstance(t, dict) and t.get("ticket_id")]
    if not tickets:
        st.info("You have no requests yet. Use **New request** to create one.")
        return
    st.dataframe([
        {
            "Ticket": t.get("ticket_id"),
            "Title": t.get("title") or "—",
            "Event": EVENT_CATEGORY_LABELS.get(t.get("event_category"), t.get("event_category") or "—"),
            "Event date": t.get("event_date") or "—",
            "Status": badge(t.get("status")),
            "Updated": t.get("updated_at") or t.get("created_at") or "—",
        }
        for t in tickets
    ], width="stretch", hide_index=True)

    ids = [t["ticket_id"] for t in tickets]
    labels = {t["ticket_id"]: f"{t['ticket_id']} · {t.get('title') or ''} · {badge(t.get('status'))}" for t in tickets}
    if st.session_state.get("my_selected_ticket") not in ids:
        st.session_state["my_selected_ticket"] = ids[0]
    tid = st.selectbox("Open a request", ids, format_func=lambda x: labels.get(x, x), key="my_selected_ticket")
    if not tid:
        return
    try:
        view = api_get(_ticket_path(tid))
    except Exception as exc:
        st.error(friendly_error(exc, ticket=True))
        return
    if isinstance(view, dict):
        render_user_ticket(view)


def render() -> None:
    user = current_user() or {}
    st.title("📋 My requests")
    st.caption(f"Welcome, {user.get('name') or user.get('username', '')}. Submit workplace event requests "
               "and follow their progress here.")
    flash = st.session_state.pop("my_flash", None)
    if flash:
        st.success(flash)
    tab_new, tab_list = st.tabs(["New request", "My requests"])
    with tab_new:
        new_request_section()
    with tab_list:
        my_requests_section()
