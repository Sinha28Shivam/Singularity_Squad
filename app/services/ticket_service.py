"""Ticket service: runs the multi-agent workflow and persists ticket, draft PO and audit trail.

Persistence is not atomic across collections. If a write fails after the ticket was created,
the ticket is left with status ``persistence_error`` (or ``failed`` if the workflow itself raised)
and the caller receives a PersistenceError (HTTP 503) instead of a success response.
"""
from __future__ import annotations

import logging
import secrets
from datetime import date, datetime, timezone
from typing import Optional

from app.db import PersistenceError, ensure_indexes, get_client, persistence_mode
from app.orchestrator import run_workflow
from app.repositories import (
    append_audit_entries,
    create_ticket,
    mark_ticket_status,
    update_ticket_result,
    upsert_draft_po,
)
from app.schemas import WorkflowResult

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
        updated_at = _now()
        update_ticket_result(ticket_id, result.model_dump(mode="json"), result.final_status, updated_at)
        if result.procurement and result.procurement.po:
            upsert_draft_po(ticket_id, result.procurement.po, updated_at)
        append_audit_entries(ticket_id, result.audit_trail)
    except Exception as exc:
        logger.error("Persistence failed for ticket %s (%s)", ticket_id, type(exc).__name__)
        _mark_best_effort(ticket_id, "persistence_error", f"Persistence error: {type(exc).__name__}")
        if isinstance(exc, PersistenceError):
            raise
        raise PersistenceError(_PERSIST_FAILED_MSG) from exc

    result.persisted = True
    return result
