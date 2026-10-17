"""Approvals page: pending-approval queue and decision screen.

Approvers (role ``approver``) can Approve / Reject / Request changes with a deliberate two-step
confirmation. Admins see the same queue read-only. The backend is the only permission check and the only
source of truth: the UI never shows success unless the API confirmed it, and always reloads afterwards.
"""
from __future__ import annotations

import uuid
from typing import Optional
from urllib.parse import quote

import requests
import streamlit as st

from ui.lib.api import (
    APPROVAL_ACTIONS, APPROVAL_STATUSES, COMMENT_REQUIRED_ACTIONS, PO_DRAFT_LABEL, RISK_BADGE, ApiError,
    api_get, api_post, approval_badge, current_user, friendly_error,
)
from ui.lib.approval_render import (
    decision_line, fmt_amount, fmt_ts, render_decisions, render_reasons, render_snapshot, render_summary,
    safety_notice,
)

UNCONFIRMED = "Outcome could not be confirmed — reload before retrying."
CONFLICT = "This approval was already decided or the request changed — reloaded."
COMMENT_REQUIRED = "A comment is required to reject or request changes."
ADMIN_READ_ONLY = ("Read-only: admins can review the approval queue but cannot decide. "
                   "Decisions require an eligible approver.")


def _apath(approval_id: str) -> str:
    return f"/api/approvals/{quote(approval_id, safe='')}"


def _flash(kind: str, text: str) -> None:
    st.session_state["apr_flash"] = (kind, text)


def _is_approver() -> bool:
    return (current_user() or {}).get("role") == "approver"


# --------------------------------------------------------------------------- state
def _pending() -> Optional[dict]:
    p = st.session_state.get("apr_pending")
    return p if isinstance(p, dict) else None


def _sync_pending(view: Optional[dict]) -> None:
    """Drop a pending confirmation that no longer matches the server's current request."""
    p = _pending()
    if not p:
        return
    if (not isinstance(view, dict) or p.get("approval_id") != view.get("approval_id")
            or view.get("status") != "pending" or p.get("request_version") != view.get("request_version")
            or p.get("workflow_snapshot_hash") != view.get("workflow_snapshot_hash")):
        st.session_state.pop("apr_pending", None)


def load_detail(approval_id: str) -> Optional[dict]:
    """GET the approval from the server (source of truth) and store it for display."""
    try:
        data = api_get(_apath(approval_id))
    except Exception as exc:
        st.session_state.pop("apr_view", None)
        _sync_pending(None)
        if isinstance(exc, ApiError) and exc.status_code == 404:
            _flash("error", "Approval request not found.")
        else:
            _flash("error", friendly_error(exc))
        return None
    if not isinstance(data, dict) or not data.get("approval_id"):
        _flash("error", "Unexpected response from backend.")
        return None
    st.session_state["apr_view"] = data
    st.session_state["apr_selected"] = data["approval_id"]
    _sync_pending(data)
    return data


# --------------------------------------------------------------------------- queue
def queue_section() -> None:
    top1, top2 = st.columns([5, 1])
    top1.subheader("Approval queue")
    if top2.button("🔄 Refresh", key="apr_refresh"):
        sel = st.session_state.get("apr_selected")
        if sel:
            load_detail(sel)
        st.rerun()

    options = APPROVAL_STATUSES + ["All"]
    status_filter = st.selectbox(
        "Status filter", options, index=0,
        format_func=lambda s: "All statuses" if s == "All" else approval_badge(s), key="apr_status_filter",
    )
    params: dict = {"limit": 100}
    if status_filter != "All":
        params["status"] = status_filter
    try:
        data = api_get("/api/approvals", params=params)
    except Exception as exc:
        st.error(friendly_error(exc))
        return
    data = data if isinstance(data, dict) else {}
    counts = data.get("counts") or {}
    cols = st.columns(len(APPROVAL_STATUSES))
    for col, s in zip(cols, APPROVAL_STATUSES):
        col.metric(approval_badge(s), int(counts.get(s, 0) or 0))

    rows = [a for a in data.get("approvals") or [] if isinstance(a, dict) and a.get("approval_id")]
    if not rows:
        st.info("No approval requests match this filter.")
    else:
        st.dataframe([
            {
                "Approval": a.get("approval_id"),
                "Ticket": a.get("ticket_id"),
                "Summary": a.get("summary") or "—",
                "Amount": fmt_amount(a.get("amount"), a.get("currency")),
                "Risk": RISK_BADGE.get(a.get("risk_level"), a.get("risk_level") or "—"),
                "Reasons": ", ".join(a.get("reason_codes") or []) or "—",
                "Requester": a.get("requester_id") or "—",
                "Version": a.get("request_version"),
                "Status": approval_badge(a.get("status")),
                "Created": fmt_ts(a.get("created_at")),
                "Expires": fmt_ts(a.get("expires_at")),
            }
            for a in rows
        ], width="stretch", hide_index=True)

    labels = {a["approval_id"]: f"{a['approval_id']} · {a.get('ticket_id', '')} · "
                                f"{fmt_amount(a.get('amount'), a.get('currency'))} · {approval_badge(a.get('status'))}"
              for a in rows}

    def _pick() -> None:
        aid = st.session_state.get("apr_pick")
        if aid:
            load_detail(aid)

    st.selectbox("Open an approval request", [None] + list(labels.keys()),
                 format_func=lambda a: "(select an approval request)" if a is None else labels.get(a, a),
                 key="apr_pick", on_change=_pick)


