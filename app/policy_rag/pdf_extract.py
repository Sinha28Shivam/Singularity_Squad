"""Page-aware PDF text extraction with pypdf.

PDF content is untrusted data: nothing here executes scripts, follows links or interprets instructions.
Extracted text is never logged.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

from app.policy_rag.errors import PolicyRagError

PDF_MAGIC = b"%PDF-"
DEFAULT_LOW_TEXT_CHARS = 80
DEFAULT_MAX_PAGES = 500

ERROR_MESSAGES = {
    "not_pdf": "The file is not a PDF (extension and file signature must both be PDF).",
    "too_large": "The file exceeds the maximum upload size.",
    "too_many_pages": "The PDF has more pages than allowed.",
    "encrypted": "Encrypted or password-protected PDFs are not supported.",
    "corrupt": "The PDF could not be parsed; it may be damaged.",
    "empty_text": "No extractable text was found (scanned/image-only PDF). OCR or manual review is required.",
}

# pypdf logs parser warnings that can include fragments of the document; keep them out of app logs.
logging.getLogger("pypdf").setLevel(logging.ERROR)


class PdfError(PolicyRagError):
    """Typed, client-safe extraction/validation failure. ``code`` is a stable error category."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(422, message or ERROR_MESSAGES.get(code, "The PDF could not be processed."))
        self.code = code


@dataclass
class PageText:
    page_number: int          # 1-based
    text: str
    low_text: bool = False
    char_count: int = field(default=0)


def extraction_tool() -> dict:
    """Extraction tool and version recorded with each document (plan C5)."""
    try:
        import pypdf
        version = getattr(pypdf, "__version__", None)
    except ImportError:  # pragma: no cover - dependency is pinned
        version = None
    return {"tool": "pypdf", "tool_version": version, "ocr": None}


def extraction_quality(pages: list[PageText], *, low_text_chars: int = DEFAULT_LOW_TEXT_CHARS,
                       review_ratio: float = 0.30) -> dict:
    """Page-level quality metadata. Contains page numbers and counts only, never text."""
    low = [p.page_number for p in pages if p.low_text]
    empty = [p.page_number for p in pages if p.char_count == 0]
    total = len(pages)
    ratio = round(len(low) / total, 4) if total else 0.0
    warnings = []
    if empty:
        warnings.append("pages_without_text")  # likely scanned/image-only; OCR is not configured
    if low:
        warnings.append("low_text_pages")
    if total and ratio > review_ratio:
        warnings.append("low_text_ratio_exceeds_review_threshold")
    return {**extraction_tool(), "page_count": total, "low_text_chars_per_page": int(low_text_chars),
            "low_text_page_count": len(low), "low_text_ratio": ratio, "low_text_pages": low,
            "empty_text_pages": empty, "total_chars": sum(p.char_count for p in pages),
            "quality": "complete" if not low else ("failed" if total and len(empty) == total else "partial"),
            "needs_review": total == 0 or ratio > review_ratio,
            "warnings": warnings}


def has_pdf_signature(data: bytes) -> bool:
    # The spec allows leading junk before the header within the first 1024 bytes; accept a little slack.
    return bool(data) and PDF_MAGIC in data[:1024]


_SEGMENT_SPLIT = re.compile(r"(\s{2,})")


def _collapse_segment(segment: str) -> str:
    tokens = segment.split(" ")
    if len(tokens) == 3 and all(len(t) == 1 and t.isalpha() and t.isupper() for t in tokens):
        return "".join(tokens)  # e.g. "R A G"
    if len(tokens) < 4:
        return segment
    singles = sum(1 for t in tokens if len(t) == 1)
    if singles / len(tokens) < 0.75 or not any(t.isalpha() for t in tokens):
        return segment
    return "".join(tokens)


def normalize_letter_spacing(line: str) -> str:
    """Collapse letter-spaced runs ("S I N G U L A R I T Y" -> "SINGULARITY").

    Conservative: only segments (separated by 2+ spaces) of at least 4 tokens where >= 75% of tokens are
    single characters are collapsed; word gaps (double spaces) become single spaces.
    """
    if not line or line.count(" ") < 3:
        return line
    parts = _SEGMENT_SPLIT.split(line)
    out = []
    changed = False
    for part in parts:
        if part.strip() == "":
            out.append(part)
            continue
        collapsed = _collapse_segment(part)
        changed = changed or collapsed != part
        out.append(collapsed)
    if not changed:
        return line
    return re.sub(r"\s{2,}", " ", "".join(out)).strip()


def _clean_text(raw: str) -> str:
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    lines = []
    for line in text.split("\n"):
        line = "".join(ch for ch in line if ch == "\t" or ch.isprintable()).rstrip()
        lines.append(normalize_letter_spacing(line))
    # Drop runs of blank lines but keep single paragraph breaks.
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return cleaned.strip()


def extract_pages(data: bytes, *, max_pages: int = DEFAULT_MAX_PAGES,
                  low_text_chars: int = DEFAULT_LOW_TEXT_CHARS) -> list[PageText]:
    """Extract text per page. Raises ``PdfError`` with code not_pdf/encrypted/corrupt/too_many_pages/empty_text."""
    if not has_pdf_signature(data):
        raise PdfError("not_pdf")
    try:
        from pypdf import PdfReader
        from pypdf.errors import FileNotDecryptedError, PdfReadError
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise PdfError("corrupt", "PDF support is not available on the server.") from exc
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise PdfError("encrypted")
        page_count = len(reader.pages)
    except PdfError:
        raise
    except FileNotDecryptedError as exc:
        raise PdfError("encrypted") from exc
    except (PdfReadError, Exception) as exc:  # noqa: BLE001 - any parser failure is a safe "corrupt"
        raise PdfError("corrupt") from exc
    if page_count == 0:
        raise PdfError("corrupt")
    if page_count > max_pages:
        raise PdfError("too_many_pages")

    pages: list[PageText] = []
    for index in range(page_count):
        try:
            raw = reader.pages[index].extract_text() or ""
        except FileNotDecryptedError as exc:
            raise PdfError("encrypted") from exc
        except Exception:  # noqa: BLE001 - one unreadable page is treated as low-text, not fatal
            raw = ""
        text = _clean_text(raw)
        chars = len(re.sub(r"\s+", "", text))
        pages.append(PageText(page_number=index + 1, text=text, low_text=chars < low_text_chars, char_count=chars))
    if all(p.char_count == 0 for p in pages):
        err = PdfError("empty_text")
        err.pages = pages  # type: ignore[attr-defined] - lets ingestion keep page counts
        raise err
    return pages
