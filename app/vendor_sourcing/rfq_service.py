"""RFQ lifecycle, quote intake, verification and deterministic comparison.

No outbound supplier contact, contract award, PO dispatch, or payment is performed here.
PDF text is untrusted and remains ineligible for comparison until a human verifies its terms.

Consistency model (no multi-document transactions): every business state change is ONE single-document write
that also appends its audit event to that document's ``pending_events`` outbox (deterministic
``event_id = f"{entity_id}:{seq}"`` guarded by an ``event_seq`` compare-and-set). Events are then delivered to
``vendor_rfq_events`` (duplicate ids are ignored, so delivery is idempotent) and removed from the outbox.
If delivery fails the committed state is still returned, flagged ``audit_pending: true``; reads and
``repair_pending_events()`` re-deliver. Entities: RFQ docs (``vendor_rfqs``), quote docs (``vendor_quotes``) and
comparison docs (``vendor_rfq_comparisons``) each carry their own outbox; all events carry ``rfq_id``.
``quotes_received`` is computed from the quote collection on read, never kept as a separate counter.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
from contextlib import nullcontext
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import uuid4

from pypdf import PdfReader
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.data_loader import load_json
from app.db.errors import PersistenceError
from app.db.mongo import get_database
from app.repositories.ticket_repository import get_ticket
from app.schemas import EventRequest
from app.vendor_sourcing import metrics, repository, scoring, storage

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_EXTRACTED_CHARS = 100_000
RFQ_STATUSES = ("open", "closed", "cancelled")
MONEY_FIELDS = ("unit_price", "tax_amount", "delivery_fee", "other_fees")
TERM_FIELDS = ("currency", *MONEY_FIELDS, "delivery_date", "warranty_months", "warranty_scope", "compliance")
_INTERNAL_FIELDS = ("_id", "pdf_bytes", "pdf_storage_key", "pending_events", "event_seq", "active",
                    "previous_status")
_CORR_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_MOCK_LOCK = threading.RLock()

NOTICE_ADVISORY = "Comparison is advisory. No vendor is awarded and no purchase order is created or sent."
RANKING_METHOD = ("Eligible quotes only; lowest total cost, then earliest delivery, then quote ID. "
                  "The policy score is explanatory only and never changes eligibility or rank.")


class RFQValidationError(ValueError):
    """Client input is invalid for this RFQ (HTTP 422)."""


class RFQConflictError(ValueError):
    """The request conflicts with the current RFQ/quote/comparison state (HTTP 409)."""


class RFQNotFoundError(LookupError):
    """RFQ, quotation or comparison not found (HTTP 404)."""


# --------------------------------------------------------------------------------------------- helpers

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Any) -> Any:
    """Mongo may hand back naive datetimes (client without tz_aware); stored values are always UTC."""
    if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
        return value.replace(tzinfo=timezone.utc)
    return value


def _id(prefix: str) -> str:
    return prefix + "-" + uuid4().hex[:12].upper()


def correlation_id_or_new(value: Optional[str]) -> str:
    return value if value and _CORR_RE.match(value) else f"corr-{uuid4().hex[:16]}"


def _db():
    return get_database()


def _lock(coll: Any):
    return _MOCK_LOCK if type(coll).__module__.startswith("mongomock") else nullcontext()


def _canonical_hash(obj: Any) -> str:
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _safe(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    pending = doc.get("pending_events") or []
    for key in _INTERNAL_FIELDS:
        doc.pop(key, None)
    doc["audit_pending"] = bool(pending)
    for key in ("created_at", "updated_at", "submitted_at", "verified_at", "response_deadline", "closed_at",
                "cancelled_at"):
        if key in doc:
            doc[key] = _aware(doc[key])
    return doc


def _persistence(op: str, message: str, exc: Exception) -> PersistenceError:
    metrics.inc("persistence_failures", op)
    return PersistenceError(message)


# --------------------------------------------------------------------------------------------- indexes

def ensure_indexes() -> None:
    from app.db.indexes import _ensure_vendor_rfq_indexes
    try:
        _ensure_vendor_rfq_indexes(_db())
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: RFQ indexes could not be initialized.") from exc


# --------------------------------------------------------------------------------------------- outbox

def _make_event(*, rfq_id: str, entity_type: str, entity_id: str, seq: int, event_type: str, actor: str,
                correlation_id: Optional[str], from_status: Optional[str] = None,
                to_status: Optional[str] = None, reason: Optional[str] = None,
                details: Optional[dict] = None) -> dict:
    return {
        "event_id": f"{entity_id}:{seq}", "seq": seq, "rfq_id": rfq_id, "entity_type": entity_type,
        "entity_id": entity_id, "event_type": event_type, "action": event_type, "actor": actor,
        "from_status": from_status, "to_status": to_status, "reason": reason, "details": details or {},
        "created_at": _now(), "correlation_id": correlation_id_or_new(correlation_id),
    }


def _insert_event(event: dict) -> None:
    """Deliver one event idempotently (upsert on the deterministic event_id; duplicates are no-ops)."""
    try:
        _db().vendor_rfq_events.update_one({"event_id": event["event_id"]}, {"$setOnInsert": dict(event)},
                                           upsert=True)
    except DuplicateKeyError:
        pass


def _deliver(coll: Any, key_field: str, doc: dict) -> bool:
    """Deliver the doc's outbox; returns True when nothing remains pending. Never raises."""
    events = list(doc.get("pending_events") or [])
    if not events:
        return True
    try:
        for event in events:
            _insert_event(event)
        ids = [e["event_id"] for e in events]
        coll.update_one({key_field: doc[key_field]}, {"$pull": {"pending_events": {"event_id": {"$in": ids}}}})
        doc["pending_events"] = [e for e in (doc.get("pending_events") or []) if e["event_id"] not in ids]
        return not doc["pending_events"]
    except PyMongoError:
        metrics.inc("persistence_failures", "audit_delivery")
        return False
    except Exception:  # pragma: no cover - delivery is best effort; the outbox keeps the events
        metrics.inc("persistence_failures", "audit_delivery")
        return False