# --------------------------------------------------------------------------- detail
def detail_section(view: dict) -> None:
    aid = view["approval_id"]
    st.divider()
    h1, h2 = st.columns([5, 1])
    h1.header(f"Approval {aid}")
    if h2.button("🔄 Reload", key="apr_reload_btn"):
        load_detail(aid)
        st.rerun()
    safety_notice(view.get("safety_notice"))
    st.markdown(f"**Status:** {approval_badge(view.get('status'))} · **Ticket:** `{view.get('ticket_id', '?')}` "
                f"· **Version:** {view.get('request_version', '?')} · **Amount:** "
                f"{fmt_amount(view.get('amount'), view.get('currency'))}")
    st.caption(f"Draft PO: {PO_DRAFT_LABEL}.")
    if view.get("reconciliation_required"):
        st.warning("⚠️ Decision recorded, but follow-up updates are incomplete — an admin must reconcile.")

    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Summary")
        render_summary(view)
    with c2:
        st.subheader("Why approval is required")
        render_reasons(view)
        st.subheader("Decision history")
        render_decisions(view)
    st.subheader("What is being approved (review snapshot)")
    render_snapshot(view)
    decision_section(view)


def decision_section(view: dict) -> None:
    st.subheader("🧑‍⚖️ Decision")
    if not _is_approver():
        st.info(ADMIN_READ_ONLY)
        if view.get("cannot_decide_reason"):
            st.caption(f"Server: {view['cannot_decide_reason']}")
        return
    if not view.get("can_decide"):
        reason = view.get("cannot_decide_reason") or (
            f"This request is {str(view.get('status', '?')).replace('_', ' ')} and cannot be decided.")
        st.warning(f"You cannot decide this request: {reason}")
        return

    pending = _pending()
    locked = pending is not None
    action = st.radio("Action", list(APPROVAL_ACTIONS.keys()), format_func=lambda a: APPROVAL_ACTIONS[a],
                      horizontal=True, key="apr_action", disabled=locked)
    comment = st.text_area("Comment (required for Reject and Request changes)", key="apr_comment",
                           max_chars=2000, disabled=locked)
    if not locked:
        if st.button("Continue to confirmation…", key="apr_prepare_btn"):
            text = (comment or "").strip()
            if action in COMMENT_REQUIRED_ACTIONS and not text:
                st.error(COMMENT_REQUIRED)
                return
            # One idempotency key per pending confirmation; retries/double clicks reuse it.
            st.session_state["apr_pending"] = {
                "approval_id": view["approval_id"],
                "action": action,
                "comment": text or None,
                "idempotency_key": uuid.uuid4().hex,
                "request_version": view.get("request_version"),
                "workflow_snapshot_hash": view.get("workflow_snapshot_hash"),
            }
            st.rerun()
        return
    confirm_section(view, pending)


