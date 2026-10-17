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

from app.approvals import service as approval_service
from app.approvals.policy import POLICY_BINDING_KEY, evaluate_approval_requirement
from app.approvals.schemas import APPROVAL_SAFETY_NOTICE
from app.db import PersistenceError, ensure_indexes, get_client, persistence_mode
from app.orchestrator import run_workflow
from app.policy_rag.evaluation import binding as policy_evaluation_binding
from app.policy_rag.evaluation import evaluate_and_snapshot, policy_rag_enabled
from app.policy_rag.evaluation import redact as redact_policy_evaluation
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
    set_invoice_match,
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


_POLICY_UNAVAILABLE_MSG = ("Approval policy could not be evaluated; no approval request was created. "
                           "Please re-run the workflow later.")
ADMIN_APPROVE_FORBIDDEN = "Approvals must be decided by an eligible approver via /api/approvals."
# A pending approval may be re-run by an admin: the old request is superseded and a new version created.
_RUN_CLAIMABLE = set(RUNNABLE_STATUSES) | {"pending_approval"}


def _fail_run(ticket_id: str, exc: Exception) -> None:
    logger.error("Persistence failed for ticket %s (%s)", ticket_id, type(exc).__name__)
    _mark_best_effort(ticket_id, "persistence_error", f"Persistence error: {type(exc).__name__}")
    if isinstance(exc, PersistenceError):
        raise exc
    raise PersistenceError(_PERSIST_FAILED_MSG) from exc


