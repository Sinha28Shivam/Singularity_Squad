"""Thin LLM wrapper. Uses OpenAI-compatible API if a key is configured, otherwise returns None so
callers fall back to deterministic logic. The LLM is only used for language tasks (extraction,
explanations) - never for arithmetic or pass/fail decisions."""
from __future__ import annotations

import json
import logging
import os
import threading
from functools import lru_cache
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


def _api_key() -> Optional[str]:
    return os.getenv("OPENAI_API_KEY") or os.getenv("API_KEY")


def llm_enabled() -> bool:
    return bool(_api_key()) and os.getenv("WORKPLACEOPS_OFFLINE", "0") != "1"


@lru_cache(maxsize=1)
def _client():
    from openai import OpenAI

    kwargs: dict[str, Any] = {"api_key": _api_key(), "timeout": 30.0, "max_retries": 1}
    if os.getenv("OPENAI_BASE_URL"):
        kwargs["base_url"] = os.getenv("OPENAI_BASE_URL")
    return OpenAI(**kwargs)


def model_name() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


_last_call = threading.local()


def _set_last_call(usage: Optional[dict], error_category: Optional[str]) -> None:
    _last_call.info = {"usage": usage, "error_category": error_category}


def pop_last_call() -> Optional[dict]:
    """Provider usage / safe error category of this thread's most recent call (then cleared).

    ``None`` when no real provider call happened (e.g. offline, or a test replaced ``chat_json``)."""
    info = getattr(_last_call, "info", None)
    _last_call.info = None
    return info


def classify_error(exc: BaseException) -> str:
    """Safe, operationally distinguishable error category (never the raw provider message)."""
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "provider_timeout"
    if "ratelimit" in name:
        return "provider_rate_limited"
    if "authentication" in name or "permissiondenied" in name:
        return "provider_auth_failed"
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "invalid_output"
    return "provider_error"


def _usage(resp: Any) -> Optional[dict]:
    u = getattr(resp, "usage", None)
    if u is None:
        return None
    return {"input_tokens": getattr(u, "prompt_tokens", None), "output_tokens": getattr(u, "completion_tokens", None)}


def chat_json(system: str, user: str, max_tokens: Optional[int] = None) -> Optional[dict]:
    """Return a parsed JSON object from the model, or None if unavailable/failed."""
    if not llm_enabled():
        return None
    try:
        kwargs: dict[str, Any] = {"max_tokens": max_tokens} if max_tokens else {}
        resp = _client().chat.completions.create(
            model=model_name(),
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **kwargs,
        )
        usage = _usage(resp)
        try:
            data = json.loads(resp.choices[0].message.content or "{}")
        except ValueError as exc:
            _set_last_call(usage, classify_error(exc))
            log.warning("LLM JSON output was not valid JSON; using fallback.")
            return None
        _set_last_call(usage, None)
        return data
    except Exception as exc:  # network, auth, parse errors -> deterministic fallback
        _set_last_call(None, classify_error(exc))
        log.warning("LLM JSON call failed (%s); using fallback.", classify_error(exc))
        return None


def chat_text(system: str, user: str, max_tokens: int = 300) -> Optional[str]:
    if not llm_enabled():
        return None
    try:
        resp = _client().chat.completions.create(
            model=model_name(),
            temperature=0.2,
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        _set_last_call(_usage(resp), None)
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as exc:
        _set_last_call(None, classify_error(exc))
        log.warning("LLM text call failed (%s); using fallback.", classify_error(exc))
        return None