def _seq_filter(doc: dict) -> dict:
    seq = int(doc.get("event_seq") or 0)
    return {"event_seq": seq} if seq else {"event_seq": {"$in": [0, None]}}


def _commit(coll: Any, key_field: str, doc: dict, extra_filter: dict, set_fields: dict, event_kwargs: dict,
            actor: str, correlation_id: Optional[str]) -> Optional[dict]:
    """CAS single-document state change + outbox event. Returns the updated doc, or None if it lost the race."""
    seq = int(doc.get("event_seq") or 0) + 1
    event = _make_event(rfq_id=doc["rfq_id"], entity_id=doc[key_field], seq=seq, actor=actor,
                        correlation_id=correlation_id, **event_kwargs)
    now = _now()
    flt = {key_field: doc[key_field], **_seq_filter(doc), **extra_filter}
    result = coll.update_one(flt, {"$set": {**set_fields, "event_seq": seq, "updated_at": now},
                                   "$push": {"pending_events": event}})
    if result.modified_count != 1:
        return None
    return coll.find_one({key_field: doc[key_field]})


def _repair_doc(coll: Any, key_field: str, doc: Optional[dict]) -> Optional[dict]:
    if doc is not None and doc.get("pending_events"):
        _deliver(coll, key_field, doc)
    return doc


def repair_pending_events() -> dict:
    """Idempotently deliver every pending outbox event and heal interrupted revisions. Returns counts."""
    db = _db()
    delivered = remaining = healed = 0
    try:
        for coll, key in ((db.vendor_rfqs, "rfq_id"), (db.vendor_quotes, "quote_id"),
                          (db.vendor_rfq_comparisons, "comparison_id")):
            for doc in list(coll.find({"pending_events.0": {"$exists": True}})):
                before = len(doc.get("pending_events") or [])
                ok = _deliver(coll, key, doc)
                after = len(doc.get("pending_events") or []) if not ok else 0
                delivered += before - after
                remaining += after
        # A revision whose new quote insert failed (and whose rollback also failed) leaves the previous quote
        # superseded by a quote that does not exist: restore it.
        for doc in list(db.vendor_quotes.find({"status": "superseded", "superseded_by": {"$ne": None}})):
            if db.vendor_quotes.find_one({"quote_id": doc["superseded_by"]}, {"_id": 1}) is None:
                db.vendor_quotes.update_one(
                    {"quote_id": doc["quote_id"], "superseded_by": doc["superseded_by"]},
                    {"$set": {"status": doc.get("previous_status") or "pending_verification", "active": True,
                              "superseded_by": None, "updated_at": _now()}})
                healed += 1
    except PyMongoError as exc:
        raise _persistence("repair", "Database unavailable: RFQ audit repair could not run.", exc) from exc
    return {"delivered": delivered, "remaining": remaining, "revisions_restored": healed}


# --------------------------------------------------------------------------------------------- RFQs

def _vendor_is_eligible(vendor_id: str) -> bool:
    for vendor in load_json("vendors.json"):
        if vendor.get("vendor_id") == vendor_id:
            return bool(vendor.get("approved"))
    candidate = repository.get_candidate(vendor_id)
    return bool(candidate and candidate.get("status") == "approved")


def _validate_source_ticket(body) -> None:
    """Bind RFQ scope to a persisted, completed workflow result instead of trusting client-supplied facts."""
    ticket = get_ticket(body.request_id)
    if ticket is None:
        raise RFQValidationError("RFQ must reference an existing persisted workflow ticket.")
    result = ticket.get("workflow_result") or {}
    if result.get("final_status") not in {"pass", "warning"}:
        raise RFQValidationError("RFQ source ticket must have a completed pass/warning workflow result.")
    if result.get("missing_fields"):
        raise RFQValidationError("RFQ source ticket still has missing required fields.")
    raw_request = result.get("request")
    if not isinstance(raw_request, dict):
        raise RFQValidationError("RFQ source ticket has no validated request snapshot.")
    try:
        source = EventRequest.model_validate(raw_request)
    except Exception as exc:
        raise RFQValidationError("RFQ source ticket request snapshot is invalid.") from exc
    if source.date is None or source.attendees is None or source.budget_limit is None or not source.service:
        raise RFQValidationError("RFQ source ticket is missing required purchase requirements.")
    if source.service.casefold() != body.service.casefold():
        raise RFQValidationError("RFQ service must match the validated source ticket.")
    if Decimal(str(source.attendees)) != body.quantity:
        raise RFQValidationError("RFQ quantity must match the validated attendee/quantity count.")
    if Decimal(str(source.budget_limit)) != body.budget_limit:
        raise RFQValidationError("RFQ budget must match the validated source ticket.")
    if source.date != body.delivery_date:
        raise RFQValidationError("RFQ delivery date must match the validated source ticket.")
    if source.currency.upper() != body.currency:
        raise RFQValidationError("RFQ currency must match the validated source ticket.")
    if source.location and source.location.casefold() != body.delivery_location.casefold():
        raise RFQValidationError("RFQ delivery location must match the validated source ticket.")


