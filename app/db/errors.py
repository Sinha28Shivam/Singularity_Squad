"""Persistence-layer exceptions. Messages must be safe to show to API clients (no URIs/credentials)."""


class PersistenceError(Exception):
    """Database unavailable or a read/write failed. Maps to HTTP 503."""

    def __init__(self, message: str = "Database unavailable. Please try again later.") -> None:
        super().__init__(message)


class AuditConflictError(PersistenceError):
    """An audit event with the same (ticket_id, sequence) exists with different content."""

    def __init__(self, ticket_id: str, sequence: int) -> None:
        super().__init__(f"Audit event {sequence} for ticket {ticket_id} already exists with different content.")
        self.ticket_id = ticket_id
        self.sequence = sequence
