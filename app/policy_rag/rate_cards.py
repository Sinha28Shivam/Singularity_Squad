"""Structured rate cards: extraction from rate-card PDFs and deterministic Decimal validation.

Extraction only proposes candidate rows (always ``needs_review``); a human approver/admin must verify a row
before it is treated as authoritative. Validation never invents taxes, surcharges or discounts: it compares
``quoted_unit_rate * quantity`` with ``verified unit_rate * quantity`` within the configured tolerance.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Optional

from app.policy_rag.embeddings import load_config
from app.policy_rag.errors import PolicyRagError
from app.policy_rag.schemas import RateCardEntry, RateQuote, RateValidationResult

logger = logging.getLogger(__name__)

_CENT = Decimal("0.01")
_AMOUNT = r"(?:₹|INR|Rs\.?)\s*(\d{1,3}(?:,\d{2,3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
_ROW_PRICE = re.compile(_AMOUNT + r"(?:\s*/\s*" + _AMOUNT + r")?(?:\s*(?:/|per)\s*([A-Za-z]+)\*?)?\s*\.?\s*$")
_ADDON_PRICE = re.compile(r"—\s*" + _AMOUNT + r"(?:\s+per\s+([A-Za-z]+))?")
_RECORD = re.compile(r"Record:\s*(VEN-\d{3})", re.IGNORECASE)
_TAX_EXCLUDED = re.compile(r"before\s+(?:applicable\s+)?taxes|taxes\s+extra", re.IGNORECASE)
_TAX_INCLUDED = re.compile(r"(?:inclusive\s+of|including)\s+(?:all\s+)?(?:applicable\s+)?taxes", re.IGNORECASE)
_TABLE_HEADER = re.compile(r"^(?:Package|Pass|Format|Deal|Illustrative item|Item|Service)\b.*"
                           r"(?:price|INR|rate)\s*$", re.IGNORECASE)
_ADDON_START = re.compile(r"\b(?:add-ons?|extras|enhancements)\b|^Optional\b", re.IGNORECASE)
_SECTION_END = re.compile(r"^(?:Sample\b|Safety note|Dietary|Singularity sample|Fictional sample|\*)",
                          re.IGNORECASE)
_UNIT_ALIASES = {"people": "person", "persons": "person", "pax": "person", "head": "person",
                 "events": "event", "groups": "group", "hours": "hour", "items": "item",
                 "sessions": "session", "servings": "serving", "each": "item", "unit": "item"}


def _field(obj: Any, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _date(value: Any) -> Optional[dt.date]:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


def parse_amount(text: str) -> Decimal:
    """'1,45,000' -> Decimal('145000.00') (Indian or western digit grouping)."""
    try:
        return Decimal(text.replace(",", "")).quantize(_CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("invalid amount") from exc


def slug(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    return s[:64] or "item"


def _unit(raw: Optional[str], default: str) -> str:
    if not raw:
        return default
    u = raw.lower().rstrip("*")
    return _UNIT_ALIASES.get(u, u)


def _is_name_word(word: str) -> bool:
    return bool(word) and (word[0].isupper() or word[0].isdigit() or word in {"/", "&"})


def _is_name_line(line: str) -> bool:
    words = line.split()
    return (1 <= len(words) <= 3 and "₹" not in line and not re.search(r"[.,;:—]$", line)
            and all(_is_name_word(w) for w in words))


def _is_addon_name(line: str) -> bool:
    words = line.split()
    return (1 <= len(words) <= 5 and line[0].isupper() and "₹" not in line
            and not re.search(r"[.,;:—\-]$", line))


def _leading_name(text: str) -> str:
    """Leading Title-Case words: 'Town Hall 100 Program timeline, ...' -> 'Town Hall 100'."""
    words = text.split()
    name: list[str] = []
    for i, w in enumerate(words):
        if not _is_name_word(w):
            break
        if re.search(r"[,.;:]$", w):
            break
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if len(name) >= 2 and nxt and nxt[0].islower() and w not in {"/", "&"}:
            break
        name.append(w)
    return " ".join(name).strip(" /&")


def _vendor_name(lines: list[str]) -> Optional[str]:
    for i, line in enumerate(lines[:8]):
        if line.lower().startswith("illustrative") and i >= 1:
            parts = []
            j = i - 1
            while j >= 0 and not (line_is_upper(lines[j])) and len(parts) < 3:
                parts.insert(0, lines[j])
                j -= 1
            name = " ".join(parts).strip()
            return name[:120] or None
    return None


def line_is_upper(line: str) -> bool:
    letters = [c for c in line if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def _rate_id(document_id: str, content_hash: str, page: int, code: str, n: int) -> str:
    raw = f"{document_id}:{content_hash}:{page}:{code}:{n}"
    return "rate_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def parse_page(text: str) -> dict:
    """Parse one rate-card page into {record, vendor_name, tax_treatment, rows:[...]} (no persistence)."""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    record = None
    m = _RECORD.search(text or "")
    if m:
        record = m.group(1).upper()
    if _TAX_EXCLUDED.search(text or ""):
        tax = "excluded"
    elif _TAX_INCLUDED.search(text or ""):
        tax = "included"
    else:
        tax = "not_verified"
    per_serving = bool(re.search(r"per\s+serving", text or "", re.IGNORECASE))
    default_unit = "serving" if per_serving else "item"
    has_header = any(_TABLE_HEADER.match(ln) for ln in lines)

    rows: list[dict] = []
    mode = None if has_header else "table"
    buffer: list[str] = []
    addon: Optional[dict] = None

    def flush_addon():
        nonlocal addon
        if addon and addon.get("text"):
            joined = " ".join(addon["text"])
            pm = _ADDON_PRICE.search(joined)
            if pm and addon.get("name"):
                rows.append({"name": addon["name"], "amount": pm.group(1), "unit": _unit(pm.group(2), "item"),
                             "same_line": False, "kind": "add_on", "description": joined[:300]})
        addon = None

    for line in lines:
        if _TABLE_HEADER.match(line):
            flush_addon()
            mode, buffer = "table", []
            continue
        if _ADDON_START.search(line) and "₹" not in line and len(line.split()) <= 6:
            flush_addon()
            mode, buffer = "addon", []
            continue
        if mode is not None and _SECTION_END.match(line) and "₹" not in line:
            flush_addon()
            mode, buffer = None, []
            continue
        if mode == "table":
            pm = _ROW_PRICE.search(line)
            if pm:
                head = line[:pm.start()].strip()
                same_line = bool(head) and not buffer
                name_lines: list[str] = []
                rest = list(buffer) + ([head] if head else [])
                while rest and _is_name_line(rest[0]) and len(name_lines) < 3:
                    name_lines.append(rest.pop(0))
                if name_lines:
                    name = " ".join(name_lines)
                    if rest and not name_lines[-1][-1:].isdigit():
                        # a numeric suffix on the next row text belongs to the name ("Annual Celebration" "300")
                        pass
                else:
                    name = _leading_name(rest[0]) if rest else ""
                description = " ".join(name_lines + rest).strip()
                buffer = []
                if not name:
                    continue
                amounts = [pm.group(1)] + ([pm.group(2)] if pm.group(2) else [])
                unit = _unit(pm.group(3), default_unit)
                if len(amounts) == 2 and name.count("/") == 1:
                    for part, amt in zip([p.strip() for p in name.split("/")], amounts):
                        rows.append({"name": part, "amount": amt, "unit": unit, "same_line": False,
                                     "kind": "package", "description": description[:300]})
                    continue
                rows.append({"name": name, "amount": amounts[0], "unit": unit,
                             "same_line": same_line and len(amounts) == 1, "kind": "package",
                             "description": description[:300]})
            else:
                buffer.append(line)
        elif mode == "addon":
            starts_block = _is_addon_name(line) and (addon is None or bool(_ADDON_PRICE.search(
                " ".join(addon.get("text") or []))))
            if starts_block:
                flush_addon()
                addon = {"name": line, "text": []}
            elif addon is not None:
                addon["text"].append(line)
    flush_addon()
    return {"record": record, "vendor_name": _vendor_name(lines), "tax_treatment": tax, "rows": rows}


def build_entries(document: Any, pages: list) -> list[RateCardEntry]:
    """Pure extraction (no persistence)."""
    cfg = load_config()
    aliases: dict = cfg.get("vendor_key_aliases") or {}
    document_id = str(_field(document, "document_id"))
    content_hash = str(_field(document, "content_hash"))
    doc_vendor = _field(document, "vendor_key")
    eff_from, eff_to = _date(_field(document, "effective_from")), _date(_field(document, "effective_to"))
    entries: list[RateCardEntry] = []
    for page in pages:
        page_number = int(_field(page, "page_number"))
        parsed = parse_page(_field(page, "text") or "")
        vendor_key = aliases.get(parsed["record"]) if parsed["record"] else None
        vendor_key = vendor_key or doc_vendor
        if not vendor_key:
            continue
        seen: dict[str, int] = {}
        for row in parsed["rows"]:
            try:
                amount = parse_amount(row["amount"])
            except ValueError:
                continue
            code = slug(row["name"])
            if row["kind"] == "add_on":
                code = f"addon_{code}"[:64]
            n = seen.get(code, 0)
            seen[code] = n + 1
            entries.append(RateCardEntry(
                rate_id=_rate_id(document_id, content_hash, page_number, code, n),
                document_id=document_id, content_hash=content_hash, vendor_key=vendor_key,
                vendor_name=parsed["vendor_name"], item_or_service_code=code,
                description=(row["name"] if not row.get("description") else
                             f"{row['name']} — {row['description']}")[:400],
                unit_rate=str(amount), currency="INR", unit=row["unit"],
                tax_treatment=parsed["tax_treatment"], effective_from=eff_from, effective_to=eff_to,
                source_page=page_number, verification_status="needs_review",
                extraction_confidence="high" if row.get("same_line") else "low"))
    return entries


def _event(event_type: str, actor: str, **fields) -> None:
    try:
        from app.policy_rag import events
        events.record(event_type, actor, **fields)
    except Exception as exc:  # noqa: BLE001 - event log is best effort
        logger.warning("Policy event %s not recorded (%s)", event_type, type(exc).__name__)


def extract_rate_entries(document: Any, pages: list) -> list[RateCardEntry]:
    """Extract and persist candidate rate rows (``needs_review``). Idempotent: deterministic rate_ids,
    existing rows (and their verification decisions) are kept."""
    from app.repositories import rate_card_repository as repo

    entries = build_entries(document, pages)
    if entries:
        repo.insert_entries([e.model_dump(mode="json") for e in entries])
    return entries  # the ingestion pipeline records rate_card_extraction_created


def verify_entry(rate_id: str, actor, approve: bool) -> dict:
    """Approver/admin verifies or rejects a ``needs_review`` row (409 if already decided)."""
    from app.repositories import rate_card_repository as repo

    if getattr(actor, "role", None) not in ("approver", "admin"):
        raise PolicyRagError(403, "Approver or admin access required.")
    existing = repo.get_entry(rate_id)
    if existing is None:
        raise PolicyRagError(404, "Rate-card entry not found.")
    status = "verified" if approve else "rejected"
    updated = repo.set_verification(rate_id, status, actor.username, dt.datetime.now(dt.timezone.utc))
    if updated is None:
        raise PolicyRagError(409, "This rate-card entry has already been reviewed.")
    _event("rate_card_verified", actor.username, rate_id=rate_id, decision=status,
           document_id=updated.get("document_id"), actor_role=actor.role)
    return updated


# ---------------------------------------------------------------------------------------------
# Deterministic validation
# ---------------------------------------------------------------------------------------------
def _ready_document_versions(document_ids: set[str]) -> dict[str, dict]:
    """document_id -> document for documents that are currently usable (status ready)."""
    if not document_ids:
        return {}
    from app.repositories import policy_document_repository as docs_repo

    out = {}
    for doc in docs_repo.list_documents(statuses=["ready"]):
        if doc.get("document_id") in document_ids:
            out[doc["document_id"]] = doc
    return out


def _valid_on(entry: dict, as_of: dt.date) -> bool:
    start, end = _date(entry.get("effective_from")), _date(entry.get("effective_to"))
    return (start is None or start <= as_of) and (end is None or as_of <= end)


def _fmt(d: Decimal) -> str:
    return format(d.quantize(_CENT, rounding=ROUND_HALF_UP), "f")


def validate_quote(quote: RateQuote, *, entries: Optional[list[dict]] = None,
                   documents: Optional[dict[str, dict]] = None) -> RateValidationResult:
    """Validate a quoted unit rate against structured rate rows (exact vendor_key + item code)."""
    cfg = load_config()
    tolerance = Decimal(str(cfg.get("rate_tolerance_inr") or "0"))
    require_verified = bool(cfg.get("require_verified_rate_cards", True))
    try:
        quoted = parse_amount(quote.quoted_unit_rate)
    except ValueError:
        raise PolicyRagError(422, "quoted_unit_rate must be a decimal amount.") from None
    if quoted < 0:
        raise PolicyRagError(422, "quoted_unit_rate must be non-negative.")
    qty = Decimal(quote.quantity)
    quoted_total = (quoted * qty).quantize(_CENT, rounding=ROUND_HALF_UP)
    base = {"quoted_unit_rate": _fmt(quoted), "currency": quote.currency, "quantity": quote.quantity,
            "quoted_total": _fmt(quoted_total)}

    if entries is None:
        from app.repositories import rate_card_repository as repo
        entries = repo.list_entries(vendor_key=quote.vendor_key, item_or_service_code=quote.item_or_service_code)
    entries = [e for e in entries if e.get("vendor_key") == quote.vendor_key
               and e.get("item_or_service_code") == quote.item_or_service_code
               and e.get("verification_status") != "rejected"]
    if documents is None:
        documents = _ready_document_versions({e.get("document_id") for e in entries})
    entries = [e for e in entries if e.get("document_id") in documents]
    if not entries:
        return RateValidationResult(**base, validation_status="unknown",
                                    reason="No rate-card entry exists for this vendor and item code.")

    current = [e for e in entries if _valid_on(e, quote.as_of)]
    if not current:
        return RateValidationResult(**base, validation_status="needs_review",
                                    candidate_rate_ids=[e["rate_id"] for e in entries],
                                    reason="Rate-card entries exist but none is effective on the request date.")
    same_currency = [e for e in current if (e.get("currency") or "INR") == quote.currency]
    if not same_currency:
        return RateValidationResult(**base, validation_status="needs_review",
                                    candidate_rate_ids=[e["rate_id"] for e in current],
                                    reason="Currency mismatch between the quote and the rate card.")
    candidates = same_currency
    if quote.unit:
        unit = _unit(quote.unit, quote.unit)
        candidates = [e for e in candidates if e.get("unit") == unit]
        if not candidates:
            return RateValidationResult(**base, validation_status="needs_review",
                                        candidate_rate_ids=[e["rate_id"] for e in same_currency],
                                        reason="Unit mismatch between the quote and the rate card.")
    in_bounds = [e for e in candidates
                 if (e.get("minimum_quantity") is None or quote.quantity >= int(e["minimum_quantity"]))
                 and (e.get("maximum_quantity") is None or quote.quantity <= int(e["maximum_quantity"]))]
    if not in_bounds:
        return RateValidationResult(**base, validation_status="needs_review",
                                    candidate_rate_ids=[e["rate_id"] for e in candidates],
                                    reason="Quantity is outside the rate-card quantity bounds.")

    verified = [e for e in in_bounds if e.get("verification_status") == "verified"]
    ids = [e["rate_id"] for e in in_bounds]
    if len(verified) > 1:
        return RateValidationResult(**base, validation_status="conflict", candidate_rate_ids=ids,
                                    reason="Multiple verified rate-card entries apply; precedence is not "
                                           "configured, so none was chosen.")
    if not verified:
        if require_verified or len(in_bounds) != 1:
            single = in_bounds[0] if len(in_bounds) == 1 else None
            extra = {}
            if single:
                rate = Decimal(single["unit_rate"])
                extra = {"expected_unit_rate": _fmt(rate), "expected_total": _fmt(rate * qty),
                         "rate_id": single["rate_id"], "source_document_id": single["document_id"],
                         "source_page": single.get("source_page"),
                         "rate_card_version": (documents.get(single["document_id"]) or {}).get("version_label")}
            return RateValidationResult(**base, **extra, validation_status="needs_review", candidate_rate_ids=ids,
                                        reason="The applicable rate-card entry is not verified yet."
                                        if single else "Several unverified rate-card entries could apply.")
        verified = in_bounds

    entry = verified[0]
    rate = Decimal(entry["unit_rate"])
    expected_total = (rate * qty).quantize(_CENT, rounding=ROUND_HALF_UP)
    diff = quoted_total - expected_total
    pct = _fmt(diff / expected_total * 100) if expected_total > 0 else None
    if abs(diff) <= tolerance:
        status, reason = "pass", "Quoted total matches the verified rate card within tolerance."
    elif diff < 0:
        status, reason = "pass", "Quoted total is below the verified rate card."
    else:
        status, reason = "fail", "Quoted total exceeds the verified rate card beyond tolerance."
    return RateValidationResult(
        **base, expected_unit_rate=_fmt(rate), expected_total=_fmt(expected_total),
        difference_amount=_fmt(diff), difference_percentage=pct, rate_id=entry["rate_id"],
        candidate_rate_ids=ids, source_document_id=entry["document_id"], source_page=entry.get("source_page"),
        rate_card_version=(documents.get(entry["document_id"]) or {}).get("version_label"),
        validation_status=status, reason=reason)


def overlapping_verified_conflicts(vendor_key: str, as_of: dt.date, *,
                                   entries: Optional[list[dict]] = None,
                                   documents: Optional[dict[str, dict]] = None) -> list[dict]:
    """Groups of verified, currently effective entries for the same vendor/item with different rates."""
    if entries is None:
        from app.repositories import rate_card_repository as repo
        entries = repo.list_entries(vendor_key=vendor_key, verification_status="verified")
    entries = [e for e in entries if e.get("verification_status") == "verified" and _valid_on(e, as_of)]
    if documents is None:
        documents = _ready_document_versions({e.get("document_id") for e in entries})
    entries = [e for e in entries if e.get("document_id") in documents]
    groups: dict[str, list[dict]] = {}
    for e in entries:
        groups.setdefault(e["item_or_service_code"], []).append(e)
    out = []
    for code, rows in sorted(groups.items()):
        if len({Decimal(r["unit_rate"]) for r in rows}) > 1:
            out.append({"item_or_service_code": code, "rate_ids": sorted(r["rate_id"] for r in rows)})
    return out