def create_rfq(body, actor: str, correlation_id: Optional[str] = None) -> dict:
    _validate_source_ticket(body)
    now = _now()
    if body.delivery_date < now.date():
        raise RFQValidationError("Delivery date must be today or in the future.")
    if body.response_deadline <= now:
        raise RFQValidationError("Response deadline must be in the future.")
    if body.response_deadline.astimezone(timezone.utc).date() > body.delivery_date:
        raise RFQValidationError("Response deadline cannot be after the requested delivery date.")
    if len(set(body.candidate_ids)) != len(body.candidate_ids):
        raise RFQValidationError("candidate_ids must not contain duplicates.")
    ineligible = [vendor_id for vendor_id in body.candidate_ids if not _vendor_is_eligible(vendor_id)]
    if ineligible:
        raise RFQValidationError("RFQ recipients must be approved internal vendors or approved candidates: "
                                 + ", ".join(ineligible))
    doc = body.model_dump(mode="python")
    doc["quantity"] = str(body.quantity)
    doc["budget_limit"] = str(body.budget_limit)
    doc["delivery_date"] = body.delivery_date.isoformat()
    doc["response_deadline"] = body.response_deadline.astimezone(timezone.utc)
    rfq_id = _id("RFQ")
    snapshot_hash = hashlib.sha256(
        repr(sorted((k, str(v)) for k, v in body.model_dump().items())).encode("utf-8")).hexdigest()
    event = _make_event(rfq_id=rfq_id, entity_type="rfq", entity_id=rfq_id, seq=1, event_type="rfq_created",
                        actor=actor, correlation_id=correlation_id, to_status="open",
                        details={"request_id": doc["request_id"], "candidate_count": len(doc["candidate_ids"]),
                                 "requirements_snapshot_hash": snapshot_hash})
    doc.update({
        "rfq_id": rfq_id, "status": "open", "created_by": actor, "created_at": now, "updated_at": now,
        "requirements_snapshot_hash": snapshot_hash, "status_history": [
            {"status": "open", "at": now, "by": actor, "reason": None}],
        "event_seq": 1, "pending_events": [event],
    })
    coll = _db().vendor_rfqs
    try:
        coll.insert_one(doc)
    except DuplicateKeyError as exc:
        raise RFQConflictError("RFQ already exists.") from exc
    except PyMongoError as exc:
        raise _persistence("rfq_create", "Database unavailable: RFQ could not be saved.", exc) from exc
    metrics.inc("rfqs_created")
    _deliver(coll, "rfq_id", doc)
    doc["quotes_received"] = 0
    doc["accepting_quotes"] = True
    return _safe(doc)


def _load_rfq(rfq_id: str) -> Optional[dict]:
    try:
        return _db().vendor_rfqs.find_one({"rfq_id": rfq_id})
    except PyMongoError as exc:
        raise _persistence("rfq_read", "Database unavailable: RFQ could not be read.", exc) from exc


def _quotes_received(rfq_id: str) -> int:
    return _db().vendor_quotes.count_documents({"rfq_id": rfq_id, "superseded_by": None})


def _accepting_quotes_reason(rfq: dict) -> Optional[str]:
    if rfq.get("status") != "open":
        return f"RFQ is {rfq.get('status')}; it is not open for quotations."
    deadline = _aware(rfq.get("response_deadline"))
    if isinstance(deadline, datetime) and deadline <= _now():
        return "RFQ response deadline has passed."
    return None


def _rfq_view(doc: dict) -> dict:
    coll = _db().vendor_rfqs
    _repair_doc(coll, "rfq_id", doc)
    doc["quotes_received"] = _quotes_received(doc["rfq_id"])
    doc["accepting_quotes"] = _accepting_quotes_reason(doc) is None
    return _safe(doc)


def get_rfq(rfq_id: str) -> Optional[dict]:
    doc = _load_rfq(rfq_id)
    if doc is None:
        return None
    try:
        return _rfq_view(doc)
    except PyMongoError as exc:
        raise _persistence("rfq_read", "Database unavailable: RFQ could not be read.", exc) from exc


def list_rfqs(status: Optional[str] = None, request_id: Optional[str] = None, limit: int = 50,
              offset: int = 0) -> dict:
    limit = max(1, min(int(limit), 200))
    offset = max(0, min(int(offset), 10_000))
    query: dict[str, Any] = {}
    if status:
        if status not in RFQ_STATUSES:
            raise RFQValidationError("Unknown RFQ status filter.")
        query["status"] = status
    if request_id:
        query["request_id"] = request_id
    try:
        coll = _db().vendor_rfqs
        total = coll.count_documents(query)
        docs = list(coll.find(query).sort([("created_at", DESCENDING), ("rfq_id", ASCENDING)])
                    .skip(offset).limit(limit))
        items = [_rfq_view(d) for d in docs]
    except PyMongoError as exc:
        raise _persistence("rfq_list", "Database unavailable: RFQs could not be listed.", exc) from exc
    next_offset = offset + len(items) if offset + len(items) < total else None
    return {"items": items, "total": total, "limit": limit, "offset": offset, "next_offset": next_offset}


def _change_rfq_status(rfq_id: str, to_status: str, reason: str, actor: str,
                       correlation_id: Optional[str], allowed_from: tuple[str, ...] = ("open",)) -> dict:
    coll = _db().vendor_rfqs
    try:
        with _lock(coll):
            doc = coll.find_one({"rfq_id": rfq_id})
            if doc is None:
                raise RFQNotFoundError("RFQ not found.")
            current = doc.get("status")
            if current not in allowed_from:
                raise RFQConflictError(f"RFQ is {current}; it cannot be {to_status}.")
            now = _now()
            history = list(doc.get("status_history") or [])
            history.append({"status": to_status, "at": now, "by": actor, "reason": reason})
            updated = _commit(coll, "rfq_id", doc, {"status": current},
                              {"status": to_status, "status_history": history, f"{to_status}_at": now,
                               f"{to_status}_by": actor, "status_reason": reason},
                              {"entity_type": "rfq", "event_type": f"rfq_{to_status}", "from_status": current,
                               "to_status": to_status, "reason": reason}, actor, correlation_id)
        if updated is None:
            raise RFQConflictError("RFQ changed concurrently; reload and retry.")
    except (LookupError, ValueError):
        raise
    except PyMongoError as exc:
        raise _persistence("rfq_state", "Database unavailable: RFQ status could not be changed.", exc) from exc
    metrics.inc("rfq_state_changes", to_status)
    return _rfq_view(updated)


def close_rfq(rfq_id: str, reason: str, actor: str, correlation_id: Optional[str] = None) -> dict:
    return _change_rfq_status(rfq_id, "closed", reason, actor, correlation_id)


def cancel_rfq(rfq_id: str, reason: str, actor: str, correlation_id: Optional[str] = None) -> dict:
    return _change_rfq_status(rfq_id, "cancelled", reason, actor, correlation_id)


