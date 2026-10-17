"""HTTP helpers for the WorkplaceOps AI backend.

Every call goes through module-level ``requests.get/post/put`` (tests patch those) and attaches
``Authorization: ******`` when the user is logged in. A 401 on an authenticated call means the
session expired: the stored auth is cleared and the app reruns into the login page.
"""
from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import quote

import requests
import streamlit as st

API_BASE = os.environ.get("WORKPLACEOPS_API", "http://127.0.0.1:8000").rstrip("/")
GET_TIMEOUT = 10
POST_TIMEOUT = 120
SESSION_EXPIRED = "Session expired — please log in again."

BACKEND_HELP = (
    f"Cannot reach the WorkplaceOps AI backend at `{API_BASE}`.\n\n"
    "Start it from the project root:\n\n"
    "```\npython -m uvicorn app.main:app --reload\n```\n\n"
    "Or point the UI elsewhere with the `WORKPLACEOPS_API` environment variable."
)

# Mirrors app/portal_schemas.py (the UI deliberately does not import the backend package).
TICKET_STATUSES = [
    "submitted", "processing", "reviewed", "needs_info",
    "pending_approval", "changes_requested", "approval_expired",
    "approved", "rejected", "failed", "persistence_error",
]
STATUS_BADGE = {
    "submitted": "⏳",
    "processing": "⚙️",
    "reviewed": "🔎",
    "needs_info": "✏️",
    "pending_approval": "⏳🛡️",
    "changes_requested": "✏️",
    "approval_expired": "⌛",
    "approved": "✅",
    "rejected": "⛔",
    "failed": "⚠️",
    "persistence_error": "⚠️",
}
USER_EDITABLE_STATUSES = {"submitted", "needs_info", "changes_requested", "approval_expired"}

# Mirrors app/approvals/schemas.py.
APPROVAL_STATUSES = ["pending", "approved", "rejected", "changes_requested", "expired", "superseded"]
APPROVAL_STATUS_BADGE = {
    "pending": "⏳",
    "approved": "✅",
    "rejected": "⛔",
    "changes_requested": "✏️",
    "expired": "⌛",
    "superseded": "↪️",
}
APPROVAL_ACTIONS = {"approve": "Approve", "reject": "Reject", "request_changes": "Request changes"}
COMMENT_REQUIRED_ACTIONS = {"reject", "request_changes"}
APPROVAL_SAFETY_NOTICE = "Approval records internal authorization only. No order or payment is executed."
PO_DRAFT_LABEL = "Draft — no order placed, nothing paid"
APPROVED_INTERNALLY = ("Approved internally — purchase order remains a draft; "
                       "no order or payment has been executed.")
RISK_BADGE = {"low": "🟢 low", "medium": "🟠 medium", "high": "🔴 high"}


def approval_badge(status: Optional[str]) -> str:
    return f"{APPROVAL_STATUS_BADGE.get(status or '', '❔')} {(status or 'unknown').replace('_', ' ')}"
EVENT_CATEGORY_LABELS: dict[str, str] = {
    "team_lunch": "Team lunch",
    "offsite": "Team offsite",
    "celebration": "Celebration / party",
    "training_workshop": "Training / workshop",
    "town_hall": "Town hall / all-hands",
    "client_visit": "Client visit",
    "other": "Other event",
}
SERVICES = ["lunch", "breakfast", "dinner", "snacks"]


def badge(status: Optional[str]) -> str:
    return f"{STATUS_BADGE.get(status or '', '❔')} {(status or 'unknown').replace('_', ' ')}"


