"""Redaction for logs and telemetry: secrets never leave the process in operational records."""
from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),                                   # OpenAI-style API keys
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),                    # bearer tokens
    re.compile(r"(?i)\b(mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis)://[^\s\"']+"),  # connection URIs
    re.compile(r"(?i)\b(api[_-]?key|password|passwd|secret|token)\s*[:=]\s*[^\s,;\"']+"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),  # JWTs
]
_SENSITIVE_KEYS = {"api_key", "apikey", "authorization", "password", "secret", "token", "prompt", "system",
                   "user", "messages", "content", "raw_text", "text", "excerpt"}


def redact_text(text: str, max_len: int = 200) -> str:
    out = str(text or "")
    for p in _PATTERNS:
        out = p.sub(REDACTED, out)
    return out if len(out) <= max_len else out[: max_len - 1] + "…"


def redact_mapping(data: Any) -> Any:
    """Drop prompt/content-like keys and redact secret patterns in remaining strings (recursive)."""
    if isinstance(data, dict):
        return {k: (REDACTED if str(k).lower() in _SENSITIVE_KEYS else redact_mapping(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [redact_mapping(v) for v in data]
    if isinstance(data, str):
        return redact_text(data)
    return data