def list_events(rfq_id: str, limit: int = 500) -> dict:
    """Delivered audit events plus any still-pending outbox events for this RFQ."""
    limit = max(1, min(int(limit), 1000))
    if _load_rfq(rfq_id) is None:
        raise RFQNotFoundError("RFQ not found.")
    db = _db()
    try:
        events = list(db.vendor_rfq_events.find({"rfq_id": rfq_id}, {"_id": 0})
                      .sort([("created_at", ASCENDING), ("event_id", ASCENDING)]).limit(limit))
        pending = []
        for coll in (db.vendor_rfqs, db.vendor_quotes, db.vendor_rfq_comparisons):
            for doc in coll.find({"rfq_id": rfq_id, "pending_events.0": {"$exists": True}}, {"pending_events": 1}):
                pending.extend(doc.get("pending_events") or [])
    except PyMongoError as exc:
        raise _persistence("events_read", "Database unavailable: RFQ events could not be read.", exc) from exc
    for event in events + pending:
        event["created_at"] = _aware(event.get("created_at"))
    return {"rfq_id": rfq_id, "events": events, "pending_events": pending}


# --------------------------------------------------------------------------------------------- quotes

def _compliance_entry(value: Any) -> dict:
    """Normalize stored/legacy compliance values: bool True -> declared, False/None -> missing."""
    if isinstance(value, bool) or value is None:
        return {"status": "declared" if value else "missing", "evidence_ref": None, "expires_on": None}
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        status = value.get("status")
        if status not in {"verified", "declared", "missing"}:
            status = "missing"
        expires = value.get("expires_on")
        return {"status": status, "evidence_ref": value.get("evidence_ref"),
                "expires_on": _date(expires).isoformat() if expires else None}
    return {"status": "missing", "evidence_ref": None, "expires_on": None}


def _terms(body) -> dict:
    """Serialize quote terms for storage (Decimal -> str, date -> ISO)."""
    out = {
        "currency": body.currency,
        "unit_price": str(body.unit_price), "tax_amount": str(body.tax_amount),
        "delivery_fee": str(body.delivery_fee), "other_fees": str(body.other_fees),
        "delivery_date": body.delivery_date.isoformat(), "warranty_months": int(body.warranty_months),
        "warranty_scope": body.warranty_scope,
        "compliance": {k: _compliance_entry(v) for k, v in (body.compliance or {}).items()},
    }
    return out


def _quote_view(doc: dict) -> dict:
    _repair_doc(_db().vendor_quotes, "quote_id", doc)
    doc.setdefault("revision", 1)
    doc.setdefault("revision_of", None)
    doc.setdefault("superseded_by", None)
    return _safe(doc)


def list_quotes(rfq_id: str) -> list[dict]:
    try:
        docs = list(_db().vendor_quotes.find({"rfq_id": rfq_id}).sort(
            [("submitted_at", DESCENDING), ("quote_id", ASCENDING)]))
        return [_quote_view(q) for q in docs]
    except PyMongoError as exc:
        raise _persistence("quote_list", "Database unavailable: quotations could not be listed.", exc) from exc


def get_quote(rfq_id: str, quote_id: str) -> Optional[dict]:
    try:
        doc = _db().vendor_quotes.find_one({"rfq_id": rfq_id, "quote_id": quote_id})
        return _quote_view(doc) if doc else None
    except PyMongoError as exc:
        raise _persistence("quote_read", "Database unavailable: quotation could not be read.", exc) from exc


def _require_open_rfq_for_vendor(rfq_id: str, vendor_id: str) -> dict:
    rfq = _load_rfq(rfq_id)
    if rfq is None:
        raise RFQNotFoundError("RFQ not found.")
    reason = _accepting_quotes_reason(rfq)
    if reason:
        raise RFQConflictError(reason)
    if vendor_id not in (rfq.get("candidate_ids") or []):
        raise RFQConflictError("Vendor is not an invited RFQ recipient.")
    if not _vendor_is_eligible(vendor_id):
        raise RFQConflictError("Vendor is not currently approved.")
    return rfq


def _new_quote_doc(rfq: dict, quote_id: str, vendor_id: str, quote_reference: str, submission_type: str,
                   actor: str, correlation_id: Optional[str], *, revision: int = 1,
                   revision_of: Optional[str] = None, event_type: str = "quote_submitted",
                   event_details: Optional[dict] = None) -> dict:
    now = _now()
    event = _make_event(rfq_id=rfq["rfq_id"], entity_type="quote", entity_id=quote_id, seq=1,
                        event_type=event_type, actor=actor, correlation_id=correlation_id,
                        to_status="pending_verification",
                        details={"quote_id": quote_id, "vendor_id": vendor_id, "submission_type": submission_type,
                                 "revision": revision, "revision_of": revision_of, **(event_details or {})})
    return {
        "quote_id": quote_id, "rfq_id": rfq["rfq_id"], "vendor_id": vendor_id,
        "quote_reference": quote_reference[:120], "submission_type": submission_type,
        "status": "pending_verification", "active": True, "revision": revision, "revision_of": revision_of,
        "superseded_by": None, "quantity": rfq["quantity"], "currency": None,
        "unit_price": None, "tax_amount": None, "delivery_fee": None, "other_fees": None,
        "delivery_date": None, "warranty_months": None, "warranty_scope": None, "compliance": {},
        "submitted": {}, "verified": None, "verification": None,
        "pdf_filename": None, "pdf_sha256": None, "pdf_storage_key": None, "pdf_size": None,
        "pdf_page_count": None, "extracted_text": None, "notes": "",
        "submitted_by": actor, "submitted_at": now, "updated_at": now,
        "verified_at": None, "verified_by": None, "reviewer_note": None,
        "event_seq": 1, "pending_events": [event],
    }


def _apply_structured(doc: dict, body) -> None:
    terms = _terms(body)
    doc["submitted"] = dict(terms, notes=body.notes)
    # Top-level fields mirror the submitted values until verification replaces them with verified values.
    doc.update(terms)
    doc["notes"] = body.notes


