"""Service-level errors mapped to HTTP responses by app.main. Details must be safe to show to clients."""
from __future__ import annotations


class TicketError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
