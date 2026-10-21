"""Validated RFQ and quotation contracts. Monetary values use Decimal, never binary floats."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RFQCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=4, max_length=80, pattern=r"^REQ-[A-Za-z0-9-]{1,64}$")
    service: str = Field(min_length=2, max_length=80)
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=3)
    budget_limit: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    currency: str = Field(default="INR", pattern=r"^[A-Z]{3}$")
    delivery_date: date
    delivery_location: str = Field(min_length=2, max_length=200)
    warranty_months_required: int = Field(default=0, ge=0, le=120)
    compliance_requirements: list[str] = Field(default_factory=list, max_length=30)
    response_deadline: datetime
    candidate_ids: list[str] = Field(min_length=1, max_length=50)

    @field_validator("service", "delivery_location")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("response_deadline")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("response_deadline must include a timezone, for example Z or +05:30")
        return value


class QuoteSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vendor_id: str = Field(min_length=3, max_length=80)
    quote_reference: str = Field(min_length=1, max_length=120)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit_price: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    tax_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    delivery_fee: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    other_fees: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    delivery_date: date
    warranty_months: int = Field(ge=0, le=120)
    warranty_scope: str = Field(min_length=1, max_length=1000)
    compliance: dict[str, bool] = Field(default_factory=dict)
    notes: str = Field(default="", max_length=2000)


class QuoteVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = Field(pattern=r"^[A-Z]{3}$")
    unit_price: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    tax_amount: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    delivery_fee: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    other_fees: Decimal = Field(default=Decimal("0"), ge=0, max_digits=14, decimal_places=2)
    delivery_date: date
    warranty_months: int = Field(ge=0, le=120)
    warranty_scope: str = Field(min_length=1, max_length=1000)
    compliance: dict[str, bool] = Field(default_factory=dict)
    reviewer_note: str = Field(min_length=5, max_length=1000)


class QuoteView(BaseModel):
    quote_id: str
    rfq_id: str
    vendor_id: str
    quote_reference: str
    submission_type: str
    status: str
    currency: str
    quantity: Decimal
    unit_price: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    delivery_fee: Optional[Decimal] = None
    other_fees: Optional[Decimal] = None
    delivery_date: Optional[date] = None
    warranty_months: Optional[int] = None
    warranty_scope: Optional[str] = None
    compliance: dict[str, bool] = Field(default_factory=dict)
    pdf_filename: Optional[str] = None
    pdf_sha256: Optional[str] = None
    extracted_text: Optional[str] = None
    reviewer_note: Optional[str] = None
    submitted_at: datetime
    verified_at: Optional[datetime] = None
    verified_by: Optional[str] = None
