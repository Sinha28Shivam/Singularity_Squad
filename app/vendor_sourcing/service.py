"""Vendor discovery service: internal catalog first, external discovery is deliberately disabled."""
from __future__ import annotations

import re
from typing import Optional
from uuid import uuid4

from app.data_loader import load_json
from app.vendor_sourcing import repository
from app.vendor_sourcing.schemas import MAX_PAGE_LIMIT, MAX_PAGE_OFFSET, CandidateIn

_CORRELATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


def search_vendors(query: str = "", service: Optional[str] = None,
                   status: Optional[str] = None, limit: int = 100, offset: int = 0) -> dict:
    """Search the existing internal catalog first, then persistent onboarding candidates."""
    q = query.strip().casefold()
    service_filter = service.strip().casefold() if service else ""
    internal = []
    for raw in load_json("vendors.json"):
        haystack = " ".join([raw.get("name", ""), raw.get("vendor_id", ""),
                             *raw.get("services", [])]).casefold()
        if q and q not in haystack:
            continue
        if service_filter and service_filter not in [s.casefold() for s in raw.get("services", [])]:
            continue
        if status == "approved" and not raw.get("approved"):
            continue
        if status in {"pending_review", "rejected", "suspended"}:
            continue
        internal.append({
            "vendor_id": raw["vendor_id"], "name": raw["name"], "approved": bool(raw.get("approved")),
            "services": raw.get("services", []), "capacity": raw.get("capacity"),
            "rate_per_person": raw.get("rate_per_person"), "currency": "INR",
            "source": "internal_catalog", "read_only": True,
        })
    limit = max(1, min(int(limit), MAX_PAGE_LIMIT))
    offset = max(0, min(int(offset), MAX_PAGE_OFFSET))
    page = repository.page_candidates(query=query, status=status, service=service, limit=limit, offset=offset)
    return {"internal_vendors": internal[:limit], "candidates": page["items"],
            "candidates_total": page["total"], "limit": limit, "offset": offset,
            "next_offset": page["next_offset"],
            "external_discovery_enabled": False,
            "note": "Internal catalog and candidate registry searched first; no external search was run."}


def list_candidates(*, query: str = "", status: Optional[str] = None, service: Optional[str] = None,
                    limit: int = 50, offset: int = 0) -> dict:
    return repository.page_candidates(query=query, status=status, service=service, limit=limit, offset=offset)


def submit_candidate(body: CandidateIn, actor: str, correlation_id: Optional[str] = None) -> dict:
    data = body.model_dump()
    data["candidate_id"] = "VCAND-" + uuid4().hex[:12].upper()
    data["name"] = " ".join(data["name"].split())
    return repository.create_candidate(data, actor, correlation_id=_correlation(correlation_id))


def decide_candidate(candidate_id: str, decision: str, reason: str, actor: str,
                     correlation_id: Optional[str] = None) -> Optional[dict]:
    return repository.decide_candidate(candidate_id, decision, reason, actor,
                                       correlation_id=_correlation(correlation_id))


def get_candidate(candidate_id: str) -> Optional[dict]:
    return repository.get_candidate(candidate_id)


def candidate_events(candidate_id: str, limit: int = 200) -> Optional[list[dict]]:
    if repository.get_candidate(candidate_id) is None:  # read also repairs pending events first
        return None
    return repository.list_events(candidate_id, limit)


def repair_pending_events(limit: int = 500) -> dict:
    return repository.repair_pending_events(limit)


def _correlation(value: Optional[str]) -> str:
    value = (value or "").strip()
    if value and _CORRELATION_RE.match(value):
        return value
    return "corr-" + uuid4().hex
