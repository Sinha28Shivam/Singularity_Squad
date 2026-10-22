"""Page-aware, token-bounded chunking. Chunks never cross page boundaries; each carries its section heading.

Sizes are measured with the configured ``TokenCounter`` (``app.policy_rag.tokenization``): exact tiktoken counts
when available, otherwise the documented conservative estimate (recorded as ``token_count_exact = False``).

- ``chunk_target_tokens``: preferred window size; windows prefer to end at a line break.
- ``chunk_overlap_tokens``: tokens carried from the end of one window into the next (capped at target / 2).
- ``chunk_max_tokens``: hard limit. No chunk exceeds it under the configured tokenizer: every rendered chunk is
  re-counted and shrunk if needed, and unbroken strings (URLs, JSON, OCR runs) longer than a fraction of the
  window are split into glued pieces so a single "word" can never produce an unbounded chunk.

Output is deterministic for a fixed input, configuration and tokenizer; ``chunking_config_version`` identifies
that combination so indexes built with another configuration can be detected and re-indexed.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable, Optional

from app.policy_rag.schemas import PolicyChunk
from app.policy_rag.tokenization import TokenCounter, get_token_counter

DEFAULT_TARGET_TOKENS = 450
DEFAULT_OVERLAP_TOKENS = 60
DEFAULT_MAX_TOKENS = 512
CHUNKING_ALGORITHM_VERSION = "token-window-v2"
TOKENS_PER_WORD = 1.3  # legacy constant (pre token-aware chunking); kept for importers only

_NUMBERED_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+([A-Z][^\n]{1,90})$")
_SENTENCE_END = re.compile(r"[.;:,]$")


def estimate_tokens(text: str, *, counter: Optional[TokenCounter] = None) -> int:
    """Token count of ``text`` under the configured counter (exact or conservative estimate)."""
    return (counter or get_token_counter()).count(text or "")


def chunking_settings(cfg: Optional[dict] = None) -> dict:
    """Validated (target, overlap, max) token settings from policy RAG config values."""
    cfg = cfg or {}
    max_tokens = max(16, int(cfg.get("chunk_max_tokens", DEFAULT_MAX_TOKENS) or DEFAULT_MAX_TOKENS))
    target = max(8, min(int(cfg.get("chunk_target_tokens", DEFAULT_TARGET_TOKENS) or DEFAULT_TARGET_TOKENS),
                        max_tokens))
    overlap = max(0, min(int(cfg.get("chunk_overlap_tokens", DEFAULT_OVERLAP_TOKENS) or 0), target // 2))
    return {"target_tokens": target, "overlap_tokens": overlap, "max_tokens": max_tokens}


def chunking_config_version(*, target_tokens: int, overlap_tokens: int, max_tokens: int, tokenizer_id: str) -> str:
    payload = json.dumps({"algorithm": CHUNKING_ALGORITHM_VERSION, "target": int(target_tokens),
                          "overlap": int(overlap_tokens), "max": int(max_tokens), "tokenizer": tokenizer_id},
                         sort_keys=True)
    return "chunk-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def current_chunking_config_version(cfg: Optional[dict] = None, counter: Optional[TokenCounter] = None) -> str:
    """Version of the chunking configuration a fresh (re)index would use right now."""
    if cfg is None:
        from app.data_loader import load_json
        cfg = load_json("policy_rag_config.json")
    s = chunking_settings(cfg)
    return chunking_config_version(tokenizer_id=(counter or get_token_counter()).tokenizer_id, **s)


# Backward-compatible module constant: version for the shipped default settings under the active tokenizer.
CHUNKING_CONFIG_VERSION = chunking_config_version(
    target_tokens=DEFAULT_TARGET_TOKENS, overlap_tokens=DEFAULT_OVERLAP_TOKENS, max_tokens=DEFAULT_MAX_TOKENS,
    tokenizer_id=get_token_counter().tokenizer_id)


def detect_heading(line: str) -> Optional[str]:
    """Return a normalized section heading for heading-like lines, else None.

    Accepts numbered headings ("9.1 Insight Eventz", "1. Purpose and commitment") and short uppercase lines
    ("CORPORATE EVENT MANAGEMENT"). Long numbered list items and sentences are not headings.
    """
    s = line.strip()
    if not s or len(s) > 100:
        return None
    m = _NUMBERED_HEADING.match(s)
    if m:
        title = m.group(2).strip()
        if len(title.split()) <= 10 and not _SENTENCE_END.search(title):
            return f"{m.group(1)} {title}"
        return None
    letters = [c for c in s if c.isalpha()]
    words = [w for w in re.split(r"\s+", s) if any(c.isalpha() for c in w)]
    if (len(letters) >= 6 and 2 <= len(words) <= 8 and all(c.isupper() for c in letters)
            and any(len(w) >= 3 for w in words)):
        return s
    return None


def _field(document: Any, name: str, default=None):
    if isinstance(document, dict):
        return document.get(name, default)
    return getattr(document, name, default)


def _page_attr(page: Any, name: str):
    return page.get(name) if isinstance(page, dict) else getattr(page, name)


def chunk_id_for(document_id: str, content_hash: str, chunk_index: int) -> str:
    digest = hashlib.sha256(f"{document_id}:{content_hash}:{chunk_index}".encode()).hexdigest()
    return f"chk_{digest[:20]}"


def _split_sections(text: str, carry: Optional[str]) -> list[tuple[Optional[str], list[str]]]:
    sections: list[tuple[Optional[str], list[str]]] = [(carry, [])]
    for line in text.split("\n"):
        if not line.strip():
            continue
        heading = detect_heading(line)
        if heading:
            if sections[-1][1]:
                sections.append((heading, [line.strip()]))
            else:
                sections[-1] = (heading, [line.strip()])
        else:
            sections[-1][1].append(line.strip())
    return [s for s in sections if s[1]]


def _split_long_word(word: str, limit: int, counter: TokenCounter) -> list[str]:
    """Split an unbroken string into pieces of at most ``limit`` tokens (never inside a combining sequence)."""
    pieces: list[str] = []
    rest = word
    while rest:
        if counter.count(rest) <= limit:
            pieces.append(rest)
            break
        lo, hi = 1, len(rest)
        while lo < hi:  # longest prefix that fits
            mid = (lo + hi + 1) // 2
            if counter.count(rest[:mid]) <= limit:
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        while cut > 1 and unicodedata.category(rest[cut]).startswith("M"):  # keep combining marks attached
            cut -= 1
        pieces.append(rest[:cut])
        rest = rest[cut:]
    return pieces


def _units(lines: list[str], long_limit: int, counter: TokenCounter) -> list[tuple[str, str]]:
    """(separator, text) units: words of each line; separator is "\\n" at line starts, " " between words and
    "" between pieces of a split unbroken string (so rendering reproduces the original string)."""
    units: list[tuple[str, str]] = []
    for line in lines:
        for wi, word in enumerate(line.split()):
            sep = "\n" if wi == 0 else " "
            pieces = [word] if counter.count(word) <= long_limit else _split_long_word(word, long_limit, counter)
            for pi, piece in enumerate(pieces):
                units.append((sep if pi == 0 else "", piece))
    return units


def _render(units: list[tuple[str, str]], a: int, b: int) -> str:
    return "".join((sep if i > a else "") + text for i, (sep, text) in enumerate(units[a:b], start=a))


def _windows(lines: list[str], target: int, overlap: int, max_tokens: int, counter: TokenCounter) -> Iterable[str]:
    long_limit = max(1, min(target, max_tokens) // 4)
    units = _units(lines, long_limit, counter)
    n = len(units)
    if not n:
        return
    costs = [counter.count((sep if i else "") + text) for i, (sep, text) in enumerate(units)]
    start = 0
    while start < n:
        end, total, line_ends = start, 0, []
        while end < n and (end == start or total + costs[end] <= target):
            total += costs[end]
            end += 1
            if end < n and units[end][0] == "\n" and total >= target * 3 // 4:
                line_ends.append(end)
        if end < n and line_ends:
            end = line_ends[-1]  # prefer ending at a line break in the last quarter of the window
        if counter.count(_render(units, start, end)) > max_tokens:
            lo, hi = start + 1, end
            while lo < hi:  # largest end whose rendered chunk fits the hard limit
                mid = (lo + hi + 1) // 2
                if counter.count(_render(units, start, mid)) <= max_tokens:
                    lo = mid
                else:
                    hi = mid - 1
            end = lo
        yield _render(units, start, end)
        if end >= n:
            break
        new_start, carried = end, 0
        while new_start - 1 > start and carried + costs[new_start - 1] <= overlap:
            new_start -= 1
            carried += costs[new_start]
        start = new_start


def chunk_pages(pages: list, document: Any, *, target_tokens: int = DEFAULT_TARGET_TOKENS,
                overlap_tokens: int = DEFAULT_OVERLAP_TOKENS, max_tokens: int = DEFAULT_MAX_TOKENS,
                counter: Optional[TokenCounter] = None) -> list[PolicyChunk]:
    """Split extracted pages into ``PolicyChunk`` records (no embeddings)."""
    counter = counter or get_token_counter()
    s = chunking_settings({"chunk_target_tokens": target_tokens, "chunk_overlap_tokens": overlap_tokens,
                           "chunk_max_tokens": max_tokens})
    version = chunking_config_version(tokenizer_id=counter.tokenizer_id, **s)
    document_id = _field(document, "document_id")
    content_hash = _field(document, "content_hash")
    chunks: list[PolicyChunk] = []
    carry: Optional[str] = None
    for page in pages:
        text = _page_attr(page, "text") or ""
        page_number = int(_page_attr(page, "page_number"))
        for section, lines in _split_sections(text, carry):
            carry = section
            for piece in _windows(lines, s["target_tokens"], s["overlap_tokens"], s["max_tokens"], counter):
                if not piece.strip():
                    continue
                idx = len(chunks)
                tokens = counter.count(piece)
                chunks.append(PolicyChunk(
                    chunk_id=chunk_id_for(document_id, content_hash, idx), document_id=document_id,
                    content_hash=content_hash, page_number=page_number, section=section, chunk_index=idx,
                    text=piece, token_estimate=tokens, token_count=tokens, tokenizer_id=counter.tokenizer_id,
                    token_count_exact=bool(counter.exact), chunking_config_version=version))
    return chunks
