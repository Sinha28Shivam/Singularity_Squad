"""Request correlation IDs: honour a safe inbound X-Request-ID (or mint one), expose it via a contextvar and
echo it on every HTTP response."""
from __future__ import annotations

import re
import uuid
from contextvars import ContextVar
from typing import Optional

REQUEST_ID_HEADER = "X-Request-ID"
_HEADER_KEY = REQUEST_ID_HEADER.lower().encode("latin-1")
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")

request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def get_request_id() -> Optional[str]:
    """The current request's correlation ID, or None outside a request."""
    return request_id_var.get()


def normalize_request_id(value: Optional[str]) -> str:
    """Return value if it is 1-64 chars of [A-Za-z0-9._-]; otherwise a fresh uuid4 hex."""
    if value and _VALID_REQUEST_ID.fullmatch(value):
        return value
    return uuid.uuid4().hex


class RequestIDMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware) so streaming responses and background tasks are unaffected."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = next((v for k, v in scope.get("headers", []) if k.lower() == _HEADER_KEY), None)
        try:
            inbound = raw.decode("ascii") if raw is not None else None
        except UnicodeDecodeError:
            inbound = None
        request_id = normalize_request_id(inbound)
        token = request_id_var.set(request_id)

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != _HEADER_KEY]
                headers.append((_HEADER_KEY, request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)