def _insert_quote(doc: dict, label: str) -> dict:
    coll = _db().vendor_quotes
    try:
        with _lock(coll):
            if coll.find_one({"rfq_id": doc["rfq_id"], "vendor_id": doc["vendor_id"], "superseded_by": None,
                              "quote_id": {"$ne": doc.get("revision_of")}}, {"_id": 1}):
                raise RFQConflictError("A quotation from this vendor has already been submitted for this RFQ; "
                                       "use the revise endpoint to submit a new revision.")
            coll.insert_one(doc)
    except DuplicateKeyError as exc:
        raise RFQConflictError("A quotation from this vendor has already been submitted for this RFQ; "
                               "use the revise endpoint to submit a new revision.") from exc
    except PyMongoError as exc:
        raise _persistence("quote_create", "Database unavailable: quotation could not be saved.", exc) from exc
    metrics.inc("quotes_by_type", label)
    _deliver(coll, "quote_id", doc)
    return _safe(doc)


def submit_structured_quote(rfq_id: str, body, actor: str, correlation_id: Optional[str] = None) -> dict:
    rfq = _require_open_rfq_for_vendor(rfq_id, body.vendor_id)
    doc = _new_quote_doc(rfq, _id("QUOTE"), body.vendor_id, body.quote_reference, "structured", actor,
                         correlation_id)
    _apply_structured(doc, body)
    return _insert_quote(doc, "structured")


def revise_quote(rfq_id: str, quote_id: str, body, actor: str, correlation_id: Optional[str] = None) -> dict:
    """Create revision n+1 of an active quote; the previous quote is marked superseded (never overwritten)."""
    coll = _db().vendor_quotes
    try:
        previous = coll.find_one({"rfq_id": rfq_id, "quote_id": quote_id})
    except PyMongoError as exc:
        raise _persistence("quote_read", "Database unavailable: quotation could not be read.", exc) from exc
    if previous is None:
        raise RFQNotFoundError("Quotation not found.")
    if previous.get("superseded_by") or previous.get("status") == "superseded":
        raise RFQConflictError("Only the latest revision of a quotation can be revised.")
    rfq = _require_open_rfq_for_vendor(rfq_id, previous["vendor_id"])
    new_id = _id("QUOTE")
    revision = int(previous.get("revision") or 1) + 1
    prev_status = previous.get("status")
    try:
        with _lock(coll):
            superseded = _commit(
                coll, "quote_id", previous, {"superseded_by": None, "status": prev_status},
                {"status": "superseded", "active": False, "superseded_by": new_id, "previous_status": prev_status},
                {"entity_type": "quote", "event_type": "quote_superseded", "from_status": prev_status,
                 "to_status": "superseded", "reason": body.revision_reason,
                 "details": {"quote_id": quote_id, "superseded_by": new_id}}, actor, correlation_id)
    except PyMongoError as exc:
        raise _persistence("quote_revise", "Database unavailable: quotation could not be revised.", exc) from exc
    if superseded is None:
        raise RFQConflictError("Quotation changed concurrently; reload and retry.")
    doc = _new_quote_doc(rfq, new_id, previous["vendor_id"], body.quote_reference, "structured", actor,
                         correlation_id, revision=revision, revision_of=quote_id, event_type="quote_revised",
                         event_details={"revision_reason": body.revision_reason})
    _apply_structured(doc, body)
    doc["revision_reason"] = body.revision_reason
    try:
        result = _insert_quote(doc, "revision")
    except (RFQConflictError, PersistenceError):
        try:  # roll the supersession back; repair_pending_events() heals it if this also fails
            coll.update_one({"quote_id": quote_id, "superseded_by": new_id},
                            {"$set": {"status": prev_status, "active": True, "superseded_by": None,
                                      "updated_at": _now()}})
        except PyMongoError:
            metrics.inc("persistence_failures", "quote_revise_rollback")
        raise
    _deliver(coll, "quote_id", superseded)
    return result


def _parse_pdf(pdf_bytes: bytes) -> tuple[str, int]:
    if not pdf_bytes:
        raise RFQValidationError("PDF must be non-empty.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise RFQValidationError("PDF must be no larger than 5 MiB.")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise RFQValidationError("Uploaded file is not a valid PDF signature.")
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=True)
        if reader.is_encrypted:
            raise RFQValidationError("Encrypted PDFs are not accepted.")
        pages = len(reader.pages)
        if pages < 1 or pages > MAX_PDF_PAGES:
            raise RFQValidationError(f"PDF must contain between 1 and {MAX_PDF_PAGES} pages.")
        extracted = "\n".join((page.extract_text() or "") for page in reader.pages)[:MAX_EXTRACTED_CHARS]
    except RFQValidationError:
        raise
    except Exception as exc:
        raise RFQValidationError("PDF could not be parsed safely.") from exc
    return extracted, pages


def submit_pdf_quote(rfq_id: str, vendor_id: str, quote_reference: str, filename: str,
                     pdf_bytes: bytes, actor: str, correlation_id: Optional[str] = None) -> dict:
    rfq = _require_open_rfq_for_vendor(rfq_id, vendor_id)
    extracted, pages = _parse_pdf(pdf_bytes)
    # Display/Content-Disposition metadata only (storage keys are server-generated): basename, no dot runs.
    base = re.split(r"[\\/]", filename or "")[-1]
    safe_filename = re.sub(r"\.{2,}", ".", re.sub(r"[^A-Za-z0-9._-]", "_", base)).strip("._")[:120] or "quotation.pdf"
    quote_id = _id("QUOTE")
    try:
        stored = storage.save_pdf(rfq_id, quote_id, pdf_bytes)
    except storage.StorageError as exc:
        raise _persistence("pdf_store", "Quotation PDF storage is temporarily unavailable.", exc) from exc
    doc = _new_quote_doc(rfq, quote_id, vendor_id, quote_reference, "pdf", actor, correlation_id,
                         event_details={"pdf_sha256": stored["sha256"], "pdf_size": stored["size"],
                                        "pdf_page_count": pages})
    pdf_meta = {"pdf_filename": safe_filename, "pdf_sha256": stored["sha256"],
                "pdf_storage_key": stored["storage_key"], "pdf_size": stored["size"], "pdf_page_count": pages}
    doc.update(pdf_meta)
    doc["submitted"] = dict(pdf_meta)
    doc["extracted_text"] = extracted
    doc["parser_status"] = "text_extracted_unverified"
    doc["notes"] = "PDF terms are unverified; human verification is required before comparison."
    try:
        return _insert_quote(doc, "pdf")
    except (RFQConflictError, PersistenceError):
        try:
            storage.delete_pdf(stored["storage_key"])
        except storage.StorageError:
            pass
        raise


