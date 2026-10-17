"""Ticket service: runs the multi-agent workflow and persists ticket, draft PO and audit trail.

Write order: create ticket (status ``received``) -> run workflow -> upsert draft PO -> append audit
events -> update ticket with the final snapshot (``persisted=True``). The last write is the commit point.

Persistence is NOT atomic across collections. If a write fails after the ticket was created, the
ticket is left with status ``persistence_error`` (or ``failed`` if the workflow itself raised), its
``workflow_result`` stays empty, and the caller receives a PersistenceError (HTTP 503). A PO or some
audit events may already exist for that ticket; they are diagnosable via ``ticket_id``.
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import date, datetime, timezone
from typing import Optional

from app.db import PersistenceError, ensure_indexes, get_client, persistence_mode
from app.orchestrator import run_workflow
from app.portal_schemas import (
    EVENT_CATEGORY_LABELS,
    RUNNABLE_STATUSES,
    USER_EDITABLE_STATUSES,
    AdminTicketView,
    Decision,
    TicketListItem,
    TicketNote,
    TicketSubmission,
    UserPublic,
    UserTicketView,
)
from app.repositories import (
    append_audit_entries,
    count_tickets_by_status,
    create_submitted_ticket,
    create_ticket,
    get_last_sequence,
    get_ticket,
    list_tickets,
    list_tickets_for_owner,
    mark_ticket_status,
    push_note,
    save_run_result,
    set_decision,
    set_po_approval,
    set_status,
    update_submission,
    update_ticket_result,
    upsert_draft_po,
)
from app.schemas import AuditEntry, WorkflowResult
from app.services.errors import TicketError

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_PERSIST_FAILED_MSG = "Workflow result could not be saved. Please try again later."


def new_ticket_id() -> str:
    return f"REQ-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(4).upper()}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


_indexed_client_id: Optional[int] = None


def _ensure_indexes_once() -> None:
    # Startup may have run while MongoDB was down; make sure unique indexes exist before the first write.
    global _indexed_client_id
    client_id = id(get_client())
    if _indexed_client_id != client_id:
        ensure_indexes()
        _indexed_client_id = client_id


def _mark_best_effort(ticket_id: str, status: str, error: str) -> None:
    try:
        mark_ticket_status(ticket_id, status, error=error)
    except Exception as exc:  # noqa: BLE001 - best effort only
        logger.warning("Could not mark ticket %s as %s (%s)", ticket_id, status, type(exc).__name__)


def run_and_persist(text: str, today: Optional[date] = None,
                    invoice_id: Optional[str] = None) -> WorkflowResult:
    ticket_id = new_ticket_id()

    if persistence_mode() == "disabled":
        result = run_workflow(text, today, invoice_id, request_id=ticket_id)
        result.ticket_id = ticket_id
        result.persisted = False
        return result

    _ensure_indexes_once()
    now = _now()
    create_ticket({
        "ticket_id": ticket_id,
        "created_at": now,
        "updated_at": now,
        "status": "received",
        "raw_text": text,
        "workflow_result": None,
        "schema_version": SCHEMA_VERSION,
    })

    try:
        result = run_workflow(text, today, invoice_id, request_id=ticket_id)
    except Exception as exc:
        logger.error("Workflow failed for ticket %s (%s)", ticket_id, type(exc).__name__)
        _mark_best_effort(ticket_id, "failed", f"Workflow error: {type(exc).__name__}")
        raise

    result.ticket_id = ticket_id
    try:
        persist_result(ticket_id, result)
    except Exception as exc:
        result.persisted = False
        logger.error("Persistence failed for ticket %s (%s)", ticket_id, type(exc).__name__)
        _mark_best_effort(ticket_id, "persistence_error", f"Persistence error: {type(exc).__name__}")
        if isinstance(exc, PersistenceError):
            raise
        raise PersistenceError(_PERSIST_FAILED_MSG) from exc
    return result


def persist_result(ticket_id: str, result: WorkflowResult, commit=None) -> None:
    """Write draft PO and audit trail, then commit the ticket snapshot with persisted=True.

    The ticket update is the commit point: the stored snapshot only says persisted=True once every
    required write succeeded, so the API response and the stored document always agree. Each write is
    idempotent (PO upsert by po_id; identical audit events are ignored), so retrying this function for
    the same ticket does not duplicate the PO or audit sequence.

    ``commit(snapshot_dict, updated_at)`` replaces the default legacy ticket update (portal runs also
    record status/run metadata in the same write).
    """
    updated_at = _now()
    if result.procurement and result.procurement.po:
        upsert_draft_po(ticket_id, result.procurement.po, updated_at)
    append_audit_entries(ticket_id, result.audit_trail)
    result.persisted = True
    try:
        if commit is None:
            update_ticket_result(ticket_id, result.model_dump(mode="json"), result.final_status, updated_at)
        else:
            commit(result.model_dump(mode="json"), updated_at)
    except Exception:
        result.persisted = False
        raise


# ---------------------------------------------------------------------------------------------
# Role-based portal lifecycle
# ---------------------------------------------------------------------------------------------
_SERVICE_ORDER = ["breakfast", "lunch", "dinner", "snacks"]  # precedence used by the intake parser
_SERVICE_NEUTRAL = {"breakfast": "morning meal", "lunch": "midday meal", "dinner": "evening meal",
                    "snacks": "refreshments"}
_NOT_FOUND = "Ticket not found."


def _inr(amount: float) -> str:
    """Indian digit grouping, e.g. 100000 -> '1,00,000'; keeps paise when present."""
    whole = int(amount)
    paise = round((amount - whole) * 100)
    if paise == 100:
        whole, paise = whole + 1, 0
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups + [tail])
    return f"{s}.{paise:02d}" if paise else s


def _neutralize_services(text: str, service: str) -> str:
    """Free text must not override the structured service: the intake parser picks the first
    service keyword in a fixed order, so keywords ranked before the chosen one are reworded."""
    idx = _SERVICE_ORDER.index(service) if service in _SERVICE_ORDER else len(_SERVICE_ORDER)
    for other in _SERVICE_ORDER[:idx]:
        pattern = r"\bsnacks?\b" if other == "snacks" else r"\b" + other + r"\b"
        text = re.sub(pattern, _SERVICE_NEUTRAL[other], text, flags=re.IGNORECASE)
    return text


def build_request_text(sub: TicketSubmission) -> str:
    """Render a structured submission as request text for the intake agent.

    Structured values come first so the deterministic parser (first match wins for date, attendees
    and budget) always reads them; free text (title, location, notes) follows and cannot override them.
    """
    service = sub.service
    parts = [
        f"Organize {service} for {sub.attendees} employees on {sub.event_date.isoformat()}.",
        f"Keep the total under Rs {_inr(sub.budget_inr)}.",
    ]
    if sub.approved_vendor_only:
        parts.append("Use an approved vendor and follow the workplace event policy.")
    else:
        parts.append("Follow the workplace event policy.")
    if sub.location:
        loc = sub.location
        if not re.search(r"\b(office|campus)\b", loc, re.IGNORECASE):
            loc = f"{loc} office"
        if not loc.lower().startswith("the "):
            loc = f"the {loc}"
        parts.append(_neutralize_services(f"Venue: at {loc}.", service))
    label = EVENT_CATEGORY_LABELS.get(sub.event_category, "Other event")
    if sub.event_category == "team_lunch" and service != "lunch":
        label = "Team meal"  # the category name must not compete with the chosen catering service
    parts.append(f"Event: {label} — {_neutralize_services(sub.title, service)}.")
    if sub.notes:
        parts.append(_neutralize_services(f"Additional notes: {sub.notes}", service))
    return " ".join(parts)


def _require_persistence() -> None:
    if persistence_mode() == "disabled":
        raise TicketError(503, "Persistence is disabled")
    _ensure_indexes_once()


def _load(ticket_id: str) -> dict:
    doc = get_ticket(ticket_id)
    if doc is None:
        raise TicketError(404, _NOT_FOUND)
    return doc


def _load_owned(ticket_id: str, user: UserPublic) -> dict:
    doc = _load(ticket_id)
    if doc.get("owner_username") != user.username:
        raise TicketError(404, _NOT_FOUND)  # do not reveal other users' tickets
    return doc


def _audit_event(ticket_id: str, agent: str, action: str, detail: str, status: str = "info") -> None:
    seq = get_last_sequence(ticket_id) + 1
    append_audit_entries(ticket_id, [AuditEntry(step=seq, agent=agent, action=action, status=status,
                                                detail=detail)])


def submit_ticket(user: UserPublic, sub: TicketSubmission) -> dict:
    _require_persistence()
    ticket_id = new_ticket_id()
    doc = create_submitted_ticket(
        ticket_id, owner_username=user.username, owner_name=user.name, owner_email=user.email,
        submission=sub.model_dump(mode="json"), raw_text=build_request_text(sub),
    )
    append_audit_entries(ticket_id, [AuditEntry(
        step=1, agent="User", action="submit_ticket", status="info",
        detail=f"Ticket submitted by {user.username}; awaiting admin review.")])
    return doc


def _shift_audit(result: WorkflowResult, offset: int) -> None:
    if offset:
        result.audit_trail = [e.model_copy(update={"step": e.step + offset}) for e in result.audit_trail]


def run_ticket(ticket_id: str, admin: UserPublic, invoice_id: Optional[str] = None,
               today: Optional[date] = None) -> dict:
    _require_persistence()
    doc = _load(ticket_id)
    if doc.get("status") not in RUNNABLE_STATUSES:
        raise TicketError(409, f"Ticket cannot be run while status is '{doc.get('status')}'.")
    if not set_status(ticket_id, "processing", expected=RUNNABLE_STATUSES):
        raise TicketError(409, "Ticket is already being processed or is no longer runnable.")

    try:
        result = run_workflow(doc.get("raw_text") or "", today, invoice_id, request_id=ticket_id)
    except Exception as exc:
        logger.error("Workflow failed for ticket %s (%s)", ticket_id, type(exc).__name__)
        _mark_best_effort(ticket_id, "failed", f"Workflow error: {type(exc).__name__}")
        raise TicketError(500, "Workflow failed; the ticket was marked as failed.") from None

    result.ticket_id = ticket_id
    new_status = "needs_info" if result.final_status == "needs_clarification" else "reviewed"
    try:
        _shift_audit(result, get_last_sequence(ticket_id))
        persist_result(ticket_id, result, commit=lambda snap, at: save_run_result(
            ticket_id, snap, new_status, admin.username, at))
    except Exception as exc:
        result.persisted = False
        logger.error("Persistence failed for ticket %s (%s)", ticket_id, type(exc).__name__)
        _mark_best_effort(ticket_id, "persistence_error", f"Persistence error: {type(exc).__name__}")
        if isinstance(exc, PersistenceError):
            raise
        raise PersistenceError(_PERSIST_FAILED_MSG) from exc
    return _load(ticket_id)


def decide_ticket(ticket_id: str, admin: UserPublic, decision: str, note: Optional[str] = None) -> dict:
    _require_persistence()
    if decision not in ("approve", "reject"):
        raise TicketError(422, "Decision must be 'approve' or 'reject'.")
    doc = _load(ticket_id)
    if doc.get("status") != "reviewed":
        raise TicketError(409, f"Ticket cannot be decided while status is '{doc.get('status')}'.")
    wr = doc.get("workflow_result") or {}
    po = (wr.get("procurement") or {}).get("po")
    if decision == "approve":
        if not po:
            raise TicketError(409, "Cannot approve: no draft PO was produced for this ticket.")
        if wr.get("final_status") not in ("pass", "warning"):
            raise TicketError(409, f"Cannot approve: workflow outcome is '{wr.get('final_status')}'.")

    at = _now()
    note = (note or "").strip() or None
    record = Decision(decision=decision, by=admin.username, at=at, note=note).model_dump()
    new_status = "approved" if decision == "approve" else "rejected"
    if not set_decision(ticket_id, record, new_status, expected={"reviewed"}):
        raise TicketError(409, "Ticket status changed; please reload and try again.")
    if po:
        approval = "approved" if decision == "approve" else "rejected"
        if not set_po_approval(po["po_id"], approval, admin.username, at):
            logger.warning("Draft PO for ticket %s not found while recording decision", ticket_id)
    detail = (f"Draft PO {po['po_id']} approved by {admin.username}. PO remains a draft; nothing was "
              "purchased or issued." if decision == "approve" and po
              else f"Ticket rejected by {admin.username}.")
    _audit_event(ticket_id, "Admin", decision, detail)
    if note:
        push_note(ticket_id, _note(admin, note))
    return _load(ticket_id)


def _note(author: UserPublic, text: str) -> dict:
    return TicketNote(author=author.username, role=author.role, text=text, created_at=_now()).model_dump()


def add_note(ticket_id: str, author: UserPublic, text: str, *, require_owner: bool = False) -> dict:
    _require_persistence()
    text = (text or "").strip()
    if not text:
        raise TicketError(422, "Note text is required.")
    if require_owner:
        _load_owned(ticket_id, author)
    else:
        _load(ticket_id)
    push_note(ticket_id, _note(author, text))
    return _load(ticket_id)


def user_update_ticket(ticket_id: str, user: UserPublic, sub: TicketSubmission) -> dict:
    _require_persistence()
    doc = _load_owned(ticket_id, user)
    if doc.get("status") not in USER_EDITABLE_STATUSES:
        raise TicketError(409, f"Ticket cannot be edited while status is '{doc.get('status')}'.")
    if not update_submission(ticket_id, sub.model_dump(mode="json"), build_request_text(sub),
                             expected=USER_EDITABLE_STATUSES):
        raise TicketError(409, "Ticket status changed; it can no longer be edited.")
    _audit_event(ticket_id, "User", "resubmit_ticket", f"Ticket updated and resubmitted by {user.username}.")
    return _load(ticket_id)


def get_user_ticket(ticket_id: str, user: UserPublic) -> dict:
    _require_persistence()
    return _load_owned(ticket_id, user)


def get_admin_ticket(ticket_id: str) -> dict:
    _require_persistence()
    return _load(ticket_id)


def list_user_tickets(user: UserPublic, limit: int = 50) -> list[dict]:
    _require_persistence()
    return list_tickets_for_owner(user.username, limit)


def list_admin_tickets(status: Optional[str] = None, limit: int = 100) -> tuple[list[dict], dict[str, int]]:
    _require_persistence()
    return list_tickets(status, limit), count_tickets_by_status()


# ---------- views ----------
def _submission(doc: dict) -> Optional[TicketSubmission]:
    try:
        return TicketSubmission.model_validate(doc["submission"]) if doc.get("submission") else None
    except Exception:  # noqa: BLE001 - legacy/foreign documents
        return None


def _notes(doc: dict) -> list[TicketNote]:
    out = []
    for n in doc.get("notes") or []:
        try:
            out.append(TicketNote.model_validate(n))
        except Exception:  # noqa: BLE001
            continue
    return out


def _decision(doc: dict) -> Optional[Decision]:
    try:
        return Decision.model_validate(doc["decision"]) if doc.get("decision") else None
    except Exception:  # noqa: BLE001
        return None


def _po(doc: dict) -> Optional[dict]:
    return ((doc.get("workflow_result") or {}).get("procurement") or {}).get("po")


def _po_summary(doc: dict) -> Optional[dict]:
    po = _po(doc)
    if not po or doc.get("status") not in ("reviewed", "approved", "rejected"):
        return None
    dec = (doc.get("decision") or {}).get("decision")
    return {
        "po_id": po.get("po_id"),
        "vendor_name": po.get("vendor_name"),
        "total": po.get("total"),
        "currency": po.get("currency", "INR"),
        "status": po.get("status", "draft"),
        "approval_status": {"approve": "approved", "reject": "rejected"}.get(dec, "pending"),
    }


def _outcome(doc: dict) -> str:
    status = doc.get("status")
    if status == "submitted":
        return "Waiting for an admin to review your request."
    if status == "processing":
        return "Being processed."
    if status == "reviewed":
        return "Under admin review."
    if status == "needs_info":
        return "More information needed — please update your request."
    if status == "approved":
        po = _po(doc) or {}
        total = po.get("total")
        amount = f" for ₹{_inr(float(total))}" if isinstance(total, (int, float)) else ""
        return (f"Approved — draft PO {po.get('po_id', '')}{amount} recorded. "
                "No purchase has been made yet.")
    if status == "rejected":
        note = ((doc.get("decision") or {}).get("note") or "").strip()
        return "Rejected by admin." + (f" Note: {note}" if note else "")
    if status in ("failed", "persistence_error"):
        return "Processing failed; an admin will retry."
    return "Status unavailable."


def to_user_view(doc: dict) -> UserTicketView:
    sub = _submission(doc)
    questions = []
    if doc.get("status") == "needs_info":
        questions = list((doc.get("workflow_result") or {}).get("clarification_questions") or [])
    return UserTicketView(
        ticket_id=doc["ticket_id"],
        status=doc.get("status") or "unknown",
        submission=sub,
        outcome=_outcome(doc),
        clarification_questions=questions,
        po_summary=_po_summary(doc),
        notes=_notes(doc),
        decision=_decision(doc),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


def to_admin_view(doc: dict) -> AdminTicketView:
    return AdminTicketView(
        ticket_id=doc["ticket_id"],
        status=doc.get("status") or "unknown",
        owner_username=doc.get("owner_username"),
        owner_name=doc.get("owner_name"),
        owner_email=doc.get("owner_email"),
        submission=_submission(doc),
        raw_text=doc.get("raw_text"),
        workflow_result=doc.get("workflow_result"),
        notes=_notes(doc),
        decision=_decision(doc),
        run_count=int(doc.get("run_count") or 0),
        last_run_by=doc.get("last_run_by"),
        error=doc.get("error"),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


def to_list_item(doc: dict, admin: bool) -> TicketListItem:
    sub = doc.get("submission") or {}
    return TicketListItem(
        ticket_id=doc["ticket_id"],
        title=sub.get("title"),
        event_category=sub.get("event_category"),
        event_date=str(sub["event_date"]) if sub.get("event_date") else None,
        owner_username=doc.get("owner_username") if admin else None,
        owner_name=doc.get("owner_name") if admin else None,
        status=doc.get("status") or "unknown",
        final_status=(doc.get("workflow_result") or {}).get("final_status"),
        run_count=int(doc.get("run_count") or 0),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )
