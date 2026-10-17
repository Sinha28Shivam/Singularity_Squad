"""Typed, read-only fact registry for the Phase 3 rule engine.

Rules may only reference the fact names declared in ``FACT_TYPES``. Facts are derived from safe, structured
ticket/workflow data (submission, normalized request, selected vendor, draft PO, workflow statuses, rate-card
validations, retrieval metadata). They never contain secrets, tokens, raw database documents, file paths or
raw policy text.

Value conventions (all JSON-safe, deterministic):
- integer -> ``int``; boolean -> ``bool``; string -> ``str``
- money / decimal -> Decimal *string* (e.g. ``"52500.00"``); never floats. Money facts carry a currency in
  ``FactSnapshot.currencies``.
- date -> ISO ``YYYY-MM-DD`` string (UTC calendar-date convention)
- string_list -> sorted list of unique strings
A fact whose source value is absent is listed in ``missing`` and has no entry in ``values``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

FACT_TYPES: dict[str, str] = {
    "request.attendees": "integer",
    "request.budget_limit": "money",
    "request.service": "string",
    "request.location": "string",
    "request.event_date": "date",
    "request.event_category": "string",
    "request.currency": "string",
    "request.free_text_terms": "string_list",
    "request.approved_vendor_only": "boolean",
    "vendor.selected": "boolean",
    "vendor.vendor_id": "string",
    "vendor.vendor_key": "string",
    "vendor.approved": "boolean",
    "po.total": "money",
    "po.subtotal": "money",
    "po.tax": "money",
    "po.currency": "string",
    "po.line_count": "integer",
    "workflow.final_status": "string",
    "workflow.reconciliation_status": "string",
    "rate.validation_statuses": "string_list",
    "rate.any_fail": "boolean",
    "rate.any_unverified": "boolean",
    "rate.applicable": "boolean",
    "policy.retrieval_mode": "string",
    "policy.insufficient_evidence": "boolean",
}

# Declared (registry) currency of each money fact; rules on money facts must use this currency.
FACT_CURRENCIES: dict[str, str] = {
    "request.budget_limit": "INR",
    "po.total": "INR",
    "po.subtotal": "INR",
    "po.tax": "INR",
}

FACT_DESCRIPTIONS: dict[str, str] = {
    "request.attendees": "Number of attendees (normalized request, else submission).",
    "request.budget_limit": "Budget limit of the request.",
    "request.service": "Requested service (e.g. lunch).",
    "request.location": "Event location text.",
    "request.event_date": "Event date (ISO date).",
    "request.event_category": "Submission event category.",
    "request.currency": "Request currency.",
    "request.free_text_terms": "Sorted unique lowercase word tokens of the submission title and notes.",
    "request.approved_vendor_only": "Requester asked for approved vendors only.",
    "vendor.selected": "A vendor was selected / a draft PO names a vendor.",
    "vendor.vendor_id": "Selected vendor id.",
    "vendor.vendor_key": "Policy-library vendor key of the selected vendor.",
    "vendor.approved": "Selected vendor is on the approved list.",
    "po.total": "Draft PO total.",
    "po.subtotal": "Draft PO subtotal.",
    "po.tax": "Draft PO tax.",
    "po.currency": "Draft PO currency.",
    "po.line_count": "Number of draft PO line items.",
    "workflow.final_status": "Workflow final status.",
    "workflow.reconciliation_status": "Invoice reconciliation status.",
    "rate.validation_statuses": "Sorted unique rate-card validation statuses of applicable PO lines.",
    "rate.any_fail": "Any applicable PO line failed rate-card validation.",
    "rate.any_unverified": "Any applicable PO line could not be verified (needs_review/unknown/conflict).",
    "rate.applicable": "At least one PO line has applicable rate-card validation.",
    "policy.retrieval_mode": "Policy retrieval mode (hybrid / hybrid_local_hash / keyword_only_degraded).",
    "policy.insufficient_evidence": "Policy retrieval found insufficient evidence.",
}

UNVERIFIED_RATE_STATUSES = frozenset({"needs_review", "unknown", "conflict"})
_TOKEN = re.compile(r"[a-z0-9]+")
_MAX_TERMS = 500
_MAX_STR = 200


@dataclass(frozen=True)
class FactSnapshot:
    """Immutable typed fact snapshot. ``hash`` = sha256 of the canonical JSON of the snapshot content."""
    values: dict[str, Any]
    types: dict[str, str]
    missing: frozenset[str]
    currencies: dict[str, str] = field(default_factory=dict)
    hash: str = ""

    def has(self, name: str) -> bool:
        return name in self.values

    def get(self, name: str, default: Any = None) -> Any:
        return self.values.get(name, default)

    def subset(self, names) -> dict[str, Any]:
        """Values of the named facts that are present (used for ``facts_used`` in evaluations)."""
        return {n: self.values[n] for n in sorted(set(names)) if n in self.values}

    def to_dict(self) -> dict[str, Any]:
        return {"values": dict(self.values), "types": dict(self.types), "missing": sorted(self.missing),
                "currencies": dict(self.currencies), "hash": self.hash}


def _canonical(obj: Any) -> Any:
    if hasattr(obj, "model_dump"):
        return _canonical(obj.model_dump(mode="json", by_alias=True))
    if isinstance(obj, dict):
        return {str(k): _canonical(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return sorted(_canonical(v) for v in obj)
    if isinstance(obj, (list, tuple)):
        return [_canonical(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    if isinstance(obj, float):
        return str(Decimal(repr(obj)))
    return obj


def canonical_json(obj: Any) -> str:
    return json.dumps(_canonical(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(obj: Any) -> str:
    """sha256 hex of the canonical JSON (sorted keys, no whitespace, Decimal/date as strings)."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _d(v: Any) -> dict:
    if hasattr(v, "model_dump"):
        v = v.model_dump(mode="json")
    return v if isinstance(v, dict) else {}


