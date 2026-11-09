"""Request/response models for vendor invoice / delivery-note intake.

Extracted values are UNTRUSTED suggestions; only the fields an admin submits through ``VerifyIn`` become an
``app.schemas.Invoice`` / ``DeliveryRecord`` (after deterministic arithmetic and uniqueness checks).
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

INVOICE_ID_PATTERN = r"^INV-[A-Za-z0-9-]{1,32}$"
_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$"

DocStatus = Literal["pending_review", "verified", "rejected"]
DOC_STATUSES = ("pending_review", "verified", "rejected")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class InvoiceLineIn(_Strict):
    description: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0, le=1e9)
    unit_price: float = Field(ge=0, le=1e12)
    line_total: float = Field(ge=0, le=1e13)


class DeliveryIn(_Strict):
    """Optional delivery-note fields; ``delivery_id`` defaults to the invoice's ``delivery_id``."""
    delivery_id: Optional[str] = Field(default=None, pattern=_REF_PATTERN)
    delivered_on: date
    items: dict[str, float] = Field(min_length=1, max_length=50)
    signed_by: str = Field(min_length=1, max_length=120)
    notes: str = Field(default="", max_length=500)

    @field_validator("items")
    @classmethod
    def _items(cls, v: dict[str, float]) -> dict[str, float]:
        out: dict[str, float] = {}
        for k, q in v.items():
            name = str(k).strip()
            if not name or len(name) > 200:
                raise ValueError("delivery item descriptions must be 1-200 characters")
            if q is None or float(q) < 0:
                raise ValueError("delivered quantities must be >= 0")
            out[name] = float(q)
        return out


class VerifyIn(_Strict):
    invoice_id: str = Field(pattern=INVOICE_ID_PATTERN)
    vendor_id: str = Field(pattern=_REF_PATTERN)
    po_id: str = Field(pattern=_REF_PATTERN)
    invoice_date: date
    delivery_id: str = Field(pattern=_REF_PATTERN)
    line_items: list[InvoiceLineIn] = Field(min_length=1, max_length=50)
    subtotal: float = Field(ge=0, le=1e13)
    tax: float = Field(ge=0, le=1e13)
    total: float = Field(ge=0, le=1e13)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    delivery: Optional[DeliveryIn] = None


class RejectIn(_Strict):
    reason: str = Field(min_length=3, max_length=500)
