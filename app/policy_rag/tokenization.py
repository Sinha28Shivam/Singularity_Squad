"""Model-aware token counting behind a small interface (AI plan B1).

``get_token_counter()`` returns a counter selected from validated configuration, never from request input:

- ``TiktokenCounter``: exact BPE counts for OpenAI-compatible models (``tiktoken``), ``exact = True``.
- ``EstimateCounter``: documented conservative estimate, ``exact = False``. It is never labelled exact.

Selection (``WORKPLACEOPS_TOKENIZER``): ``auto`` (default: tiktoken if importable and its encoding loads,
else the estimate), ``tiktoken`` (same as auto, but logs a warning on fallback) or ``estimate``.
"""
from __future__ import annotations

import logging
import math
import os
import re
import unicodedata
from functools import lru_cache
from typing import Optional, Protocol

logger = logging.getLogger(__name__)

ESTIMATE_TOKENIZER_ID = "estimate-v1"
DEFAULT_ENCODING = "o200k_base"
_ALLOWED_MODES = {"auto", "tiktoken", "estimate"}
_WORD = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_WS_RUN = re.compile(r"[ \t]{2,}|\n")

# Per-message framing overhead of chat requests (role markers, separators); conservative for OpenAI chat models.
MESSAGE_OVERHEAD_TOKENS = 4
REQUEST_OVERHEAD_TOKENS = 3


class TokenCounter(Protocol):
    tokenizer_id: str
    exact: bool

    def count(self, text: str, *, model: Optional[str] = None) -> int:
        """Return a model-compatible token count or a documented conservative estimate."""
        ...


class EstimateCounter:
    """Conservative estimate: max(ceil(chars / 3), ceil(1.25 * word/punctuation pieces) + whitespace runs,
    weighted non-ASCII chars (2 for symbols/emoji/replacement chars, else 1)).

    BPE tokenizers rarely produce fewer tokens than this (English, URLs, JSON, tables, OCR noise) and it
    over-counts scripts such as Devanagari, so budgets built on it err on the safe side."""

    tokenizer_id = ESTIMATE_TOKENIZER_ID
    exact = False

    def count(self, text: str, *, model: Optional[str] = None) -> int:
        if not text:
            return 0
        pieces = len(_WORD.findall(text))
        runs = len(_WS_RUN.findall(text))
        non_ascii = 0
        for ch in text:
            if ord(ch) > 127:
                cat = unicodedata.category(ch)
                if not cat.startswith("Z"):
                    non_ascii += 2 if (ord(ch) > 0xFFFF or cat.startswith("S") or ch == "\ufffd") else 1
        return max(int(math.ceil(len(text) / 3)), int(math.ceil(pieces * 1.25)) + runs, non_ascii)


class TiktokenCounter:
    exact = True

    def __init__(self, encoding) -> None:
        self._encoding = encoding
        self.tokenizer_id = f"tiktoken:{encoding.name}"

    def count(self, text: str, *, model: Optional[str] = None) -> int:
        if not text:
            return 0
        return len(self._encoding.encode(text, disallowed_special=()))


def _mode() -> str:
    mode = (os.getenv("WORKPLACEOPS_TOKENIZER") or "auto").strip().lower()
    if mode not in _ALLOWED_MODES:
        logger.warning("Unknown WORKPLACEOPS_TOKENIZER value; using the conservative estimate.")
        return "estimate"
    return mode


@lru_cache(maxsize=8)
def _tiktoken_counter(model: str) -> Optional[TiktokenCounter]:
    try:
        import tiktoken
        try:
            enc = tiktoken.encoding_for_model(model)
        except KeyError:
            enc = tiktoken.get_encoding(DEFAULT_ENCODING)
        return TiktokenCounter(enc)
    except Exception as exc:  # noqa: BLE001 - missing package / encoding download failure -> estimate
        logger.info("tiktoken unavailable for %s (%s); using conservative estimate.", model, type(exc).__name__)
        return None


def _default_model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini")


def get_token_counter(model: Optional[str] = None) -> TokenCounter:
    mode = _mode()
    if mode == "estimate":
        return EstimateCounter()
    counter = _tiktoken_counter(model or _default_model())
    if counter is None:
        if mode == "tiktoken":
            logger.warning("WORKPLACEOPS_TOKENIZER=tiktoken but tiktoken is unavailable; counts are estimates.")
        return EstimateCounter()
    return counter


def count_tokens(text: str, *, model: Optional[str] = None) -> int:
    return get_token_counter(model).count(text or "", model=model)


def count_chat_tokens(messages: list[dict], *, model: Optional[str] = None) -> int:
    """Tokens of a chat request's input: message contents plus per-message and request framing."""
    counter = get_token_counter(model)
    total = REQUEST_OVERHEAD_TOKENS
    for m in messages:
        total += MESSAGE_OVERHEAD_TOKENS + counter.count(str(m.get("content") or ""), model=model)
        total += counter.count(str(m.get("role") or ""), model=model)
    return total


def truncate_to_tokens(text: str, max_tokens: int, *, model: Optional[str] = None) -> str:
    """Longest prefix of ``text`` (cut at whitespace where possible) that fits ``max_tokens``."""
    if max_tokens <= 0 or not text:
        return ""
    counter = get_token_counter(model)
    if counter.count(text, model=model) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if counter.count(text[:mid], model=model) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    space = cut.rfind(" ")
    return cut[:space] if space > lo // 2 else cut
