"""Thin LLM wrapper. Uses OpenAI-compatible API if a key is configured, otherwise returns None so
callers fall back to deterministic logic. The LLM is only used for language tasks (extraction,
explanations) - never for arithmetic or pass/fail decisions."""
from __future__ import annotations

import json
import logging
import os
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


def chat_json(system: str, user: str) -> Optional[dict]:
    """Return a parsed JSON object from the model, or None if unavailable/failed."""
    if not llm_enabled():
        return None
    try:
        resp = _client().chat.completions.create(
            model=model_name(),
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return json.loads(resp.choices[0].message.content or "{}")
    except Exception as exc:  # network, auth, parse errors -> deterministic fallback
        log.warning("LLM JSON call failed, using fallback: %s", exc)
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
        return (resp.choices[0].message.content or "").strip() or None
    except Exception as exc:
        log.warning("LLM text call failed, using fallback: %s", exc)
        return None
