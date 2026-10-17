"""Draft PO persistence (`purchase_orders`). POs are only ever stored with status "draft"."""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Union

from pymongo import ASCENDING
from pymongo.database import Database
from pymongo.errors import PyMongoError

from app.db.errors import PersistenceError
from app.repositories.ticket_repository import NO_ID, SCHEMA_VERSION, _as_utc, _now, _resolve_db
from app.schemas import DraftPO


def upsert_draft_po(ticket_id: str, draft_po: Union[DraftPO, dict], created_at: datetime,
                    *, db: Optional[Database] = None) -> None:
    """Upsert keyed by po_id so retries never create duplicate draft POs."""
    fields = draft_po.model_dump(mode="json") if isinstance(draft_po, DraftPO) else dict(draft_po)
    fields.pop("_id", None)
    if not fields.get("po_id"):
        raise ValueError("po_id is required")
    if fields.get("status") != "draft":
        raise ValueError("Only draft purchase orders may be persisted.")
    for key in ("created_at", "schema_version"):
        fields.pop(key, None)
    fields["ticket_id"] = ticket_id
    fields["updated_at"] = _now()
    try:
        _resolve_db(db).purchase_orders.update_one(
            {"po_id": fields["po_id"]},
            {"$set": fields, "$setOnInsert": {"created_at": _as_utc(created_at), "schema_version": SCHEMA_VERSION}},
            upsert=True,
        )
    except PyMongoError as exc:
        raise PersistenceError() from exc


def get_draft_pos_for_ticket(ticket_id: str, *, db: Optional[Database] = None) -> list[dict]:
    try:
        return list(_resolve_db(db).purchase_orders.find({"ticket_id": ticket_id}, NO_ID).sort("created_at", ASCENDING))
    except PyMongoError as exc:
        raise PersistenceError() from exc
