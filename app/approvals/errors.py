"""Approval-domain errors. ``detail`` is always safe to return to API clients."""
from __future__ import annotations


class ApprovalError(Exception):
    """Maps to an HTTP error: 403 role/self-approval, 404 not visible, 409 conflict, 422 validation."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# Safe, stable client messages (shared with the API layer and tests).
MSG_NOT_FOUND = "Approval request not found."
MSG_TICKET_NOT_FOUND = "Ticket not found."
MSG_WRONG_ROLE = "Only an eligible approver can decide this request."
MSG_SELF_APPROVAL = "You cannot decide your own request."
MSG_COMMENT_REQUIRED = "A comment is required to reject or request changes."
MSG_EXPIRED = "Approval request has expired."
MSG_TERMINAL = "This approval has already been decided or the request has changed."
MSG_STALE = "The request has changed; reload and review the latest version."
MSG_IDEMPOTENCY_CONFLICT = "Idempotency key was already used with a different request."
MSG_BLOCKED = "This request is blocked by policy and cannot be approved."