class ApiError(Exception):
    """Friendly backend error carrying the HTTP status code and the backend's `detail` message."""

    def __init__(self, status_code: int, detail: str, body: Any = None) -> None:
        super().__init__(f"HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail
        self.body = body  # parsed JSON error body (e.g. FastAPI validation entries), if any


def _extract_detail(resp: Any) -> str:
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict) and body.get("detail") is not None:
        detail = body["detail"]
        if isinstance(detail, list):  # FastAPI validation errors
            parts = []
            for d in detail:
                if isinstance(d, dict):
                    loc = [str(x) for x in d.get("loc") or [] if x not in ("body", "query", "path")]
                    msg = str(d.get("msg", d))
                    parts.append(f"{'.'.join(loc)}: {msg}" if loc else msg)
                else:
                    parts.append(str(d))
            detail = "; ".join(parts)
        return str(detail)[:500]
    text = getattr(resp, "text", "") or ""
    # Never surface HTML pages or tracebacks verbatim.
    if "Traceback" in text or text.lstrip().startswith("<"):
        return "The backend reported an internal error."
    return text[:500] or "No details provided."


def current_auth() -> Optional[dict]:
    auth = st.session_state.get("auth")
    return auth if isinstance(auth, dict) and auth.get("token") else None


def current_user() -> Optional[dict]:
    auth = current_auth()
    user = auth.get("user") if auth else None
    return user if isinstance(user, dict) else None


def _headers() -> dict:
    auth = current_auth()
    return {"Authorization": f"Bearer {auth['token']}"} if auth else {}


def clear_session(notice: Optional[str] = None) -> None:
    for key in list(st.session_state.keys()):
        if key != "auth_notice":
            del st.session_state[key]
    if notice:
        st.session_state["auth_notice"] = notice


def _handle(resp: Any, authenticated: bool) -> Any:
    status = int(getattr(resp, "status_code", 200) or 200)
    if status == 401 and authenticated:
        clear_session(SESSION_EXPIRED)
        st.rerun()
    if status >= 400:
        try:
            body = resp.json()
        except Exception:
            body = None
        raise ApiError(status, _extract_detail(resp), body if isinstance(body, (dict, list)) else None)
    if status == 204:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def api_get(path: str, params: Optional[dict] = None) -> Any:
    headers = _headers()
    return _handle(requests.get(f"{API_BASE}{path}", params=params, headers=headers, timeout=GET_TIMEOUT),
                   bool(headers))


def api_post(path: str, payload: Optional[dict] = None, timeout: int = POST_TIMEOUT) -> Any:
    headers = _headers()
    return _handle(requests.post(f"{API_BASE}{path}", json=payload or {}, headers=headers, timeout=timeout),
                   bool(headers))


def api_put(path: str, payload: dict) -> Any:
    headers = _headers()
    return _handle(requests.put(f"{API_BASE}{path}", json=payload, headers=headers, timeout=POST_TIMEOUT),
                   bool(headers))


UPLOAD_TIMEOUT = 300


def api_post_files(path: str, files: list, data: Optional[dict] = None, timeout: int = UPLOAD_TIMEOUT) -> Any:
    """Multipart POST. ``files`` is a list of ``("files", (name, bytes, mime))`` tuples (requests format)."""
    headers = _headers()
    return _handle(requests.post(f"{API_BASE}{path}", files=files, data=data or {}, headers=headers,
                                 timeout=timeout), bool(headers))


def friendly_error(exc: Exception, ticket: bool = False) -> str:
    """Turn an exception into a user-facing message. ``ticket=True`` for ticket-ID lookups."""
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return BACKEND_HELP
    if isinstance(exc, ApiError):
        if exc.status_code == 404 and ticket:
            return "Ticket not found."
        if exc.status_code == 422 and ticket:
            return "Invalid ticket ID format (expected e.g. `REQ-20261017-9F3A1C2B`)."
        if exc.status_code == 413:
            return f"File too large: {exc.detail}" if exc.detail else "File too large."
        if exc.status_code == 503:
            return f"⛔ {exc.detail or 'Database unavailable.'}"
        if exc.status_code == 409:
            return f"⚠️ Not allowed right now: {exc.detail}"
        if exc.status_code == 403:
            return exc.detail or "Admin access required."
        if exc.status_code in (401, 404):
            return exc.detail
        if exc.status_code == 422:
            return f"Please check the form: {exc.detail}"
        return f"Backend returned HTTP {exc.status_code}: {exc.detail}"
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"Backend returned HTTP {exc.response.status_code}: {_extract_detail(exc.response)}"
    return f"Unexpected error talking to backend ({type(exc).__name__})."