def read_quote_pdf(rfq_id: str, quote_id: str) -> tuple[bytes, str]:
    try:
        doc = _db().vendor_quotes.find_one({"rfq_id": rfq_id, "quote_id": quote_id})
    except PyMongoError as exc:
        raise _persistence("quote_read", "Database unavailable: quotation could not be read.", exc) from exc
    if doc is None or doc.get("submission_type") != "pdf" or not doc.get("pdf_storage_key"):
        raise RFQNotFoundError("Quotation PDF not found.")
    try:
        data = storage.read_pdf(doc["pdf_storage_key"])
    except storage.StorageError as exc:
        if "not found" in str(exc):
            raise RFQNotFoundError("Quotation PDF not found.") from exc
        raise _persistence("pdf_read", "Quotation PDF storage is temporarily unavailable.", exc) from exc
    if hashlib.sha256(data).hexdigest() != doc.get("pdf_sha256"):
        raise _persistence("pdf_integrity", "Stored quotation PDF failed its integrity check.",
                           ValueError("sha256 mismatch"))
    return data, doc.get("pdf_filename") or "quotation.pdf"


def _diff(submitted: dict, verified: dict) -> list[dict]:
    changes = []
    for field in TERM_FIELDS:
        if field == "compliance":
            before = submitted.get("compliance") or {}
            after = verified.get("compliance") or {}
            for req in sorted(set(before) | set(after)):
                b = _compliance_entry(before[req]) if req in before else None
                a = _compliance_entry(after[req]) if req in after else None
                if a != b:
                    changes.append({"field": f"compliance.{req}", "submitted": b, "verified": a})
            continue
        if submitted.get(field) != verified.get(field):
            changes.append({"field": field, "submitted": submitted.get(field), "verified": verified.get(field)})
    return changes


def verify_quote(rfq_id: str, quote_id: str, body, actor: str, correlation_id: Optional[str] = None) -> dict:
    coll = _db().vendor_quotes
    try:
        rfq = _load_rfq(rfq_id)
        if rfq is None:
            raise RFQNotFoundError("RFQ not found.")
        if rfq.get("status") == "cancelled":
            raise RFQConflictError("RFQ is cancelled; quotations cannot be verified.")
        with _lock(coll):
            quote = coll.find_one({"rfq_id": rfq_id, "quote_id": quote_id})
            if quote is None:
                raise RFQNotFoundError("Quotation not found.")
            if quote.get("status") != "pending_verification" or quote.get("superseded_by"):
                raise RFQConflictError("Only the latest quotation awaiting verification can be verified.")
            now = _now()
            verified = _terms(body)
            verified["warranty_scope_accepted"] = bool(body.warranty_scope_accepted)
            submitted = quote.get("submitted") or {}
            diff = _diff(submitted, verified)
            verification = {"reviewer": actor, "reviewed_at": now, "note": body.reviewer_note, "diff": diff,
                            "fields_changed": [d["field"] for d in diff],
                            "warranty_scope_accepted": bool(body.warranty_scope_accepted)}
            fields = {k: verified[k] for k in TERM_FIELDS}
            fields.update({"status": "verified", "verified": verified, "verification": verification,
                           "verified_by": actor, "verified_at": now, "reviewer_note": body.reviewer_note})
            updated = _commit(coll, "quote_id", quote, {"status": "pending_verification", "superseded_by": None},
                              fields,
                              {"entity_type": "quote", "event_type": "quote_verified",
                               "from_status": "pending_verification", "to_status": "verified",
                               "reason": body.reviewer_note,
                               "details": {"quote_id": quote_id, "vendor_id": quote["vendor_id"],
                                           "fields_changed": verification["fields_changed"]}},
                              actor, correlation_id)
        if updated is None:
            raise RFQConflictError("Quotation changed concurrently; reload and retry.")
    except (LookupError, ValueError):
        raise
    except PyMongoError as exc:
        raise _persistence("quote_verify", "Database unavailable: quotation verification could not be saved.",
                           exc) from exc
    metrics.inc("verifications", "changed" if diff else "unchanged")
    return _quote_view(updated)


# --------------------------------------------------------------------------------------------- comparison

def _rfq_snapshot(rfq: dict) -> dict:
    keys = ("rfq_id", "request_id", "service", "quantity", "budget_limit", "currency", "delivery_date",
            "delivery_location", "warranty_months_required", "warranty_scope_required",
            "compliance_requirements", "candidate_ids", "requirements_snapshot_hash")
    return {k: rfq.get(k) for k in keys}


def _quote_snapshot_hash(quote: dict) -> str:
    return _canonical_hash({"quote_id": quote["quote_id"], "rfq_id": quote["rfq_id"],
                            "vendor_id": quote["vendor_id"], "revision": int(quote.get("revision") or 1),
                            "status": quote.get("status"), "verified": quote.get("verified")})


def _verified_terms(quote: dict) -> dict:
    """Comparable terms come from ``verified``; legacy verified quotes fall back to top-level fields."""
    verified = quote.get("verified")
    if isinstance(verified, dict) and verified:
        return verified
    return {k: quote.get(k) for k in (*TERM_FIELDS, "warranty_scope_accepted")}


