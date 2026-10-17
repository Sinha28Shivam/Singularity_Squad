"""Controlled on-disk storage for uploaded policy PDFs.

Files live under ``POLICY_STORE_DIR`` (default ``data/policy_store``) as ``<document_id>.pdf``. Storage
names are server-generated; paths are never returned to API clients.
"""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from app.data_loader import DATA_DIR

DOCUMENT_ID_PATTERN = r"^doc_[a-f0-9]{8,32}$"
_DOC_ID_RE = re.compile(DOCUMENT_ID_PATTERN)
_STORAGE_RE = re.compile(r"^doc_[a-f0-9]{8,32}\.pdf$")
_MAX_FILENAME = 120


def store_dir() -> Path:
    """Storage directory (created on demand). Read from env at call time so tests can redirect it."""
    raw = os.getenv("POLICY_STORE_DIR", "").strip()
    path = Path(raw) if raw else DATA_DIR / "policy_store"
    path.mkdir(parents=True, exist_ok=True)
    return path


def storage_name_for(document_id: str) -> str:
    if not _DOC_ID_RE.match(document_id or ""):
        raise ValueError("invalid document_id")
    return f"{document_id}.pdf"


def _path(storage_name: str) -> Path:
    if not _STORAGE_RE.match(storage_name or ""):
        raise ValueError("invalid storage name")
    return store_dir() / storage_name


def save_pdf(document_id: str, data: bytes) -> str:
    """Write bytes atomically; returns the storage name (not a path)."""
    name = storage_name_for(document_id)
    target = _path(name)
    tmp = target.with_suffix(".part")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return name


def load_pdf(storage_name: str) -> bytes | None:
    try:
        return _path(storage_name).read_bytes()
    except (OSError, ValueError):
        return None


def delete_pdf(storage_name: str) -> None:
    try:
        _path(storage_name).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def sanitize_filename(name: str | None) -> str:
    """Display-safe base filename: no directories, control chars or odd punctuation; keeps ``.pdf``."""
    raw = (name or "").replace("\\", "/").split("/")[-1]
    raw = unicodedata.normalize("NFKC", raw)
    raw = "".join(ch for ch in raw if ch.isprintable())
    raw = re.sub(r"[^A-Za-z0-9 ._()&+-]", "_", raw).strip(" .")
    raw = re.sub(r"\s+", " ", raw)
    raw = re.sub(r"_+", "_", raw)
    if not raw:
        raw = "document.pdf"
    if len(raw) > _MAX_FILENAME:
        stem, dot, ext = raw.rpartition(".")
        if dot and len(ext) <= 5:
            raw = stem[: _MAX_FILENAME - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            raw = raw[:_MAX_FILENAME]
    return raw


def slugify(value: str, max_len: int = 96) -> str:
    """Lowercase slug matching the logical_key pattern ``^[a-z0-9][a-z0-9_.-]*$``."""
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"\.pdf$", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-.")
    return (text[:max_len].rstrip("-.") or "document")