def show_api_error(exc: Exception, ticket: bool = False) -> None:
    st.error(friendly_error(exc, ticket=ticket))


# --------------------------------------------------------------------------- payments (SIMULATION ONLY)
# Mirrors app/payments/schemas.py. Nothing here can move money: the backend only talks to a local simulator.
PAYMENTS_BASE = "/api/payments/simulation"
PAYMENT_POST_TIMEOUT = 30
SIMULATION_BANNER = "SIMULATED — NO MONEY TRANSFERRED"
PAYMENT_STATUS_LABELS = {
    "submitted_to_simulator": "Submitted to simulator",
    "accepted_by_simulator": "Accepted by simulator",
    "processing_simulated": "Simulated processing",
    "failed_simulated": "Simulation failed (retryable)",
}


def payment_status_label(status: Optional[str]) -> str:
    return PAYMENT_STATUS_LABELS.get(status or "", (status or "unknown").replace("_", " "))


def _ppath(payment_id: str) -> str:
    return f"{PAYMENTS_BASE}/{quote(payment_id, safe='')}"


def eligibility(ticket_id: str, invoice_id: str) -> Any:
    return api_get(f"{PAYMENTS_BASE}/eligibility/{quote(invoice_id, safe='')}", params={"ticket_id": ticket_id})


def create_simulation(body: dict) -> Any:
    """POST the simulation request. Returns PaymentSimulationOut (201 new / 200 idempotent replay)."""
    return api_post(PAYMENTS_BASE, body, timeout=PAYMENT_POST_TIMEOUT)


def get_payment(payment_id: str) -> Any:
    data = api_get(_ppath(payment_id))
    return data.get("payment") if isinstance(data, dict) and isinstance(data.get("payment"), dict) else data


def get_payment_events(payment_id: str) -> list:
    data = api_get(f"{_ppath(payment_id)}/events")
    if isinstance(data, dict):
        data = data.get("events")
    return [e for e in data or [] if isinstance(e, dict)]


def retry_payment(payment_id: str) -> Any:
    return api_post(f"{_ppath(payment_id)}/retry", {"confirmed_by_user": True}, timeout=PAYMENT_POST_TIMEOUT)


def list_payments(ticket_id: Optional[str] = None) -> list:
    data = api_get(PAYMENTS_BASE, params={"ticket_id": ticket_id} if ticket_id else None)
    if isinstance(data, dict):
        data = data.get("payments") or data.get("items")
    rows = []
    for p in data or []:
        if isinstance(p, dict) and isinstance(p.get("payment"), dict):
            p = p["payment"]
        if isinstance(p, dict):
            rows.append(p)
    return rows


def payment_error(exc: Exception) -> dict:
    """Normalise a payment API failure to ``{error, message, reasons[{code, message}]}`` for display."""
    if isinstance(exc, ApiError) and isinstance(exc.body, dict) and (exc.body.get("error") or exc.body.get("message")):
        reasons = [r for r in exc.body.get("reasons") or [] if isinstance(r, dict)]
        return {"error": exc.body.get("error"), "message": str(exc.body.get("message") or exc.detail)[:500],
                "reasons": [{"code": str(r.get("code") or "?"), "message": str(r.get("message") or "")[:300]}
                            for r in reasons],
                "status_code": exc.status_code}
    return {"error": None, "message": friendly_error(exc), "reasons": [],
            "status_code": getattr(exc, "status_code", None)}


@st.cache_data(ttl=15, show_spinner=False)
def fetch_health() -> Optional[dict]:
    try:
        return requests.get(f"{API_BASE}/api/health", timeout=GET_TIMEOUT).json()
    except Exception:
        return None


@st.cache_data(ttl=60, show_spinner=False)
def fetch_invoices() -> list:
    try:
        data = requests.get(f"{API_BASE}/api/invoices", timeout=GET_TIMEOUT).json()
        return data if isinstance(data, list) else []
    except Exception:
        return []
