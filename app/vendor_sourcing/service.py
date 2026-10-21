"""Vendor discovery service: internal catalog first, external discovery is deliberately disabled."""
from __future__ import annotations

from typing import Optional

from app.data_loader import load_json
from app.vendor_sourcing import repository
from app.vendor_sourcing.schemas import CandidateIn, CandidateView


def search_vendors(query: str = "", service: Optional[str] = None,
                   status: Optional[str] = None, limit: int = 100) -> dict:
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
        internal.append({
            "vendor_id": raw["vendor_id"], "name": raw["name"], "approved": bool(raw.get("approved")),
            "services": raw.get("services", []), "capacity": raw.get("capacity"),
            "rate_per_person": raw.get("rate_per_person"), "currency": "INR",
            "source": "internal_catalog", "read_only": True,
        })
    candidates = repository.list_candidates(query=query, status=status, limit=limit)
    if service_filter:
        candidates = [c for c in candidates if service_filter in [s.casefold() for s in c.get("services", [])]]
    return {"internal_vendors": internal[:limit], "candidates": candidates[:limit],
            "external_discovery_enabled": False,
            "note": "Internal catalog and candidate registry searched first; no external search was run."}


def submit_candidate(body: CandidateIn, actor: str) -> dict:
    data = body.model_dump()
    data["candidate_id"] = "VCAND-" + __import__("uuid").uuid4().hex[:12].upper()
    data["name"] = " ".join(data["name"].split())
    return repository.create_candidate(data, actor)


def decide_candidate(candidate_id: str, decision: str, reason: str, actor: str) -> Optional[dict]:
    return repository.decide_candidate(candidate_id, decision, reason, actor)
