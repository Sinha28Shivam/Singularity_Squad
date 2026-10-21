"""Private vendor-quote PDF store: atomic writes, hashes, key validation, size limit, retention."""
import hashlib
import os
import time

import pytest

from app.vendor_sourcing import storage
from app.vendor_sourcing.storage import StorageError

PDF = b"%PDF-1.4\n% synthetic test pdf\n%%EOF\n"


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setenv("VENDOR_QUOTE_STORE_DIR", str(tmp_path / "store"))
    return tmp_path / "store"


def test_save_read_delete_roundtrip(_store):
    meta = storage.save_pdf("RFQ-ABC123", "QUOTE-DEF456", PDF)
    assert set(meta) == {"storage_key", "sha256", "size"}
    assert meta["sha256"] == hashlib.sha256(PDF).hexdigest() and meta["size"] == len(PDF)
    assert meta["storage_key"].startswith("RFQ-ABC123/QUOTE-DEF456-") and meta["storage_key"].endswith(".pdf")
    assert storage.read_pdf(meta["storage_key"]) == PDF
    assert not list(_store.rglob("*.part"))  # atomic: no temp leftovers
    assert storage.delete_pdf(meta["storage_key"]) is True
    assert storage.delete_pdf(meta["storage_key"]) is False
    with pytest.raises(StorageError):
        storage.read_pdf(meta["storage_key"])


def test_keys_are_unique_per_save():
    a = storage.save_pdf("RFQ-ABC123", "QUOTE-DEF456", PDF)["storage_key"]
    b = storage.save_pdf("RFQ-ABC123", "QUOTE-DEF456", PDF)["storage_key"]
    assert a != b


@pytest.mark.parametrize("rfq_id,quote_id", [
    ("../etc", "QUOTE-1X"), ("RFQ-1/..", "QUOTE-1X"), ("RFQ-ABC", "..\\..\\x"), ("rfq-lower", "QUOTE-1X"),
    ("", "QUOTE-1X"), ("RFQ-ABC", "QUOTE-1X/../../evil"), ("C:", "QUOTE-1X"),
])
def test_save_rejects_unsafe_ids(rfq_id, quote_id):
    with pytest.raises(StorageError):
        storage.save_pdf(rfq_id, quote_id, PDF)


@pytest.mark.parametrize("key", [
    "../secret.pdf", "RFQ-ABC/../../x.pdf", "/etc/passwd", "C:\\Windows\\win.ini", "RFQ-ABC\\QUOTE-X-0123456789abcdef.pdf",
    "RFQ-ABC/QUOTE-X-0123456789abcdef.pdf/../..", "", None, "RFQ-ABC/QUOTE-X.pdf",
])
def test_read_and_delete_reject_untrusted_keys(key):
    with pytest.raises(StorageError):
        storage.read_pdf(key)
    with pytest.raises(StorageError):
        storage.delete_pdf(key)


def test_size_and_type_limits():
    with pytest.raises(StorageError):
        storage.save_pdf("RFQ-ABC123", "QUOTE-DEF456", b"")
    with pytest.raises(StorageError):
        storage.save_pdf("RFQ-ABC123", "QUOTE-DEF456", "text")  # type: ignore[arg-type]
    with pytest.raises(StorageError):
        storage.save_pdf("RFQ-ABC123", "QUOTE-DEF456", b"x" * (storage.MAX_PDF_BYTES + 1))
    assert storage.save_pdf("RFQ-ABC123", "QUOTE-DEF456", b"x" * storage.MAX_PDF_BYTES)["size"] == storage.MAX_PDF_BYTES


def test_failed_write_leaves_no_partial_file(monkeypatch, _store):
    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(storage.os, "replace", boom)
    with pytest.raises(StorageError):
        storage.save_pdf("RFQ-ABC123", "QUOTE-DEF456", PDF)
    assert [p for p in _store.rglob("*") if p.is_file()] == []


def test_purge_older_than(_store):
    old = storage.save_pdf("RFQ-OLD111", "QUOTE-OLD111", PDF)["storage_key"]
    new = storage.save_pdf("RFQ-NEW111", "QUOTE-NEW111", PDF)["storage_key"]
    stale = time.time() - 40 * 86400
    os.utime(_store / old, (stale, stale))
    outside = _store.parent / "outside.pdf"
    outside.write_bytes(PDF)
    os.utime(outside, (stale, stale))
    stray = _store / "RFQ-NEW111" / "notes.txt"
    stray.write_text("keep")
    os.utime(stray, (stale, stale))

    assert storage.purge_older_than(30) == 1
    with pytest.raises(StorageError):
        storage.read_pdf(old)
    assert not (_store / "RFQ-OLD111").exists()
    assert storage.read_pdf(new) == PDF
    assert outside.exists() and stray.exists()
    with pytest.raises(StorageError):
        storage.purge_older_than(-1)


def test_default_root_under_data_dir(monkeypatch):
    monkeypatch.delenv("VENDOR_QUOTE_STORE_DIR", raising=False)
    from app.data_loader import DATA_DIR
    assert storage.store_dir() == (DATA_DIR / "vendor_quote_store").resolve()
