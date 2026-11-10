"""Invoice intake workflow: upload (``pending_review``) -> human verify (``verified``) or reject (``rejected``).

* Extracted values are untrusted suggestions; only admin-submitted fields become an ``Invoice``/``DeliveryRecord``.
* Verification recomputes line totals, subtotal and total deterministically and refuses inconsistent input (422).
* Invoice ids are unique across ``data/invoices.json`` and verified uploads (409), enforced atomically with a
  reservation document keyed by the id in ``invoice_document_ids``.
* Every state change appends an event to the document (atomic with the transition) and mirrors it to the audit
  trail (``agent_logs`` under the synthetic id ``<doc_id>``). PDF text is never logged or written to audit.
* ``verified_invoices()`` / ``verified_deliveries()`` feed ``app.agents.billing`` and fail soft (empty list).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from pymongo.errors import DuplicateKeyError, PyMongoError

from app.data_loader import load_json
from app.db.errors import PersistenceError
from app.db.mongo import get_database, persistence_mode
from app.invoice_intake import extraction, repository
from app.invoice_intake.schemas import RejectIn, VerifyIn
from app.schemas import DeliveryRecord, Invoice

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = repository.MAX_PDF_BYTES
RESERVATIONS = "invoice_document_ids"
_TOL = 0.01
_INTERNAL = ("_id", "pdf_storage_key", "extracted_text")


class IntakeError(Exception):
    """Client-safe failure with an HTTP status; ``detail`` is a string or a JSON-able dict."""

    def __init__(self, status_code: int, detail: Any) -> None:
        super().__init__(detail if isinstance(detail, str) else detail.get("message", "error"))
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _r(x: float) -> float:
    return round(float(x) + 0.0, 2)


def _safe_filename(filename: Optional[str]) -> str:
    base = re.split(r"[\\/]", filename or "")[-1]
    return re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]", "_", base)).strip("._")[:120] or "invoice.pdf"


def _view(doc: Optional[dict], *, include_text: bool = False) -> Optional[dict]:
    if doc is None:
        return None
    out = {k: v for k, v in doc.items() if k not in _INTERNAL}
    if include_text:
        out["extracted_text"] = doc.get("extracted_text") or ""
        out["extracted_text_untrusted"] = True
    return out


def _event(doc_id: str, seq: int, event_type: str, actor: str, detail: str, extra: Optional[dict] = None) -> dict:
    return {"seq": seq, "event_type": event_type, "actor": actor, "at": _now(), "detail": detail,
            **(extra or {})}


def _mirror_audit(doc_id: str, event: dict) -> None:
    """Best-effort copy of a document event into the shared audit trail (the document copy is canonical)."""
    try:
        from app.repositories.audit_repository import append_audit_entries
        append_audit_entries(doc_id, [{
            "step": event["seq"], "agent": "Invoice Intake", "action": event["event_type"], "status": "info",
            "detail": f"{event['detail']} (by {event['actor']})", "evidence": [], "llm_used": False,
            "timestamp": event["at"].isoformat()}])
    except Exception:  # noqa: BLE001 - audit mirror must not undo a committed transition
        logger.warning("Invoice intake audit mirror failed for %s (%s)", doc_id, event["event_type"])


# ----------------------------------------------------------------------------------------------- upload
def upload(filename: Optional[str], data: bytes, actor: str) -> dict:
    if not data:
        raise IntakeError(422, "The uploaded file is empty.")
    if len(data) > MAX_PDF_BYTES:
        raise IntakeError(413, "PDF exceeds the 5 MiB upload limit.")
    if b"%PDF-" not in data[:1024]:
        raise IntakeError(415, "The file is not a PDF (file signature must be %PDF-).")
    try:
        result = extraction.extract_document(data)
    except extraction.PdfError as exc:
        raise IntakeError(422, exc.detail) from exc
    doc_id = f"INVDOC-{uuid4().hex[:12].upper()}"
    try:
        stored = repository.save_pdf(doc_id, data)
    except repository.StorageError as exc:
        raise PersistenceError("Invoice PDF storage is temporarily unavailable.") from exc
    warnings = list(result["warnings"])
    try:
        dup = repository.find_by_sha256(stored["sha256"])
    except PersistenceError:
        try:
            repository.delete_pdf(stored["storage_key"])
        except repository.StorageError:
            pass
        raise
    if dup:
        warnings.append(f"duplicate_pdf_of:{dup['doc_id']}")
    now = _now()
    event = _event(doc_id, 1, "invoice_document_uploaded", actor,
                   f"Invoice PDF uploaded ({result['page_count']} page(s), {stored['size']} bytes); "
                   f"extraction {result['method']}, {len(warnings)} warning(s), "
                   f"{len(result['injection_flags'])} injection flag(s).",
                   {"pdf_sha256": stored["sha256"]})
    doc = {
        "doc_id": doc_id, "status": "pending_review", "filename": _safe_filename(filename),
        "pdf_sha256": stored["sha256"], "pdf_size": stored["size"], "pdf_storage_key": stored["storage_key"],
        "page_count": result["page_count"], "uploaded_by": actor, "uploaded_at": now, "updated_at": now,
        "extracted": result["fields"], "extracted_delivery": result["delivery"],
        "extraction": {"method": result["method"], "llm_status": result["llm_status"],
                       "quality": result["quality"].get("quality"), "untrusted": True},
        "warnings": warnings, "injection_flags": result["injection_flags"],
        "extracted_text": result["text"],
        "verified": None, "verified_delivery": None, "verified_by": None, "verified_at": None,
        "rejected_reason": None, "rejected_by": None, "rejected_at": None,
        "events": [event],
    }
    try:
        repository.insert(doc)
    except PersistenceError:
        try:
            repository.delete_pdf(stored["storage_key"])
        except repository.StorageError:
            pass
        raise
    _mirror_audit(doc_id, event)
    logger.info("Invoice document %s uploaded: %s", doc_id, extraction.summary_for_log(result))
    return _view(doc)


# ------------------------------------------------------------------------------------------------ reads
def _require(doc_id: str) -> dict:
    if not repository.DOC_ID_RE.match(doc_id or ""):
        raise IntakeError(404, "Invoice document not found.")
    doc = repository.get(doc_id)
    if doc is None:
        raise IntakeError(404, "Invoice document not found.")
    return doc


def get_document(doc_id: str) -> dict:
    return _view(_require(doc_id), include_text=True)


def list_documents(status: Optional[str] = None, limit: int = 100) -> list[dict]:
    return [_view(d) for d in repository.list_docs(status, limit)]


def read_pdf(doc_id: str) -> tuple[bytes, str]:
    doc = _require(doc_id)
    try:
        data = repository.read_pdf(doc.get("pdf_storage_key") or "")
    except repository.StorageError as exc:
        raise IntakeError(404, "The stored PDF is not available.") from exc
    import hashlib
    if hashlib.sha256(data).hexdigest() != doc.get("pdf_sha256"):
        raise IntakeError(409, "The stored PDF failed its integrity check.")
    return data, doc.get("filename") or "invoice.pdf"


# ---------------------------------------------------------------------------------------------- verify
def arithmetic_errors(body: VerifyIn) -> list[dict]:
    """Deterministic recomputation; returns a list of {field, expected, actual, message} problems."""
    errors: list[dict] = []
    for i, li in enumerate(body.line_items):
        exp = _r(li.quantity * li.unit_price)
        if abs(exp - li.line_total) > _TOL:
            errors.append({"field": f"line_items.{i}.line_total", "expected": exp, "actual": li.line_total,
                           "message": f"{li.description}: {li.quantity:g} x {li.unit_price:.2f} = {exp:.2f}"})
    sub = _r(sum(li.line_total for li in body.line_items))
    if abs(sub - body.subtotal) > _TOL:
        errors.append({"field": "subtotal", "expected": sub, "actual": body.subtotal,
                       "message": f"Subtotal should be the sum of line totals ({sub:.2f})."})
    tot = _r(body.subtotal + body.tax)
    if abs(tot - body.total) > _TOL:
        errors.append({"field": "total", "expected": tot, "actual": body.total,
                       "message": f"Total should be subtotal + tax ({tot:.2f})."})
    return errors


def _demo_ids(name: str, key: str) -> set[str]:
    return {str(x.get(key)) for x in load_json(name) if isinstance(x, dict)}


def _reserve(kind: str, value: str, doc_id: str) -> bool:
    try:
        get_database()[RESERVATIONS].insert_one({"_id": f"{kind}:{value}", "doc_id": doc_id, "at": _now()})
        return True
    except DuplicateKeyError:
        return False
    except PyMongoError as exc:
        raise PersistenceError() from exc


def _release(kind: str, value: str, doc_id: str) -> None:
    try:
        get_database()[RESERVATIONS].delete_one({"_id": f"{kind}:{value}", "doc_id": doc_id})
    except PyMongoError:
        logger.warning("Could not release %s reservation for %s", kind, doc_id)


def verify(doc_id: str, body: VerifyIn, actor: str) -> dict:
    doc = _require(doc_id)
    if doc.get("status") != "pending_review":
        raise IntakeError(409, f"Only documents pending review can be verified (current: {doc.get('status')}).")
    errors = arithmetic_errors(body)
    if errors:
        raise IntakeError(422, {"message": "The invoice arithmetic is inconsistent; correct the fields and retry.",
                                "errors": errors})
    invoice = body.model_dump(mode="json", exclude={"delivery"})
    invoice["line_items"] = [{**li, "line_total": _r(li["line_total"])} for li in invoice["line_items"]]
    Invoice(**invoice)  # final schema check
    delivery: Optional[dict] = None
    if body.delivery is not None:
        d = body.delivery.model_dump(mode="json")
        delivery = {"delivery_id": d.get("delivery_id") or body.delivery_id, "po_id": body.po_id,
                    "vendor_id": body.vendor_id, "delivered_on": d["delivered_on"], "items": d["items"],
                    "signed_by": d["signed_by"], "notes": d.get("notes") or ""}
        if delivery["delivery_id"] != body.delivery_id:
            raise IntakeError(422, {"message": "The delivery note id must match the invoice's delivery_id.",
                                    "errors": [{"field": "delivery.delivery_id", "expected": body.delivery_id,
                                                "actual": delivery["delivery_id"],
                                                "message": "Delivery id mismatch."}]})
        DeliveryRecord(**delivery)
    if body.invoice_id in _demo_ids("invoices.json", "invoice_id"):
        raise IntakeError(409, f"Invoice id {body.invoice_id} already exists.")
    if delivery and delivery["delivery_id"] in _demo_ids("deliveries.json", "delivery_id"):
        raise IntakeError(409, f"Delivery id {delivery['delivery_id']} already exists.")
    if not _reserve("invoice", body.invoice_id, doc_id):
        raise IntakeError(409, f"Invoice id {body.invoice_id} already exists.")
    if delivery and not _reserve("delivery", delivery["delivery_id"], doc_id):
        _release("invoice", body.invoice_id, doc_id)
        raise IntakeError(409, f"Delivery id {delivery['delivery_id']} already exists.")
    changed = _diff(doc.get("extracted") or {}, invoice)
    now = _now()
    seq = len(doc.get("events") or []) + 1
    event = _event(doc_id, seq, "invoice_document_verified", actor,
                   f"Verified as {body.invoice_id} ({body.currency} {body.total:,.2f}); "
                   f"{len(changed)} field(s) edited from the extraction"
                   f"{'; delivery ' + delivery['delivery_id'] if delivery else ''}.",
                   {"invoice_id": body.invoice_id, "edited_fields": changed})
    try:
        ok = repository.update_if_status(doc_id, "pending_review", {
            "status": "verified", "verified": invoice, "verified_delivery": delivery, "verified_by": actor,
            "verified_at": now, "updated_at": now, "edited_fields": changed}, event)
    except PersistenceError:
        _release("invoice", body.invoice_id, doc_id)
        if delivery:
            _release("delivery", delivery["delivery_id"], doc_id)
        raise
    if not ok:
        _release("invoice", body.invoice_id, doc_id)
        if delivery:
            _release("delivery", delivery["delivery_id"], doc_id)
        raise IntakeError(409, "The document changed; reload and try again.")
    _mirror_audit(doc_id, event)
    return _view(repository.get(doc_id))


def _diff(extracted: dict, verified: dict) -> list[str]:
    return [k for k in extraction.FIELD_NAMES if extracted.get(k) != verified.get(k)]


def reject(doc_id: str, body: RejectIn, actor: str) -> dict:
    doc = _require(doc_id)
    if doc.get("status") != "pending_review":
        raise IntakeError(409, f"Only documents pending review can be rejected (current: {doc.get('status')}).")
    now = _now()
    seq = len(doc.get("events") or []) + 1
    event = _event(doc_id, seq, "invoice_document_rejected", actor, f"Rejected: {body.reason}")
    if not repository.update_if_status(doc_id, "pending_review", {
            "status": "rejected", "rejected_reason": body.reason, "rejected_by": actor, "rejected_at": now,
            "updated_at": now}, event):
        raise IntakeError(409, "The document changed; reload and try again.")
    _mirror_audit(doc_id, event)
    return _view(repository.get(doc_id))


# ------------------------------------------------------------------------------- billing integration
def _verified_docs_soft() -> list[dict]:
    if persistence_mode() == "disabled":
        return []
    try:
        return repository.verified_docs()
    except Exception:  # noqa: BLE001 - billing falls back to the JSON fixtures
        logger.warning("Verified invoice documents unavailable; using demo invoices only.")
        return []


def verified_invoices() -> list[Invoice]:
    """VERIFIED uploaded invoices (empty if Mongo is unavailable or persistence is disabled)."""
    out: list[Invoice] = []
    for d in _verified_docs_soft():
        try:
            out.append(Invoice(**d["verified"]))
        except Exception:  # noqa: BLE001 - a malformed record is skipped, never fatal
            logger.warning("Skipping malformed verified invoice document %s", d.get("doc_id"))
    return out


def verified_deliveries() -> list[DeliveryRecord]:
    out: list[DeliveryRecord] = []
    for d in _verified_docs_soft():
        if not d.get("verified_delivery"):
            continue
        try:
            out.append(DeliveryRecord(**d["verified_delivery"]))
        except Exception:  # noqa: BLE001
            logger.warning("Skipping malformed verified delivery in %s", d.get("doc_id"))
    return out