def _evaluate(quote: dict, rfq: dict, quantity: Decimal, required_delivery: date) -> dict:
    base = {"quote_id": quote["quote_id"], "vendor_id": quote["vendor_id"],
            "revision": int(quote.get("revision") or 1), "quote_snapshot_hash": _quote_snapshot_hash(quote)}
    if quote.get("status") != "verified":
        metrics.inc("ineligible_reasons", "not_verified")
        return {**base, "status": quote.get("status"), "eligible": False, "eligibility": "excluded",
                "reason": "Quotation terms are not verified; excluded from ranking.",
                "reasons": ["Quotation terms are not verified; excluded from ranking."],
                "reason_codes": ["not_verified"], "score": None}
    terms = _verified_terms(quote)
    if terms.get("currency") != rfq["currency"]:
        metrics.inc("ineligible_reasons", "currency_mismatch")
        return {**base, "status": "ineligible", "eligible": False, "eligibility": "ineligible",
                "currency": terms.get("currency"), "reason": "Currency mismatch.", "reasons": ["Currency mismatch."],
                "reason_codes": ["currency_mismatch"], "score": None}
    unit = Decimal(str(terms["unit_price"]))
    tax = Decimal(str(terms.get("tax_amount") or 0))
    fee = Decimal(str(terms.get("delivery_fee") or 0))
    other = Decimal(str(terms.get("other_fees") or 0))
    total = (unit * quantity + tax + fee + other).quantize(Decimal("0.01"))
    budget = Decimal(str(rfq["budget_limit"]))
    quote_delivery = _date(terms["delivery_date"])
    delivery_ok = quote_delivery <= required_delivery
    required_warranty = int(rfq.get("warranty_months_required") or 0)
    months_ok = int(terms.get("warranty_months") or 0) >= required_warranty
    scope_required = rfq.get("warranty_scope_required")
    scope_ok = (not scope_required) or bool(terms.get("warranty_scope_accepted"))
    warranty_ok = months_ok and scope_ok

    compliance = {k: _compliance_entry(v) for k, v in (terms.get("compliance") or {}).items()}
    gate: dict[str, str] = {}
    for req in rfq.get("compliance_requirements") or []:
        entry = compliance.get(req) or _compliance_entry(None)
        expires = _date(entry.get("expires_on"))
        if entry["status"] == "verified":
            gate[req] = "expired" if expires is not None and expires < required_delivery else "passed"
        elif entry["status"] == "declared":
            gate[req] = "review_required"
        else:
            gate[req] = "missing"
    missing = [r for r, g in gate.items() if g == "missing"]
    expired = [r for r, g in gate.items() if g == "expired"]
    review = [r for r, g in gate.items() if g == "review_required"]

    reasons, codes = [], []
    if not delivery_ok:
        reasons.append("Delivery after requested date.")
        codes.append("delivery_late")
    if not months_ok:
        reasons.append("Warranty below requirement.")
        codes.append("warranty_insufficient")
    if not scope_ok:
        reasons.append("Warranty scope not accepted by reviewer against the RFQ requirement.")
        codes.append("warranty_scope_not_accepted")
    if missing:
        reasons.append("Missing/failed compliance: " + ", ".join(missing))
        codes.append("compliance_missing")
    if expired:
        reasons.append("Compliance evidence expired before delivery: " + ", ".join(expired))
        codes.append("compliance_expired")
    if review:
        reasons.append("Compliance only declared, evidence review required: " + ", ".join(review))
        codes.append("compliance_review_required")
    if total > budget:
        reasons.append("Total cost exceeds RFQ budget.")
        codes.append("over_budget")
    for code in codes:
        metrics.inc("ineligible_reasons", code)
    eligible = not codes
    eligibility = "eligible" if eligible else (
        "review_required" if codes == ["compliance_review_required"] else "ineligible")
    return {
        **base, "status": "verified", "currency": rfq["currency"], "unit_price": str(unit),
        "quantity": str(quantity), "tax_amount": str(tax), "delivery_fee": str(fee), "other_fees": str(other),
        "total_cost": str(total), "within_budget": total <= budget,
        "delivery_date": quote_delivery.isoformat(), "delivery_compliant": delivery_ok,
        "warranty_months": int(terms.get("warranty_months") or 0), "warranty_scope": terms.get("warranty_scope"),
        "warranty_months_compliant": months_ok, "warranty_scope_compliant": scope_ok,
        "warranty_compliant": warranty_ok, "compliance": compliance, "compliance_gate": gate,
        "missing_compliance": missing, "expired_compliance": expired, "review_required_compliance": review,
        "eligible": eligible, "eligibility": eligibility, "reasons": reasons, "reason_codes": codes,
    }