def run_ticket(ticket_id: str, admin: UserPublic, invoice_id: Optional[str] = None,
               today: Optional[date] = None) -> dict:
    """Run the workflow for a portal ticket and map the outcome to the ticket/approval lifecycle.

    The snapshot is committed with status ``processing``; only after the approval request (if required)
    exists does the ticket move to ``pending_approval`` / ``needs_info`` / ``reviewed``. Policy evaluation
    fails closed: no approval request is created and the caller gets 503.
    """
    _require_persistence()
    doc = _load(ticket_id)
    previous = doc.get("status")
    if previous not in _RUN_CLAIMABLE:
        raise TicketError(409, f"Ticket cannot be run while status is '{previous}'.")
    if not set_status(ticket_id, "processing", expected=_RUN_CLAIMABLE):
        raise TicketError(409, "Ticket is already being processed or is no longer runnable.")
    if previous == "pending_approval":
        try:  # the old request must not stay approvable while its content is being re-evaluated
            approval_service.supersede_pending(ticket_id, "rerun")
        except Exception as exc:  # noqa: BLE001
            _fail_run(ticket_id, exc)

    try:
        result = run_workflow(doc.get("raw_text") or "", today, invoice_id, request_id=ticket_id)
    except Exception as exc:
        logger.error("Workflow failed for ticket %s (%s)", ticket_id, type(exc).__name__)
        _mark_best_effort(ticket_id, "failed", f"Workflow error: {type(exc).__name__}")
        raise TicketError(500, "Workflow failed; the ticket was marked as failed.") from None

    result.ticket_id = ticket_id
    requirement = None
    try:
        requirement = evaluate_approval_requirement(result.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001 - fail closed below
        logger.error("Approval policy evaluation failed for ticket %s (%s)", ticket_id, type(exc).__name__)

    try:
        _shift_audit(result, get_last_sequence(ticket_id))
        persist_result(ticket_id, result, commit=lambda snap, at: save_run_result(
            ticket_id, snap, "processing", admin.username, at))
    except Exception as exc:  # noqa: BLE001
        result.persisted = False
        _fail_run(ticket_id, exc)

    policy_binding = None
    if requirement is not None and policy_rag_enabled():
        # Policy RAG runs on the persisted result (run_count is now final) and before the approval
        # requirement is fixed. It can only add reasons / raise risk / hard-block; any failure fails closed.
        try:
            evaluation = evaluate_and_snapshot(_load(ticket_id), result.model_dump(mode="json"))
            evaluation_json = evaluation.model_dump(mode="json")
            requirement = evaluate_approval_requirement(result.model_dump(mode="json"),
                                                        policy_evaluation=evaluation_json)
            policy_binding = policy_evaluation_binding(evaluation_json)
        except Exception as exc:  # noqa: BLE001 - fail closed below
            logger.error("Policy RAG evaluation failed for ticket %s (%s)", ticket_id, type(exc).__name__)
            requirement = None

    if requirement is None:
        try:
            approval_service.supersede_pending(ticket_id, "policy_unavailable")
            set_status(ticket_id, "reviewed", expected={"processing"}, error="Approval policy unavailable.")
        except Exception as exc:  # noqa: BLE001
            _fail_run(ticket_id, exc)
        raise TicketError(503, _POLICY_UNAVAILABLE_MSG)

    if requirement.needs_clarification or result.final_status == "needs_clarification":
        new_status = "needs_info"
    elif requirement.required and not requirement.blocked:
        new_status = "pending_approval"
    else:
        new_status = "reviewed"
    try:
        ticket_doc = _load(ticket_id)
        if policy_binding is not None:
            ticket_doc[POLICY_BINDING_KEY] = policy_binding
        approval = approval_service.on_workflow_completed(ticket_doc, result.model_dump(mode="json"),
                                                          requirement)
        if new_status == "pending_approval" and not approval:
            raise PersistenceError("Approval request could not be created.")
        if not set_status(ticket_id, new_status, expected={"processing", new_status}):
            raise PersistenceError("Ticket status changed while the run was being finalised.")
    except Exception as exc:  # noqa: BLE001
        try:  # never leave an approvable request behind a ticket that is not pending_approval
            approval_service.supersede_pending(ticket_id, "run_finalisation_failed")
        except Exception:  # noqa: BLE001 - best effort; a re-run supersedes it as well
            pass
        _fail_run(ticket_id, exc)
    return _load(ticket_id)


def decide_ticket(ticket_id: str, admin: UserPublic, decision: str, note: Optional[str] = None) -> dict:
    """Legacy admin decision. Admins may only close (reject) a ``reviewed`` ticket — one that needed no
    approval or is hard-blocked. Approving is reserved for eligible approvers via /api/approvals."""
    _require_persistence()
    if decision not in ("approve", "reject"):
        raise TicketError(422, "Decision must be 'approve' or 'reject'.")
    if decision == "approve":
        raise TicketError(403, ADMIN_APPROVE_FORBIDDEN)
    doc = _load(ticket_id)
    if doc.get("status") == "pending_approval":
        raise TicketError(409, "Ticket has a pending approval request; it must be decided via /api/approvals.")
    if doc.get("status") != "reviewed":
        raise TicketError(409, f"Ticket cannot be decided while status is '{doc.get('status')}'.")
    wr = doc.get("workflow_result") or {}
    po = (wr.get("procurement") or {}).get("po")

    at = _now()
    note = (note or "").strip() or None
    record = Decision(decision=decision, by=admin.username, at=at, note=note).model_dump()
    if not set_decision(ticket_id, record, "rejected", expected={"reviewed"}):
        raise TicketError(409, "Ticket status changed; please reload and try again.")
    if po and not set_po_approval(po["po_id"], "rejected", admin.username, at):
        logger.warning("Draft PO for ticket %s not found while recording decision", ticket_id)
    _audit_event(ticket_id, "Admin", decision, f"Ticket rejected by {admin.username}.")
    if note:
        push_note(ticket_id, _note(admin, note))
    return _load(ticket_id)


def match_invoice_after_approval(ticket_id: str, admin: UserPublic, invoice_id: str) -> dict:
    """Reconcile a (synthetic) invoice that arrived after approval against the ticket's approved draft PO.

    The workflow result, PO and approval are not changed (re-running would supersede the approval); the
    result is stored separately as ``invoice_match`` and bound to the approved PO and run. Nothing is paid.
    Re-matching is refused once a simulated payment exists for the ticket.
    """
    from app.agents import billing, policy
    from app.schemas import DraftPO

    _require_persistence()
    doc = _load(ticket_id)
    if doc.get("status") != "approved":
        raise TicketError(409, "An invoice can be matched after approval only on an approved ticket.")
    wr = doc.get("workflow_result") or {}
    if isinstance(wr.get("reconciliation"), dict):
        raise TicketError(409, "This ticket's invoice was already reconciled during the workflow run.")
    po = (wr.get("procurement") or {}).get("po")
    if not isinstance(po, dict):
        raise TicketError(409, "This ticket has no draft PO to match an invoice against.")
    invoice = billing.get_invoice(invoice_id)
    if invoice is None:
        raise TicketError(404, f"Invoice {invoice_id} not found.")
    from app.repositories.payment_repository import list_simulations
    if list_simulations(ticket_id, limit=1):
        raise TicketError(409, "A simulated payment already exists for this ticket; the invoice can't change.")

    draft = DraftPO(**po)
    inv, delivery, _ = billing.bind_demo_documents(invoice, billing.get_delivery(invoice.delivery_id), draft)
    rec = billing.reconcile(inv, draft, delivery, billing.load_history(), policy.load_rules())
    at = _now()
    match = rec.model_dump(mode="json")
    match.update({"po_id": draft.po_id, "po_total": draft.total, "currency": draft.currency,
                  "run_count": int(doc.get("run_count") or 0), "matched_by": admin.username, "matched_at": at,
                  "post_approval": True})
    if not set_invoice_match(ticket_id, match, expected={"approved"}):
        raise TicketError(409, "Ticket status changed; please reload and try again.")
    _audit_event(ticket_id, "Billing Guardian", "post_approval_invoice_match",
                 f"{rec.invoice_id} matched after approval against approved {draft.po_id}: {rec.status}; "
                 f"expected INR {rec.expected_total:,.2f}, invoiced INR {rec.invoiced_total:,.2f}. "
                 f"{rec.recommendation} No payment was made.", status="pass" if rec.status == "match" else "fail")
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
    if doc.get("status") in ("changes_requested", "approval_expired"):
        try:  # defensive: an edited request must never be authorised by an older approval version
            approval_service.supersede_pending(ticket_id, "resubmitted")
        except Exception as exc:  # noqa: BLE001 - the next admin run supersedes it as well
            logger.warning("Could not supersede approval for ticket %s (%s)", ticket_id, type(exc).__name__)
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


_PO_SUMMARY_STATUSES = {"reviewed", "pending_approval", "changes_requested", "approval_expired",
                        "approved", "rejected"}


def _po_summary(doc: dict) -> Optional[dict]:
    po = _po(doc)
    if not po or doc.get("status") not in _PO_SUMMARY_STATUSES:
        return None
    return {
        "po_id": po.get("po_id"),
        "vendor_name": po.get("vendor_name"),
        "total": po.get("total"),
        "currency": po.get("currency", "INR"),
        "status": po.get("status", "draft"),
        "approval_status": {"approved": "approved", "rejected": "rejected"}.get(doc.get("status"), "pending"),
    }


def _last_decision_comment(approval: Optional[dict]) -> Optional[str]:
    current = (approval or {}).get("current") or {}
    decisions = current.get("decisions") or []
    return (decisions[-1].get("comment") or None) if decisions else None


def _outcome(doc: dict, approval: Optional[dict] = None) -> str:
    status = doc.get("status")
    if status == "submitted":
        return "Waiting for an admin to review your request."
    if status == "processing":
        return "Being processed."
    if status == "reviewed":
        return "Under admin review."
    if status == "needs_info":
        return "More information needed — please update your request."
    if status == "pending_approval":
        return "Waiting for approval by an approver."
    if status == "changes_requested":
        return "Changes requested — please update your request."
    if status == "approval_expired":
        return "Approval request expired — an admin will re-run it."
    if status == "approved":
        po = _po(doc) or {}
        total = po.get("total")
        amount = f" for ₹{_inr(float(total))}" if isinstance(total, (int, float)) else ""
        return (f"Approved — draft PO {po.get('po_id', '')}{amount} recorded. "
                "No purchase has been made yet.")
    if status == "rejected":
        if doc.get("decision"):
            note = ((doc.get("decision") or {}).get("note") or "").strip()
            return "Rejected by admin." + (f" Note: {note}" if note else "")
        comment = _last_decision_comment(approval)
        return "Rejected by an approver." + (f" Note: {comment}" if comment else "")
    if status in ("failed", "persistence_error"):
        return "Processing failed; an admin will retry."
    return "Status unavailable."


# ---------- approval info embedded in ticket views ----------
_REQUESTER_APPROVAL_FIELDS = ("approval_id", "request_version", "status", "reason_codes", "reasons", "risk_level",
                              "amount", "currency", "required_role", "requester_id", "created_at", "expires_at",
                              "decided_at")


def _ticket_approval(doc: dict, viewer: UserPublic) -> Optional[dict]:
    """TicketApprovalView for this ticket as a JSON dict, or None if the ticket never had a request."""
    try:
        view = approval_service.ticket_approval_view(doc["ticket_id"], viewer, ticket=doc)
    except PersistenceError:
        raise
    except Exception as exc:  # noqa: BLE001 - ApprovalError (404/none) or legacy data: no approval panel
        if getattr(exc, "status_code", None) not in (404, None):
            logger.warning("Approval info unavailable for ticket %s (%s)", doc.get("ticket_id"),
                           type(exc).__name__)
        return None
    data = view.model_dump(mode="json") if hasattr(view, "model_dump") else dict(view)
    if not data.get("current") and not data.get("history"):
        return None
    return data


def requester_approval(data: Optional[dict]) -> Optional[dict]:
    """Requester-safe projection: status, reasons, amount, history with comments, safety notice.
    Excludes the review snapshot (vendor/line items), hashes and idempotency keys."""
    if not data:
        return None

    def project(a: dict) -> dict:
        out = {k: a.get(k) for k in _REQUESTER_APPROVAL_FIELDS}
        out["decisions"] = [{"action": d.get("action"), "actor_id": d.get("actor_id"),
                             "actor_role": d.get("actor_role"), "comment": d.get("comment"),
                             "decided_at": d.get("decided_at")} for d in a.get("decisions") or []]
        return out

    current = data.get("current")
    return {
        "current": project(current) if current else None,
        "history": [project(a) for a in data.get("history") or []],
        "safety_notice": APPROVAL_SAFETY_NOTICE,
    }


def _latest_policy_evaluation(doc: dict) -> Optional[dict]:
    """Latest stored PolicyEvaluationResult (JSON) for the ticket, or None. View-only: failures hide the panel."""
    if not doc.get("run_count"):
        return None
    try:
        from app.repositories import evidence_snapshot_repository
        snap = evidence_snapshot_repository.get_latest(doc["ticket_id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Policy evidence unavailable for ticket %s (%s)", doc.get("ticket_id"), type(exc).__name__)
        return None
    if not snap or not isinstance(snap.get("evaluation"), dict):
        return None
    return {**snap["evaluation"], "snapshot_id": snap.get("snapshot_id")}


def _policy_status(doc: dict) -> Optional[dict]:
    """Server-derived requirement flags for the ticket's current run, so a ticket that needs clarification
    AND is hard-blocked shows both. View-only: failures hide the panel (never shown as 'approvable')."""
    wr = doc.get("workflow_result")
    if not isinstance(wr, dict) or not wr.get("final_status"):
        return None
    try:
        evaluation = approval_service._policy_evaluation_for(doc)
        req = evaluate_approval_requirement(wr, policy_evaluation=evaluation)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Policy status unavailable for ticket %s (%s)", doc.get("ticket_id"), type(exc).__name__)
        return None
    engine = (evaluation or {}).get("rule_engine") if isinstance(evaluation, dict) else None
    return {"needs_clarification": req.needs_clarification, "blocked": req.blocked, "required": req.required,
            "reason_codes": list(req.reason_codes), "reasons": list(req.reasons), "risk_level": req.risk_level,
            "policy_set_version": engine.get("policy_set_version") if isinstance(engine, dict) else None,
            "rule_engine_overall": engine.get("overall") if isinstance(engine, dict) else None}


def _requester_policy_status(status: Optional[dict]) -> Optional[dict]:
    if not status:
        return None
    return {k: status.get(k) for k in ("needs_clarification", "blocked", "reasons")}


def _owner_viewer(doc: dict) -> UserPublic:
    return UserPublic(username=doc.get("owner_username") or "", name=doc.get("owner_name") or "",
                      email=doc.get("owner_email") or "", role="user")


def to_user_view(doc: dict) -> UserTicketView:
    sub = _submission(doc)
    questions = []
    if doc.get("status") == "needs_info":
        questions = list((doc.get("workflow_result") or {}).get("clarification_questions") or [])
    approval = _ticket_approval(doc, _owner_viewer(doc)) if doc.get("owner_username") else None
    return UserTicketView(
        ticket_id=doc["ticket_id"],
        status=doc.get("status") or "unknown",
        submission=sub,
        outcome=_outcome(doc, approval),
        clarification_questions=questions,
        po_summary=_po_summary(doc),
        notes=_notes(doc),
        decision=_decision(doc),
        approval=requester_approval(approval),
        policy_evidence=(redact_policy_evaluation(ev, doc["ticket_id"])
                         if (ev := _latest_policy_evaluation(doc)) else None),
        policy_status=_requester_policy_status(_policy_status(doc)),
        created_at=doc.get("created_at"),
        updated_at=doc.get("updated_at"),
    )


_SYSTEM_ADMIN_VIEWER = UserPublic(username="", name="Admin", email="", role="admin")


def to_admin_view(doc: dict, viewer: Optional[UserPublic] = None) -> AdminTicketView:
    return AdminTicketView(
        approval=_ticket_approval(doc, viewer or _SYSTEM_ADMIN_VIEWER),
        policy_evaluation=_latest_policy_evaluation(doc),
        policy_status=_policy_status(doc),
        invoice_match=doc.get("invoice_match"),
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