def confirm_section(view: dict, pending: dict) -> None:
    label = APPROVAL_ACTIONS.get(pending["action"], pending["action"])
    with st.container(border=True):
        st.markdown(f"**Step 2 of 2 — confirm: {label}**")
        st.markdown(f"You are about to **{label.lower()}** approval `{pending['approval_id']}` "
                    f"(version {pending['request_version']}, amount "
                    f"{fmt_amount(view.get('amount'), view.get('currency'))}).")
        if pending.get("comment"):
            st.markdown(f"Comment: “{pending['comment']}”")
        st.caption("This records an internal authorization decision only. No order or payment is executed.")
        checked = st.checkbox("I have reviewed the snapshot above and want to record this decision.",
                              key="apr_confirm_check")
        inflight = bool(st.session_state.get("apr_inflight"))
        b1, b2 = st.columns(2)
        if b1.button(f"Confirm {label.lower()}", key="apr_confirm_btn", type="primary",
                     disabled=not checked or inflight):
            submit_decision(pending)
            st.rerun()
        if b2.button("Cancel", key="apr_cancel_btn", disabled=inflight):
            st.session_state.pop("apr_pending", None)
            st.session_state.pop("apr_confirm_check", None)
            st.rerun()


def submit_decision(pending: dict) -> None:
    aid = pending["approval_id"]
    body = {k: pending[k] for k in ("action", "comment", "idempotency_key", "request_version",
                                    "workflow_snapshot_hash")}
    st.session_state["apr_inflight"] = True
    try:
        data = api_post(f"{_apath(aid)}/decision", body, timeout=30)
    except ApiError as exc:
        st.session_state.pop("apr_inflight", None)
        if exc.status_code == 409:
            st.session_state.pop("apr_pending", None)
            load_detail(aid)
            _flash("warning", f"{CONFLICT} ({exc.detail})" if exc.detail else CONFLICT)
        elif exc.status_code >= 500:
            # Unknown outcome: keep the pending confirmation (same idempotency key) for a safe retry.
            _flash("error", UNCONFIRMED)
        else:
            st.session_state.pop("apr_pending", None)
            _flash("error", friendly_error(exc))
        return
    except (requests.ConnectionError, requests.Timeout):
        st.session_state.pop("apr_inflight", None)
        _flash("error", UNCONFIRMED)
        return
    except Exception:
        st.session_state.pop("apr_inflight", None)
        _flash("error", UNCONFIRMED)
        return
    st.session_state.pop("apr_inflight", None)
    st.session_state.pop("apr_pending", None)
    st.session_state.pop("apr_confirm_check", None)
    st.session_state.pop("apr_comment", None)
    data = data if isinstance(data, dict) else {}
    view = load_detail(aid)
    if view is None:
        _flash("error", UNCONFIRMED)
        return
    decisions = [d for d in view.get("decisions") or [] if isinstance(d, dict)]
    decision = next((d for d in decisions if d.get("idempotency_key") == pending["idempotency_key"]), None)
    if decision is None:
        # Server views may omit idempotency keys: accept only a matching recorded decision by this user.
        me = (current_user() or {}).get("username")
        decision = next((d for d in reversed(decisions)
                         if d.get("action") == pending["action"] and d.get("actor_id") == me
                         and d.get("request_version") in (None, pending["request_version"])), None)
    if decision is None:
        _flash("error", UNCONFIRMED)
        return
    msg = f"Server recorded: {decision_line(decision)}."
    if data.get("replayed"):
        msg += " (Already recorded earlier — no duplicate decision was created.)"
    st.session_state["apr_outcome"] = msg
    if data.get("reconciliation_required") or view.get("reconciliation_required"):
        _flash("warning", "Decision recorded, but follow-up updates are incomplete — an admin must reconcile.")


def render() -> None:
    st.title("🛡️ Approvals")
    if _is_approver():
        st.caption("Review pending approval requests and record your decision. "
                   f"Draft POs stay drafts — {PO_DRAFT_LABEL.split('— ', 1)[-1]}.")
    else:
        st.caption(ADMIN_READ_ONLY)
    outcome = st.session_state.pop("apr_outcome", None)
    if outcome:
        st.success(outcome)
    msg = st.session_state.pop("apr_flash", None)
    if msg:
        kind, text = msg
        {"error": st.error, "warning": st.warning}.get(kind, st.success)(text)
    queue_section()
    view = st.session_state.get("apr_view")
    if isinstance(view, dict) and view.get("approval_id"):
        detail_section(view)
    else:
        st.info("Select an approval request from the queue to review it.")
