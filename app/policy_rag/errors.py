"""Typed, client-safe errors for the policy RAG layer."""

from __future__ import annotations


class PolicyRagError(Exception):
    """Error with an HTTP status and a safe (no document text / paths) detail."""

    def __init__(self, status_code: int = 400, detail: str = "Policy request failed.") -> None:
        super().__init__(detail)
        self.status_code = int(status_code)
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PolicyRagError(status_code={self.status_code!r}, detail={self.detail!r})"