def _first(*vals):
    for v in vals:
        if v is not None and v != "":
            return v
    return None


def _int(v) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    if d != d.to_integral_value():
        return None
    return int(d)


def _money(v) -> Optional[str]:
    if v is None or isinstance(v, bool):
        return None
    try:
        d = Decimal(repr(v)) if isinstance(v, float) else Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite():
        return None
    return str(d.quantize(Decimal("0.01")))


def _str(v) -> Optional[str]:
    if v is None or isinstance(v, (dict, list)):
        return None
    s = str(v).strip()
    return s[:_MAX_STR] if s else None


def _date(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    try:
        return dt.date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        return None


def _bool(v) -> Optional[bool]:
    return v if isinstance(v, bool) else None


def tokenize_terms(text: str) -> list[str]:
    """Sorted unique lowercase word tokens ([a-z0-9]+)."""
    return sorted(set(_TOKEN.findall((text or "").lower())))[:_MAX_TERMS]


def _vendor_key(vendor_id: Optional[str]) -> Optional[str]:
    if not vendor_id:
        return None
    try:
        from app.policy_rag.embeddings import load_config
        return (load_config().get("catering_vendor_keys") or {}).get(str(vendor_id))
    except Exception:  # pragma: no cover - config unreadable -> fact missing (never guessed)
        return None


def _rate_entries(rate_validations) -> list[dict]:
    out = []
    for rv in rate_validations or []:
        d = _d(rv)
        status = d.get("validation_status")
        if not status or status == "not_applicable" or d.get("applicable") is False:
            continue
        out.append(d)
    return out


def build_facts(ticket: dict, workflow_result: dict, rate_validations: list[dict] | None = None,
                retrieval_info: dict | None = None) -> FactSnapshot:
    """Build the typed fact snapshot.

    ``rate_validations``: RateValidationResult dicts/models per PO line (``None`` = not computed -> rate facts
    missing; ``[]`` or only not_applicable entries -> ``rate.applicable = False``). Entries with
    ``validation_status == "not_applicable"`` or ``applicable: False`` are ignored.
    ``retrieval_info``: ``{"retrieval_mode": ..., "insufficient_evidence": ...}`` (``None`` -> policy facts missing).
    """
    ticket = _d(ticket)
    wr = _d(workflow_result)
    sub = _d(ticket.get("submission"))
    req = _d(wr.get("request"))
    vendors = _d(wr.get("vendors"))
    selected = _d(vendors.get("selected"))
    po = _d(_d(wr.get("procurement")).get("po"))
    recon = _d(wr.get("reconciliation"))

    raw: dict[str, Any] = {}
    currencies: dict[str, str] = {}

    raw["request.attendees"] = _int(_first(req.get("attendees"), sub.get("attendees")))
    raw["request.service"] = _str(_first(req.get("service"), sub.get("service")))
    raw["request.location"] = _str(_first(req.get("location"), sub.get("location")))
    raw["request.event_date"] = _date(_first(req.get("date"), sub.get("event_date")))
    raw["request.event_category"] = _str(_first(sub.get("event_category"), req.get("category")))
    req_currency = _str(req.get("currency")) or ("INR" if sub.get("budget_inr") is not None else None)
    raw["request.currency"] = req_currency.upper() if req_currency else None
    if req.get("budget_limit") is not None:
        raw["request.budget_limit"] = _money(req.get("budget_limit"))
        currencies["request.budget_limit"] = raw["request.currency"] or "INR"
    else:
        raw["request.budget_limit"] = _money(sub.get("budget_inr"))
        currencies["request.budget_limit"] = "INR"
    if sub:
        text = " ".join(str(sub.get(k) or "") for k in ("title", "notes"))
        raw["request.free_text_terms"] = tokenize_terms(text)
    else:
        raw["request.free_text_terms"] = None
    raw["request.approved_vendor_only"] = _bool(sub.get("approved_vendor_only"))

    vendor_id = _str(_first(po.get("vendor_id"), selected.get("vendor_id")))
    raw["vendor.selected"] = bool(vendor_id)
    raw["vendor.vendor_id"] = vendor_id
    raw["vendor.vendor_key"] = _vendor_key(vendor_id)
    approved = _bool(selected.get("approved")) if selected and selected.get("vendor_id") == vendor_id else None
    raw["vendor.approved"] = approved

    po_currency = (_str(po.get("currency")) or "INR").upper() if po else None
    for name, key in (("po.total", "total"), ("po.subtotal", "subtotal"), ("po.tax", "tax")):
        raw[name] = _money(po.get(key)) if po else None
        if po:
            currencies[name] = po_currency
    raw["po.currency"] = po_currency
    items = po.get("line_items") if po else None
    raw["po.line_count"] = len(items) if isinstance(items, list) else None

    raw["workflow.final_status"] = _str(wr.get("final_status"))
    raw["workflow.reconciliation_status"] = _str(recon.get("status"))

    if rate_validations is None:
        for n in ("rate.validation_statuses", "rate.any_fail", "rate.any_unverified", "rate.applicable"):
            raw[n] = None
    else:
        entries = _rate_entries(rate_validations)
        statuses = sorted({str(e["validation_status"]) for e in entries})
        raw["rate.validation_statuses"] = statuses
        raw["rate.applicable"] = bool(entries)
        raw["rate.any_fail"] = "fail" in statuses
        raw["rate.any_unverified"] = any(s in UNVERIFIED_RATE_STATUSES for s in statuses)

    info = _d(retrieval_info) if retrieval_info is not None else None
    raw["policy.retrieval_mode"] = _str(info.get("retrieval_mode")) if info is not None else None
    raw["policy.insufficient_evidence"] = _bool(info.get("insufficient_evidence")) if info is not None else None

    values = {k: raw[k] for k in FACT_TYPES if raw.get(k) is not None}
    missing = frozenset(k for k in FACT_TYPES if raw.get(k) is None)
    currencies = {k: v for k, v in currencies.items() if k in values}
    types = dict(FACT_TYPES)
    digest = canonical_hash({"values": values, "types": types, "missing": sorted(missing),
                             "currencies": currencies})
    return FactSnapshot(values=values, types=types, missing=missing, currencies=currencies, hash=digest)


def snapshot_from_values(values: dict[str, Any], currencies: dict[str, str] | None = None) -> FactSnapshot:
    """Build a snapshot from already-typed values (tests / replay). Unknown names are rejected."""
    unknown = sorted(set(values) - set(FACT_TYPES))
    if unknown:
        raise ValueError(f"unknown fact(s): {', '.join(unknown)}")
    vals = {k: values[k] for k in FACT_TYPES if values.get(k) is not None}
    cur = {k: FACT_CURRENCIES[k] for k in vals if k in FACT_CURRENCIES}
    cur.update({k: v for k, v in (currencies or {}).items() if k in vals})
    missing = frozenset(k for k in FACT_TYPES if k not in vals)
    types = dict(FACT_TYPES)
    digest = canonical_hash({"values": vals, "types": types, "missing": sorted(missing), "currencies": cur})
    return FactSnapshot(values=vals, types=types, missing=missing, currencies=cur, hash=digest)
