"""RFQ lifecycle, quote intake and deterministic comparison.

No outbound supplier contact, contract award, PO dispatch, or payment is performed here.
PDF text is untrusted and remains ineligible for comparison until a human verifies its terms.
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import uuid4

from pypdf import PdfReader
from pymongo import ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.data_loader import load_json
from app.db.errors import PersistenceError
from app.db.mongo import get_database
from app.vendor_sourcing import repository
from app.repositories.ticket_repository import get_ticket
from app.schemas import EventRequest


MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 20
MAX_EXTRACTED_CHARS = 100_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _id(prefix: str) -> str:
    return prefix + "-" + uuid4().hex[:12].upper()


def _safe(doc: Optional[dict]) -> Optional[dict]:
    if doc is None:
        return None
    doc.pop("_id", None)
    # Original PDF bytes are never returned by API responses.
    doc.pop("pdf_bytes", None)
    return doc


def ensure_indexes() -> None:
    try:
        db = get_database()
        db.vendor_rfqs.create_index([("rfq_id", ASCENDING)], unique=True, name="vendor_rfq_id_unique")
        db.vendor_rfqs.create_index([("request_id", ASCENDING), ("created_at", DESCENDING)],
                                    name="vendor_rfq_request_created")
        db.vendor_quotes.create_index([("quote_id", ASCENDING)], unique=True, name="vendor_quote_id_unique")
        db.vendor_quotes.create_index([("rfq_id", ASCENDING), ("vendor_id", ASCENDING)], unique=True,
                                      name="vendor_quote_rfq_vendor_unique")
        db.vendor_quotes.create_index([("rfq_id", ASCENDING), ("submitted_at", DESCENDING)],
                                      name="vendor_quote_rfq_submitted")
        db.vendor_rfq_events.create_index([("event_id", ASCENDING)], unique=True, name="vendor_rfq_event_unique")
        db.vendor_rfq_events.create_index([("rfq_id", ASCENDING), ("created_at", ASCENDING)],
                                          name="vendor_rfq_event_time")
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: RFQ indexes could not be initialized.") from exc


def _event(rfq_id: str, event_type: str, actor: str, details: dict) -> None:
    event = {"event_id": _id("RFQ-EVT"), "rfq_id": rfq_id, "event_type": event_type,
             "actor": actor, "details": details, "created_at": _now()}
    try:
        get_database().vendor_rfq_events.insert_one(event)
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: RFQ audit event could not be recorded.") from exc


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
        raise ValueError("RFQ must reference an existing persisted workflow ticket.")
    result = ticket.get("workflow_result") or {}
    if result.get("final_status") not in {"pass", "warning"}:
        raise ValueError("RFQ source ticket must have a completed pass/warning workflow result.")
    if result.get("missing_fields"):
        raise ValueError("RFQ source ticket still has missing required fields.")
    raw_request = result.get("request")
    if not isinstance(raw_request, dict):
        raise ValueError("RFQ source ticket has no validated request snapshot.")
    try:
        source = EventRequest.model_validate(raw_request)
    except Exception as exc:
        raise ValueError("RFQ source ticket request snapshot is invalid.") from exc
    if source.date is None or source.attendees is None or source.budget_limit is None or not source.service:
        raise ValueError("RFQ source ticket is missing required purchase requirements.")
    if source.service.casefold() != body.service.casefold():
        raise ValueError("RFQ service must match the validated source ticket.")
    if Decimal(str(source.attendees)) != body.quantity:
        raise ValueError("RFQ quantity must match the validated attendee/quantity count.")
    if Decimal(str(source.budget_limit)) != body.budget_limit:
        raise ValueError("RFQ budget must match the validated source ticket.")
    if source.date != body.delivery_date:
        raise ValueError("RFQ delivery date must match the validated source ticket.")
    if source.currency.upper() != body.currency:
        raise ValueError("RFQ currency must match the validated source ticket.")
    if source.location and source.location.casefold() != body.delivery_location.casefold():
        raise ValueError("RFQ delivery location must match the validated source ticket.")


def create_rfq(body, actor: str) -> dict:
    _validate_source_ticket(body)
    if body.delivery_date < _now().date():
        raise ValueError("Delivery date must be today or in the future.")
    if body.response_deadline <= _now():
        raise ValueError("Response deadline must be in the future.")
    if body.response_deadline.date() > body.delivery_date:
        raise ValueError("Response deadline cannot be after the requested delivery date.")
    if len(set(body.candidate_ids)) != len(body.candidate_ids):
        raise ValueError("candidate_ids must not contain duplicates.")
    ineligible = [vendor_id for vendor_id in body.candidate_ids if not _vendor_is_eligible(vendor_id)]
    if ineligible:
        raise ValueError("RFQ recipients must be approved internal vendors or approved candidates: "
                         + ", ".join(ineligible))
    now = _now()
    doc = body.model_dump(mode="python")
    doc["quantity"] = str(body.quantity)
    doc["budget_limit"] = str(body.budget_limit)
    doc["delivery_date"] = body.delivery_date.isoformat()
    doc.update({
        "rfq_id": _id("RFQ"), "status": "open", "created_by": actor,
        "created_at": now, "updated_at": now, "quotes_received": 0,
        "requirements_snapshot_hash": hashlib.sha256(
            repr(sorted((k, str(v)) for k, v in body.model_dump().items())).encode("utf-8")
        ).hexdigest(),
    })
    try:
        get_database().vendor_rfqs.insert_one(doc)
        _event(doc["rfq_id"], "rfq_created", actor, {
            "request_id": doc["request_id"], "candidate_count": len(doc["candidate_ids"]),
            "requirements_snapshot_hash": doc["requirements_snapshot_hash"],
        })
        return _safe(doc)
    except DuplicateKeyError as exc:
        raise ValueError("RFQ already exists.") from exc
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: RFQ could not be saved.") from exc


def get_rfq(rfq_id: str) -> Optional[dict]:
    try:
        return _safe(get_database().vendor_rfqs.find_one({"rfq_id": rfq_id}))
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: RFQ could not be read.") from exc


def list_quotes(rfq_id: str) -> list[dict]:
    try:
        return [_safe(q) for q in get_database().vendor_quotes.find({"rfq_id": rfq_id}).sort(
            [("submitted_at", DESCENDING), ("quote_id", ASCENDING)])]
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: quotations could not be listed.") from exc


def _insert_quote(doc: dict, actor: str) -> dict:
    try:
        get_database().vendor_quotes.insert_one(doc)
        _event(doc["rfq_id"], "quote_submitted", actor,
               {"quote_id": doc["quote_id"], "vendor_id": doc["vendor_id"],
                "submission_type": doc["submission_type"], "status": doc["status"]})
        get_database().vendor_rfqs.update_one({"rfq_id": doc["rfq_id"]}, {"$inc": {"quotes_received": 1},
                                                                         "$set": {"updated_at": _now()}})
        return _safe(doc)
    except DuplicateKeyError as exc:
        raise ValueError("A quotation from this vendor has already been submitted for this RFQ.") from exc
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: quotation could not be saved.") from exc


def submit_structured_quote(rfq_id: str, body, actor: str) -> dict:
    rfq = get_rfq(rfq_id)
    if rfq is None:
        raise LookupError("RFQ not found.")
    if rfq["status"] != "open":
        raise ValueError("RFQ is not open for quotations.")
    if rfq["response_deadline"] <= _now():
        raise ValueError("RFQ response deadline has passed.")
    if body.vendor_id not in rfq["candidate_ids"]:
        raise ValueError("Vendor is not an invited RFQ recipient.")
    if not _vendor_is_eligible(body.vendor_id):
        raise ValueError("Vendor is not currently approved.")
    now = _now()
    doc = body.model_dump(mode="python")
    for money_field in ("unit_price", "tax_amount", "delivery_fee", "other_fees"):
        doc[money_field] = str(doc[money_field])
    doc["delivery_date"] = body.delivery_date.isoformat()
    doc.update({
        "quote_id": _id("QUOTE"), "rfq_id": rfq_id, "submission_type": "structured",
        "status": "pending_verification", "currency": body.currency, "quantity": rfq["quantity"],
        "submitted_by": actor, "submitted_at": now, "verified_at": None, "verified_by": None,
        "reviewer_note": None, "pdf_filename": None, "pdf_sha256": None,
        "extracted_text": None, "pdf_bytes": None,
    })
    return _insert_quote(doc, actor)


def submit_pdf_quote(rfq_id: str, vendor_id: str, quote_reference: str, filename: str,
                     pdf_bytes: bytes, actor: str) -> dict:
    rfq = get_rfq(rfq_id)
    if rfq is None:
        raise LookupError("RFQ not found.")
    if rfq["status"] != "open":
        raise ValueError("RFQ is not open for quotations.")
    if rfq["response_deadline"] <= _now():
        raise ValueError("RFQ response deadline has passed.")
    if vendor_id not in rfq["candidate_ids"] or not _vendor_is_eligible(vendor_id):
        raise ValueError("Vendor is not an approved invited RFQ recipient.")
    if not pdf_bytes or len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError("PDF must be non-empty and no larger than 5 MiB.")
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("Uploaded file is not a valid PDF signature.")
    safe_filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename or "quotation.pdf")[:120]
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=True)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs are not accepted.")
        if len(reader.pages) < 1 or len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("PDF must contain between 1 and 20 pages.")
        extracted = "\n".join((page.extract_text() or "") for page in reader.pages)[:MAX_EXTRACTED_CHARS]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF could not be parsed safely.") from exc
    now = _now()
    doc = {
        "quote_id": _id("QUOTE"), "rfq_id": rfq_id, "vendor_id": vendor_id,
        "quote_reference": quote_reference[:120], "submission_type": "pdf",
        "status": "pending_verification", "currency": None, "quantity": rfq["quantity"],
        "pdf_filename": safe_filename, "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "pdf_bytes": pdf_bytes, "extracted_text": extracted, "unit_price": None,
        "tax_amount": None, "delivery_fee": None, "other_fees": None, "delivery_date": None,
        "warranty_months": None, "warranty_scope": None, "compliance": {},
        "notes": "PDF terms are unverified; human verification is required before comparison.",
        "submitted_by": actor, "submitted_at": now, "verified_at": None,
        "verified_by": None, "reviewer_note": None,
    }
    return _insert_quote(doc, actor)


def verify_quote(rfq_id: str, quote_id: str, body, actor: str) -> dict:
    try:
        coll = get_database().vendor_quotes
        quote = coll.find_one({"rfq_id": rfq_id, "quote_id": quote_id})
        if quote is None:
            raise LookupError("Quotation not found.")
        if quote.get("status") != "pending_verification":
            raise ValueError("Only quotations awaiting verification can be verified.")
        rfq = get_rfq(rfq_id)
        if rfq is None:
            raise LookupError("RFQ not found.")
        now = _now()
        fields = body.model_dump(mode="python")
        for money_field in ("unit_price", "tax_amount", "delivery_fee", "other_fees"):
            fields[money_field] = str(fields[money_field])
        fields["delivery_date"] = body.delivery_date.isoformat()
        fields.update({"status": "verified", "verified_by": actor, "verified_at": now,
                       "reviewer_note": body.reviewer_note})
        updated = coll.find_one_and_update({"rfq_id": rfq_id, "quote_id": quote_id,
                                            "status": "pending_verification"},
                                           {"$set": fields}, return_document=True)
        if updated is None:
            raise ValueError("Quotation changed concurrently; reload and retry.")
        _event(rfq_id, "quote_verified", actor,
               {"quote_id": quote_id, "vendor_id": quote["vendor_id"], "note": body.reviewer_note})
        return _safe(updated)
    except (LookupError, ValueError):
        raise
    except PyMongoError as exc:
        raise PersistenceError("Database unavailable: quotation verification could not be saved.") from exc


def compare_quotes(rfq_id: str, actor: str) -> dict:
    rfq = get_rfq(rfq_id)
    if rfq is None:
        raise LookupError("RFQ not found.")
    quotes = list_quotes(rfq_id)
    required_compliance = rfq.get("compliance_requirements") or []
    required_warranty = int(rfq.get("warranty_months_required") or 0)
    quantity = Decimal(str(rfq["quantity"]))
    rows = []
    for quote in quotes:
        if quote.get("status") != "verified":
            rows.append({"quote_id": quote["quote_id"], "vendor_id": quote["vendor_id"],
                         "status": quote.get("status"), "eligible": False,
                         "reason": "Quotation terms are not verified; excluded from ranking."})
            continue
        if quote.get("currency") != rfq["currency"]:
            rows.append({"quote_id": quote["quote_id"], "vendor_id": quote["vendor_id"],
                         "status": "ineligible", "eligible": False, "reason": "Currency mismatch."})
            continue
        total = (Decimal(str(quote["unit_price"])) * quantity
                 + Decimal(str(quote.get("tax_amount") or 0))
                 + Decimal(str(quote.get("delivery_fee") or 0))
                 + Decimal(str(quote.get("other_fees") or 0))
        )
        from datetime import date as date_type
        quote_delivery = date_type.fromisoformat(str(quote["delivery_date"])[:10])
        required_delivery = date_type.fromisoformat(str(rfq["delivery_date"])[:10])
        delivery_ok = quote_delivery <= required_delivery
        warranty_ok = int(quote["warranty_months"]) >= required_warranty
        compliance = quote.get("compliance") or {}
        missing_compliance = [req for req in required_compliance if compliance.get(req) is not True]
        eligible = delivery_ok and warranty_ok and not missing_compliance and total <= Decimal(str(rfq["budget_limit"]))
        reasons = []
        if not delivery_ok: reasons.append("Delivery after requested date.")
        if not warranty_ok: reasons.append("Warranty below requirement.")
        if missing_compliance: reasons.append("Missing/failed compliance: " + ", ".join(missing_compliance))
        if total > Decimal(str(rfq["budget_limit"])): reasons.append("Total cost exceeds RFQ budget.")
        rows.append({
            "quote_id": quote["quote_id"], "vendor_id": quote["vendor_id"], "status": "verified",
            "currency": rfq["currency"], "unit_price": str(quote["unit_price"]),
            "quantity": str(quantity), "tax_amount": str(quote.get("tax_amount") or 0),
            "delivery_fee": str(quote.get("delivery_fee") or 0), "other_fees": str(quote.get("other_fees") or 0),
            "total_cost": str(total.quantize(Decimal("0.01"))), "delivery_date": quote_delivery.isoformat(),
            "delivery_compliant": delivery_ok, "warranty_months": quote["warranty_months"],
            "warranty_compliant": warranty_ok, "compliance": compliance,
            "missing_compliance": missing_compliance, "eligible": eligible,
            "reasons": reasons,
        })
    eligible_rows = [r for r in rows if r.get("eligible")]
    eligible_rows.sort(key=lambda r: (Decimal(r["total_cost"]), r["delivery_date"], r["quote_id"]))
    for rank, row in enumerate(eligible_rows, start=1):
        row["rank"] = rank
    _event(rfq_id, "quotes_compared", actor, {"quote_count": len(rows), "eligible_count": len(eligible_rows)})
    return {
        "rfq_id": rfq_id, "currency": rfq["currency"], "budget_limit": str(rfq["budget_limit"]),
        "ranking_method": "Eligible quotes only; lowest total cost, then earliest delivery, then quote ID.",
        "eligible_count": len(eligible_rows), "recommended_quote_id": eligible_rows[0]["quote_id"] if eligible_rows else None,
        "quotes": rows, "award_decision": "human_review_required",
        "notice": "Comparison is advisory. No vendor is awarded and no purchase order is created or sent.",
    }
