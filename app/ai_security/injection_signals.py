"""Prompt-injection detection SIGNALS (plan A2). A hit flags content for review; it is not the only defense.

Text is normalised before matching so trivial obfuscation does not evade the patterns: NFKC folding (full-width
and compatibility characters), removal of zero-width/format characters, common Cyrillic/Greek homoglyph folding
and whitespace collapsing.
"""
from __future__ import annotations

import re
import unicodedata

INJECTION_EVENT_CODE = "PROMPT_INJECTION_SIGNAL"

_HOMOGLYPHS = str.maketrans({
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O", "Р": "P", "С": "C", "Т": "T",
    "Х": "X", "ο": "o", "α": "a", "ε": "e", "ι": "i", "Ι": "I", "Ο": "O", "Α": "A", "Ε": "E",
})
_WS = re.compile(r"\s+")

SUSPICIOUS_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\bignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier|preceding)\b",
    r"\bdisregard\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)?\s*(instructions|rules|policy)",
    r"\bforget\s+(all\s+|any\s+)?(the\s+|your\s+)?(previous|prior|above|earlier)?\s*instructions\b",
    r"\bsystem\s+prompt\b",
    r"\bapprove\s+(this|everything|all|every|automatically)\b",
    r"\bauto[- ]?approve\b",
    r"\bmark\s+(this|it|everything|all)\s+(as\s+)?(approved|compliant|passed)\b",
    r"\byou\s+are\s+now\b",
    r"\b(developer|jailbreak|admin|god)\s+mode\b",
    r"\boverride\s+(the\s+)?(rules|policy|instructions|schema)\b",
    r"\breveal\s+(the\s+|your\s+)?(secret|api\s*key|password|token|credential|prompt|instruction)s?\b",
    r"\b(print|show|output|repeat)\s+(the\s+|your\s+)?(system\s+)?(prompt|instructions)\b",
    r"\bnew\s+instructions\s*:",
    r"</?\s*(system|assistant|instructions?)\s*>",
)]


def normalize_for_detection(text: str) -> str:
    s = unicodedata.normalize("NFKC", text or "")
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Cf")  # zero-width joiners, BOM, bidi marks
    s = s.translate(_HOMOGLYPHS)
    return _WS.sub(" ", s).strip()


def suspicious_hits(text: str) -> list[str]:
    """Distinct injection-pattern matches in ``text`` (normalised). Empty list = no signal."""
    norm = normalize_for_detection(text)
    hits: list[str] = []
    for p in SUSPICIOUS_PATTERNS:
        m = p.search(norm)
        if m and m.group(0) not in hits:
            hits.append(m.group(0))
    return hits


def joined_hits(texts: list[str]) -> list[str]:
    """Hits in each text and across adjacent pairs (catches an instruction split across two chunks)."""
    hits: list[str] = []
    for i, t in enumerate(texts):
        for h in suspicious_hits(t) + (suspicious_hits(f"{texts[i - 1][-200:]} {t[:200]}") if i else []):
            if h not in hits:
                hits.append(h)
    return hits
