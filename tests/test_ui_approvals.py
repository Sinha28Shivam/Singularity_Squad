"""Phase 2 approval UI tests (AppTest) against the in-memory fake backend from test_ui_portal.py."""
from __future__ import annotations

from unittest.mock import patch

import pytest

pytest.importorskip("streamlit.testing.v1")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from test_ui_portal import APP, SAFETY, FakeBackend, texts  # noqa: E402

UNCONFIRMED = "Outcome could not be confirmed — reload before retrying."
FORBIDDEN_CLAIMS = ("ordered", "purchased", "paid for", "payment executed", "payment was made")


@pytest.fixture(autouse=True)
def _clear_caches():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.fixture
def backend():
    be = FakeBackend()
    with patch("requests.get", side_effect=be.get), patch("requests.post", side_effect=be.post), \
            patch("requests.put", side_effect=be.put):
        yield be


def run(at: AppTest) -> AppTest:
    at.run()
    assert not at.exception, at.exception
    return at


def logged_in(backend: FakeBackend, username: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["auth"] = backend.login_token(username)
    return run(at)


def click(at: AppTest, key: str) -> AppTest:
    at.button(key=key).click()
    return run(at)


def keys(at: AppTest) -> set:
    return {b.key for b in at.button}


def decision_posts(backend: FakeBackend, aid: str) -> list:
    return backend.find("POST", f"/api/approvals/{aid}/decision")


def pending_ticket(backend: FakeBackend, **kw) -> tuple[str, str]:
    tid = backend.add_ticket(status="reviewed", run_count=1)
    return tid, backend.add_approval(tid, **kw)


def open_approval(backend: FakeBackend, aid: str, user: str = "carol") -> AppTest:
    at = logged_in(backend, user)
    at.selectbox(key="apr_pick").select(aid)
    return run(at)


def prepare(at: AppTest, action: str, comment: str = "") -> AppTest:
    at.radio(key="apr_action").set_value(action)
    if comment:
        at.text_area(key="apr_comment").input(comment)
    return click(at, "apr_prepare_btn")


def confirm(at: AppTest) -> AppTest:
    at.checkbox(key="apr_confirm_check").check()
    run(at)
    return click(at, "apr_confirm_btn")


NEGATIONS = ("nothing is purchased", "nothing purchased", "nothing paid", "nothing is paid", "no order placed")


def assert_no_false_claims(body: str) -> None:
    low = body.lower()
    for neg in NEGATIONS:
        low = low.replace(neg, "")
    for claim in FORBIDDEN_CLAIMS:
        assert claim not in low, claim


# --------------------------------------------------------------------------- navigation
def test_navigation_per_role(backend):
    at = logged_in(backend, "carol")
    body = texts(at)
    assert "🛡️ Approvals" in body
    assert "Admin console" not in body and "📋 My requests" not in body
    assert not any(c[1].startswith("/api/admin") or c[1].startswith("/api/my") for c in backend.calls)

    assert page_titles(at) == ["Approvals"]

    at = logged_in(backend, "admin")
    assert "🛠️ Admin console" in texts(at)
    assert page_titles(at) == ["Admin console", "Approvals (read-only)"]

    backend.calls.clear()
    at = logged_in(backend, "alice")
    body = texts(at)
    assert "📋 My requests" in body and "🛡️ Approvals" not in body
    assert page_titles(at) == ["My requests"]
    assert not backend.find("GET", "/api/approvals")


def page_titles(at: AppTest) -> list:
    return [str(info.get("page_name")) for info in at._registered_pages.values()]


def test_admin_approvals_page_is_read_only(backend):
    _, aid = pending_ticket(backend)
    # The admin's second navigation page is the same approvals.render callable; render it directly.
    from ui.pages_portal import approvals as page
    at2 = AppTest.from_function(_render_approvals_page, default_timeout=60)
    at2.session_state["auth"] = backend.login_token("admin")
    run(at2)
    assert page.ADMIN_READ_ONLY in texts(at2)
    at2.selectbox(key="apr_pick").select(aid)
    run(at2)
    body = texts(at2)
    assert aid in body and page.ADMIN_READ_ONLY in body
    assert "apr_prepare_btn" not in keys(at2) and "apr_confirm_btn" not in keys(at2)
    assert not at2.get("radio")
    assert not decision_posts(backend, aid)


def _render_approvals_page():
    from ui.pages_portal import approvals
    approvals.render()


# --------------------------------------------------------------------------- approver queue/detail
def test_approver_sees_queue_and_detail(backend):
    tid, aid = pending_ticket(backend, amount=59000.0)
    at = logged_in(backend, "carol")
    assert backend.find("GET", "/api/approvals")[-1][2] == {"limit": 100, "status": "pending"}
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["⏳ pending"] == "1"
    df = at.dataframe[0].value
    assert list(df["Approval"]) == [aid] and list(df["Ticket"]) == [tid]
    assert "₹" in list(df["Amount"])[0] and "AMOUNT_THRESHOLD" in list(df["Reasons"])[0]
    assert list(df["Requester"]) == ["alice"]

    at.selectbox(key="apr_pick").select(aid)
    run(at)
    assert backend.find("GET", f"/api/approvals/{aid}")
    body = texts(at)
    assert SAFETY in body
    assert "PO_REQUIRES_HUMAN_APPROVAL" in body and "exceeds the approval threshold" in body
    assert "Draft — no order placed, nothing paid" in body
    assert "No decisions recorded yet." in body
    summary = {r["Field"]: r["Value"] for r in at.dataframe[1].value.to_dict("records")}
    assert summary["Request version"] == "1" and summary["Requester"] == "alice"
    assert summary["Amount"].startswith("₹") and "medium" in summary["Risk level"]
    assert summary["Created"].startswith("2026-10-17") and summary["Expires"].startswith("2026-10-20")
    assert "apr_prepare_btn" in keys(at)
    assert_no_false_claims(body)

    at.selectbox(key="apr_status_filter").select("All")
    run(at)
    assert backend.find("GET", "/api/approvals")[-1][2] == {"limit": 100}


def test_no_post_before_confirmation_and_approve_shows_server_record(backend):
    tid, aid = pending_ticket(backend)
    at = open_approval(backend, aid)
    prepare(at, "approve")
    assert not decision_posts(backend, aid)
    assert "apr_confirm_btn" in keys(at)
    assert at.button(key="apr_confirm_btn").disabled  # checkbox not ticked yet
    at.checkbox(key="apr_confirm_check").check()
    run(at)
    assert not decision_posts(backend, aid)
    click(at, "apr_confirm_btn")

    posts = decision_posts(backend, aid)
    assert len(posts) == 1
    sent = posts[0][2]
    a = backend.approvals[aid]
    assert set(sent) == {"action", "comment", "idempotency_key", "request_version", "workflow_snapshot_hash"}
    assert sent["action"] == "approve" and sent["comment"] is None
    assert sent["request_version"] == 1 and sent["workflow_snapshot_hash"] == a["workflow_snapshot_hash"]
    assert len(sent["idempotency_key"]) >= 8
    # Reloaded from the server after submit and the server-recorded decision + timestamp shown.
    assert len(backend.find("GET", f"/api/approvals/{aid}")) >= 2
    success = " ".join(str(s.value) for s in at.main.success)
    assert "Server recorded" in success and "Approved" in success and "carol" in success
    assert "2026-10-17 05:30:00" in success
    assert backend.tickets[tid]["status"] == "approved"
    assert "apr_prepare_btn" not in keys(at)
    assert "already approved" in " ".join(str(w.value) for w in at.warning)
    assert_no_false_claims(texts(at))


def test_cancel_confirmation_sends_nothing(backend):
    _, aid = pending_ticket(backend)
    at = open_approval(backend, aid)
    prepare(at, "approve")
    click(at, "apr_cancel_btn")
    assert "apr_prepare_btn" in keys(at) and "apr_confirm_btn" not in keys(at)
    assert not decision_posts(backend, aid)


@pytest.mark.parametrize("action", ["reject", "request_changes"])
def test_comment_required_client_side(backend, action):
    _, aid = pending_ticket(backend)
    at = open_approval(backend, aid)
    prepare(at, action)
    assert any("A comment is required to reject or request changes." in str(e.value) for e in at.error)
    assert "apr_confirm_btn" not in keys(at)
    assert not decision_posts(backend, aid)

    at.text_area(key="apr_comment").input("   ")
    click(at, "apr_prepare_btn")
    assert "apr_confirm_btn" not in keys(at)

    prepare(at, action, "Vendor quote is outdated")
    confirm(at)
    sent = decision_posts(backend, aid)[-1][2]
    assert sent["action"] == action and sent["comment"] == "Vendor quote is outdated"
    expected = {"reject": "rejected", "request_changes": "changes_requested"}[action]
    assert backend.approvals[aid]["status"] == expected
    assert "Vendor quote is outdated" in texts(at)


def test_503_shows_no_success_and_retry_reuses_idempotency_key(backend):
    _, aid = pending_ticket(backend)
    backend.decision_faults = ["commit_503"]  # committed server-side, but the response was lost
    at = open_approval(backend, aid)
    prepare(at, "approve")
    confirm(at)
    errors = " ".join(str(e.value) for e in at.error)
    assert UNCONFIRMED in errors
    assert not at.main.success
    assert "Server recorded" not in texts(at)
    first_key = decision_posts(backend, aid)[0][2]["idempotency_key"]
    assert "apr_confirm_btn" in keys(at)  # still pending confirmation for a safe retry

    click(at, "apr_confirm_btn")
    posts = decision_posts(backend, aid)
    assert len(posts) == 2
    assert posts[1][2]["idempotency_key"] == first_key
    assert posts[1][2] == posts[0][2]
    assert len(backend.approvals[aid]["decisions"]) == 1  # replayed, no duplicate
    success = " ".join(str(s.value) for s in at.main.success)
    assert "Server recorded" in success and "no duplicate decision" in success


def test_503_without_commit_never_shows_success(backend):
    _, aid = pending_ticket(backend)
    backend.decision_faults = [503, 503]
    at = open_approval(backend, aid)
    prepare(at, "approve")
    confirm(at)
    click(at, "apr_confirm_btn")  # double click / retry
    posts = decision_posts(backend, aid)
    assert len(posts) == 2 and posts[0][2]["idempotency_key"] == posts[1][2]["idempotency_key"]
    assert not at.main.success and UNCONFIRMED in " ".join(str(e.value) for e in at.error)
    assert backend.approvals[aid]["status"] == "pending"


def test_connection_error_is_unconfirmed(backend):
    import requests as rq
    _, aid = pending_ticket(backend)
    at = open_approval(backend, aid)
    prepare(at, "approve")
    at.checkbox(key="apr_confirm_check").check()
    run(at)
    original = backend.post

    def boom(url, json=None, headers=None, timeout=None, **kw):
        if url.endswith("/decision"):
            raise rq.ConnectionError("down")
        return original(url, json=json, headers=headers, timeout=timeout, **kw)

    with patch("requests.post", side_effect=boom):
        click(at, "apr_confirm_btn")
    assert not at.main.success and UNCONFIRMED in " ".join(str(e.value) for e in at.error)


def test_409_shows_warning_and_reloads(backend):
    _, aid = pending_ticket(backend)
    at = open_approval(backend, aid)
    prepare(at, "approve")
    # Another approver decides concurrently.
    a = backend.approvals[aid]
    a["status"] = "rejected"
    a["decisions"].append({"decision_id": "DEC-000000000099", "action": "reject", "actor_id": "dave",
                           "actor_role": "approver", "comment": "Over budget", "decided_at": "2026-10-17T05:00:00+00:00",
                           "idempotency_key": "other-key-123", "request_version": 1,
                           "workflow_snapshot_hash": a["workflow_snapshot_hash"]})
    gets_before = len(backend.find("GET", f"/api/approvals/{aid}"))
    confirm(at)
    assert len(backend.find("GET", f"/api/approvals/{aid}")) > gets_before
    warnings = " ".join(str(w.value) for w in at.warning)
    assert "already decided or the request changed — reloaded" in warnings
    assert not at.main.success
    body = texts(at)
    assert "Over budget" in body and "dave" in body
    assert "apr_confirm_btn" not in keys(at) and "apr_prepare_btn" not in keys(at)


def test_stale_version_409_reloads(backend):
    tid, aid = pending_ticket(backend)
    at = open_approval(backend, aid)
    prepare(at, "approve")
    backend.approvals[aid]["workflow_snapshot_hash"] = "f" * 64  # request changed under us
    confirm(at)
    assert "already decided or the request changed" in " ".join(str(w.value) for w in at.warning)
    assert backend.approvals[aid]["status"] == "pending"
    assert not at.main.success


def test_cannot_decide_reason_shown_for_own_request(backend):
    backend.users["carol"]["role"] = "approver"
    tid = backend.add_ticket(owner="carol", status="reviewed", run_count=1)
    aid = backend.add_approval(tid)
    at = open_approval(backend, aid)
    assert "You cannot decide your own request." in " ".join(str(w.value) for w in at.warning)
    assert "apr_prepare_btn" not in keys(at)
    assert not decision_posts(backend, aid)


def test_reconciliation_warning_after_decision(backend):
    _, aid = pending_ticket(backend)
    backend.reconcile_on_decide = True
    at = open_approval(backend, aid)
    prepare(at, "approve")
    confirm(at)
    assert "Server recorded" in " ".join(str(s.value) for s in at.main.success)
    assert "reconcile" in " ".join(str(w.value) for w in at.warning)


# --------------------------------------------------------------------------- requester
def test_requester_sees_approval_panel_without_decision_controls(backend):
    tid, aid = pending_ticket(backend)
    at = logged_in(backend, "alice")
    body = texts(at)
    assert "🛡️ Approval" in body and "pending" in body
    assert "PO_REQUIRES_HUMAN_APPROVAL" in body and "exceeds the approval threshold" in body
    assert SAFETY in body
    assert any("₹" in str(m.value) for m in at.metric)
    assert "version 1" in body
    k = keys(at)
    assert not {"apr_prepare_btn", "apr_confirm_btn", "admin_approve_btn", "admin_reject_btn"} & k
    assert not any("approve" in (b.label or "").lower() for b in at.button)
    assert not backend.find("GET", "/api/approvals")
    assert not any(c[0] == "POST" and "/api/approvals" in c[1] for c in backend.calls)
    assert not [f for f in at.get("form_submit_button") if f.label == "Resubmit request"]
    assert_no_false_claims(body)


def test_requester_changes_requested_can_edit_and_resubmit(backend):
    tid, aid = pending_ticket(backend)
    a = backend.approvals[aid]
    a["status"] = "changes_requested"
    a["decisions"].append({"decision_id": "DEC-000000000001", "action": "request_changes", "actor_id": "carol",
                           "actor_role": "approver", "comment": "Please reduce headcount to 80",
                           "decided_at": "2026-10-17T05:30:00+00:00", "idempotency_key": "k" * 16,
                           "request_version": 1, "workflow_snapshot_hash": a["workflow_snapshot_hash"]})
    backend.tickets[tid]["status"] = "changes_requested"
    at = logged_in(backend, "alice")
    body = texts(at)
    assert "Please reduce headcount to 80" in body and "Changes requested" in body
    form = f"edit_ticket_form_{tid}"
    at.number_input(key=f"{form}_attendees").set_value(80)
    btn = next(b for b in at.get("form_submit_button") if b.form_id == form)
    btn.click()
    run(at)
    sent = backend.find("PUT", f"/api/my/tickets/{tid}")[-1][2]
    assert sent["attendees"] == 80
    assert backend.tickets[tid]["status"] == "submitted"


def test_requester_approval_expired_can_resubmit(backend):
    tid, aid = pending_ticket(backend)
    backend.approvals[aid]["status"] = "expired"
    backend.tickets[tid]["status"] = "approval_expired"
    at = logged_in(backend, "alice")
    body = texts(at)
    assert "⌛" in body and "expired" in body
    assert [f for f in at.get("form_submit_button") if f.label == "Resubmit request"]


# --------------------------------------------------------------------------- admin console
def test_admin_console_has_no_approve_and_reject_only_for_reviewed(backend):
    tid, aid = pending_ticket(backend)
    reviewed = backend.add_ticket(status="reviewed", run_count=1)
    at = logged_in(backend, "admin")
    at.selectbox(key="admin_pick").select(tid)
    run(at)
    k = keys(at)
    assert "admin_approve_btn" not in k and "admin_reject_btn" not in k
    assert not any("approve" in (b.label or "").lower() for b in at.button)
    body = texts(at)
    assert "Waiting for an eligible approver" in body and SAFETY in body and "PO_REQUIRES_HUMAN_APPROVAL" in body
    assert "admin_reconcile_btn" not in k

    at.selectbox(key="admin_pick").select(reviewed)
    run(at)
    assert "admin_reject_btn" in keys(at) and "admin_approve_btn" not in keys(at)
    click(at, "admin_reject_btn")
    assert backend.find("POST", f"/api/admin/tickets/{reviewed}/decision")[-1][2]["decision"] == "reject"
    assert not any(c[0] == "POST" and c[2] and c[2].get("decision") == "approve" for c in backend.calls)


def test_admin_reconcile_button(backend):
    tid, aid = pending_ticket(backend)
    a = backend.approvals[aid]
    a.update(status="approved", reconciliation_required=True)
    backend.tickets[tid]["status"] = "pending_approval"
    at = logged_in(backend, "admin")
    at.selectbox(key="admin_pick").select(tid)
    run(at)
    assert "reconciliation required" in " ".join(str(w.value) for w in at.warning)
    click(at, "admin_reconcile_btn")
    assert backend.find("POST", f"/api/admin/approvals/{aid}/reconcile")
    assert not backend.approvals[aid]["reconciliation_required"]
    assert "admin_reconcile_btn" not in keys(at)
    assert "Reconciliation finished" in " ".join(str(s.value) for s in at.main.success)


def test_status_badges_for_new_statuses(backend):
    from ui.lib.api import badge
    assert badge("pending_approval").startswith("⏳🛡️")
    assert badge("changes_requested").startswith("✏️")
    assert badge("approval_expired").startswith("⌛")
    backend.add_ticket(status="pending_approval")
    at = logged_in(backend, "admin")
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["⏳🛡️ pending approval"] == "1"
    assert "⌛ approval expired" in metrics and "✏️ changes requested" in metrics
