"""Page-aware chunking. Chunks never cross page boundaries; each carries its section heading.

Token estimate: words * 1.3 (no tokenizer dependency). Defaults come from data/policy_rag_config.json.
"""
from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Iterable, Optional

from app.policy_rag.schemas import PolicyChunk

DEFAULT_TARGET_TOKENS = 450
DEFAULT_OVERLAP_TOKENS = 60
TOKENS_PER_WORD = 1.3

_NUMBERED_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+([A-Z][^\n]{1,90})$")
_SENTENCE_END = re.compile(r"[.;:,]$")


def estimate_tokens(text: str) -> int:
    words = len(text.split())
    return int(math.ceil(words * TOKENS_PER_WORD)) if words else 0


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


def _windows(lines: list[str], target_words: int, overlap_words: int) -> Iterable[str]:
    words: list[str] = []
    line_breaks: list[int] = []  # word offsets where a line ends (so chunks keep line structure)
    for line in lines:
        words.extend(line.split())
        line_breaks.append(len(words))
    if not words:
        return
    breaks = set(line_breaks)

    def render(a: int, b: int) -> str:
        out, cur = [], []
        for i in range(a, b):
            cur.append(words[i])
            if (i + 1) in breaks:
                out.append(" ".join(cur))
                cur = []
        if cur:
            out.append(" ".join(cur))
        return "\n".join(out)

    start = 0
    n = len(words)
    while start < n:
        end = min(n, start + target_words)
        if end < n:
            # Prefer ending at a line break in the last quarter of the window.
            candidates = [b for b in line_breaks if start + target_words * 3 // 4 <= b <= end]
            if candidates:
                end = candidates[-1]
        yield render(start, end)
        if end >= n:
            break
        start = max(end - overlap_words, start + 1)


def chunk_pages(pages: list, document: Any, *, target_tokens: int = DEFAULT_TARGET_TOKENS,
                overlap_tokens: int = DEFAULT_OVERLAP_TOKENS) -> list[PolicyChunk]:
    """Split extracted pages into ``PolicyChunk`` records (no embeddings)."""
    document_id = _field(document, "document_id")
    content_hash = _field(document, "content_hash")
    target_words = max(20, int(target_tokens / TOKENS_PER_WORD))
    overlap_words = max(0, min(int(overlap_tokens / TOKENS_PER_WORD), target_words // 2))
    chunks: list[PolicyChunk] = []
    carry: Optional[str] = None
    for page in pages:
        text = _page_attr(page, "text") or ""
        page_number = int(_page_attr(page, "page_number"))
        for section, lines in _split_sections(text, carry):
            carry = section
            for piece in _windows(lines, target_words, overlap_words):
                if not piece.strip():
                    continue
                idx = len(chunks)
                chunks.append(PolicyChunk(
                    chunk_id=chunk_id_for(document_id, content_hash, idx), document_id=document_id,
                    content_hash=content_hash, page_number=page_number, section=section, chunk_index=idx,
                    text=piece, token_estimate=estimate_tokens(piece)))
    return chunks
