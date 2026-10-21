"""Private on-disk store for vendor quotation PDFs (bytes never live in MongoDB).

* Root: ``VENDOR_QUOTE_STORE_DIR`` (default ``data/vendor_quote_store``, gitignored), read at call time.
* Keys are server-generated (``<rfq_id>/<quote_id>-<random>.pdf`` with strictly validated ids); clients never
  supply paths, and every resolved path is re-checked to stay inside the root, so traversal is impossible.
* Writes are atomic (temp file in the same directory + fsync + ``os.replace``).
* ``purge_older_than(days)`` is the retention helper (plan P2.17); MongoDB keeps only key, sha256 and size.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from app.data_loader import DATA_DIR

MAX_PDF_BYTES = 5 * 1024 * 1024
_ID_RE = re.compile(r"^[A-Z][A-Z0-9-]{2,63}$")
_KEY_RE = re.compile(r"^([A-Z][A-Z0-9-]{2,63})/([A-Z][A-Z0-9-]{2,63})-([a-f0-9]{16})\.pdf$")


class StorageError(Exception):
    """Invalid key/input or the private store could not be read/written."""


def store_dir() -> Path:
    raw = os.getenv("VENDOR_QUOTE_STORE_DIR", "").strip()
    path = Path(raw) if raw else DATA_DIR / "vendor_quote_store"
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def _validate_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise StorageError(f"invalid {label}")
    return value


def _path_for(key: str) -> Path:
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise StorageError("invalid storage key")
    root = store_dir()
    path = (root / key).resolve()
    if root not in path.parents:
        raise StorageError("invalid storage key")
    return path


def save_pdf(rfq_id: str, quote_id: str, data: bytes) -> dict:
    """Store bytes atomically and return ``{"storage_key", "sha256", "size"}``."""
    _validate_id(rfq_id, "rfq_id")
    _validate_id(quote_id, "quote_id")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise StorageError("PDF data must be non-empty bytes")
    if len(data) > MAX_PDF_BYTES:
        raise StorageError("PDF exceeds the storage size limit")
    key = f"{rfq_id}/{quote_id}-{uuid4().hex[:16]}.pdf"
    target = _path_for(key)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid4().hex[:8]}.part")
    try:
        with open(tmp, "xb") as fh:
            fh.write(bytes(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise StorageError("PDF could not be stored") from exc
    return {"storage_key": key, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def read_pdf(key: str) -> bytes:
    path = _path_for(key)
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise StorageError("stored PDF not found") from exc
    except OSError as exc:
        raise StorageError("stored PDF could not be read") from exc


def delete_pdf(key: str) -> bool:
    """Delete a stored PDF. Returns True if a file was removed."""
    path = _path_for(key)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StorageError("stored PDF could not be deleted") from exc
    try:
        path.parent.rmdir()  # only succeeds when the RFQ directory is empty
    except OSError:
        pass
    return True


def purge_older_than(days: float) -> int:
    """Retention: delete stored PDFs (and stale temp files) older than ``days``. Returns files removed."""
    if days < 0:
        raise StorageError("days must be >= 0")
    cutoff = time.time() - float(days) * 86400
    root = store_dir()
    removed = 0
    for child in root.iterdir():
        if not child.is_dir() or child.is_symlink() or not _ID_RE.match(child.name):
            continue
        for item in child.iterdir():
            if not item.is_file() or item.is_symlink():
                continue
            if not (_KEY_RE.match(f"{child.name}/{item.name}") or item.name.endswith(".part")):
                continue
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink()
                    removed += 1
            except OSError:
                continue
        try:
            child.rmdir()
        except OSError:
            pass
    return removed