def compare_quotes(rfq_id: str, actor: str, correlation_id: Optional[str] = None) -> dict:
    rfq = _load_rfq(rfq_id)
    if rfq is None:
        raise RFQNotFoundError("RFQ not found.")
    if rfq.get("status") == "cancelled":
        raise RFQConflictError("RFQ is cancelled; quotations cannot be compared.")
    try:
        quotes = [q for q in _db().vendor_quotes.find({"rfq_id": rfq_id, "superseded_by": None})
                  if q.get("status") != "superseded"]
    except PyMongoError as exc:
        raise _persistence("quote_list", "Database unavailable: quotations could not be listed.", exc) from exc
    quotes.sort(key=lambda q: q["quote_id"])
    quantity = Decimal(str(rfq["quantity"]))
    required_delivery = _date(rfq["delivery_date"])
    rows = [_evaluate(q, rfq, quantity, required_delivery) for q in quotes]
    eligible_rows = sorted((r for r in rows if r.get("eligible")),
                           key=lambda r: (Decimal(r["total_cost"]), r["delivery_date"], r["quote_id"]))
    for rank, row in enumerate(eligible_rows, start=1):
        row["rank"] = rank
    rows.sort(key=lambda r: (r.get("rank") is None, r.get("rank") or 0, r["quote_id"]))
    policy_version = policy_hash = scoring_error = None
    try:
        policy = scoring.load_policy()
        scoring.score_rows(rows, required_delivery, policy)
        policy_version, policy_hash = policy["version"], policy["policy_hash"]
    except scoring.ScoringPolicyError as exc:
        scoring_error = str(exc)
        for row in rows:
            row["score"] = None
    recommended = eligible_rows[0]["quote_id"] if eligible_rows else None
    now = _now()
    comparison_id = _id("RFQCMP")
    rfq_hash = _canonical_hash(_rfq_snapshot(rfq))
    result = {
        "comparison_id": comparison_id, "rfq_id": rfq_id, "rfq_status": rfq.get("status"),
        "currency": rfq["currency"], "budget_limit": str(rfq["budget_limit"]),
        "ranking_method": RANKING_METHOD, "eligible_count": len(eligible_rows),
        "recommended_quote_id": recommended, "quotes": rows,
        "scoring_policy_version": policy_version, "scoring_policy_hash": policy_hash,
        "scoring_error": scoring_error, "rfq_snapshot_hash": rfq_hash,
        "quote_snapshot_hashes": {r["quote_id"]: r["quote_snapshot_hash"] for r in rows},
        "award_decision": "human_review_required", "human_decision": "pending", "decision": None,
        "created_by": actor, "created_at": now, "notice": NOTICE_ADVISORY,
    }
    event = _make_event(rfq_id=rfq_id, entity_type="comparison", entity_id=comparison_id, seq=1,
                        event_type="quotes_compared", actor=actor, correlation_id=correlation_id,
                        to_status="pending",
                        details={"comparison_id": comparison_id, "quote_count": len(rows),
                                 "eligible_count": len(eligible_rows), "recommended_quote_id": recommended,
                                 "scoring_policy_version": policy_version, "rfq_snapshot_hash": rfq_hash})
    doc = dict(result, event_seq=1, pending_events=[event], updated_at=now)
    coll = _db().vendor_rfq_comparisons
    try:
        coll.insert_one(doc)
    except PyMongoError as exc:
        raise _persistence("comparison_create", "Database unavailable: comparison could not be saved.",
                           exc) from exc
    metrics.inc("comparisons", "with_recommendation" if recommended else "no_eligible_quote")
    _deliver(coll, "comparison_id", doc)
    return _safe(doc)


def list_comparisons(rfq_id: str, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    try:
        coll = _db().vendor_rfq_comparisons
        docs = list(coll.find({"rfq_id": rfq_id}).sort([("created_at", DESCENDING),
                                                        ("comparison_id", ASCENDING)]).limit(limit))
        return [_safe(_repair_doc(coll, "comparison_id", d)) for d in docs]
    except PyMongoError as exc:
        raise _persistence("comparison_list", "Database unavailable: comparisons could not be listed.",
                           exc) from exc


def decide_comparison(rfq_id: str, comparison_id: str, body, actor: str,
                      correlation_id: Optional[str] = None) -> dict:
    """Record the human selection. No award, PO or dispatch happens; an accept closes the RFQ if open."""
    db = _db()
    coll = db.vendor_rfq_comparisons
    try:
        rfq = _load_rfq(rfq_id)
        if rfq is None:
            raise RFQNotFoundError("RFQ not found.")
        if rfq.get("status") == "cancelled":
            raise RFQConflictError("RFQ is cancelled; comparison decisions cannot be recorded.")
        with _lock(coll):
            comparison = coll.find_one({"rfq_id": rfq_id, "comparison_id": comparison_id})
            if comparison is None:
                raise RFQNotFoundError("Comparison not found.")
            if comparison.get("human_decision") != "pending":
                raise RFQConflictError("A human decision has already been recorded for this comparison.")
            deviates = None
            if body.decision == "accept":
                row = next((r for r in comparison.get("quotes") or [] if r.get("quote_id") == body.quote_id), None)
                if row is None or not row.get("eligible"):
                    raise RFQConflictError("Only an eligible quotation from this comparison can be accepted.")
                current = db.vendor_quotes.find_one({"rfq_id": rfq_id, "quote_id": body.quote_id})
                if (current is None or current.get("superseded_by")
                        or _quote_snapshot_hash(current) != row.get("quote_snapshot_hash")):
                    raise RFQConflictError("Comparison is stale: the quotation changed; run a new comparison.")
                if coll.find_one({"rfq_id": rfq_id, "human_decision": "accepted"}, {"_id": 1}):
                    raise RFQConflictError("Another comparison for this RFQ has already been accepted.")
                deviates = body.quote_id != comparison.get("recommended_quote_id")
            to_state = "accepted" if body.decision == "accept" else "rejected"
            decision = {"decision": body.decision, "quote_id": body.quote_id if body.decision == "accept" else None,
                        "reason": body.reason, "decided_by": actor, "decided_at": _now(),
                        "deviates_from_recommendation": deviates,
                        "effect": "human selection recorded only; no award, PO or supplier contact"}
            try:
                updated = _commit(coll, "comparison_id", comparison, {"human_decision": "pending"},
                                  {"human_decision": to_state, "decision": decision},
                                  {"entity_type": "comparison", "event_type": f"comparison_{to_state}",
                                   "from_status": "pending", "to_status": to_state, "reason": body.reason,
                                   "details": {"comparison_id": comparison_id, "quote_id": decision["quote_id"],
                                               "deviates_from_recommendation": deviates}},
                                  actor, correlation_id)
            except DuplicateKeyError as exc:
                raise RFQConflictError("Another comparison for this RFQ has already been accepted.") from exc
        if updated is None:
            raise RFQConflictError("Comparison changed concurrently; reload and retry.")
    except (LookupError, ValueError):
        raise
    except PyMongoError as exc:
        raise _persistence("comparison_decision", "Database unavailable: decision could not be saved.",
                           exc) from exc
    metrics.inc("comparison_decisions", body.decision)
    _deliver(coll, "comparison_id", updated)
    rfq_status = rfq.get("status")
    if body.decision == "accept" and rfq_status == "open":
        try:
            rfq_status = close_rfq(rfq_id, f"Closed after human acceptance of comparison {comparison_id}.",
                                   actor, correlation_id)["status"]
        except RFQConflictError:
            rfq_status = (_load_rfq(rfq_id) or {}).get("status")
    out = _safe(updated)
    out["rfq_status"] = rfq_status
    return out
