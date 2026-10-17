"""/api/policy-rules*, /api/policy-set/active and /api/policy-documents/{id}/extract-rules (Phase 3).

Policy reviewers (approver or admin) list, inspect, create, validate, approve and reject rule versions;
extraction from a document is admin-only. Requesters get 403 everywhere. Identity and role come only from
the bearer session; bodies never carry actor/role/status (extra=forbid). Errors are client-safe.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field

from app.auth.deps import require_admin, require_approver_or_admin
from app.policy_rag.storage import DOCUMENT_ID_PATTERN
from app.policy_rules import evaluator, extraction, registry
from app.policy_rules.registry import RULE_ID_PATTERN, RuleRegistryError
from app.policy_rules.schemas import PolicyRuleIn, RuleReviewIn, RuleStatus
from app.portal_schemas import UserPublic

router = APIRouter(tags=["policy-rules"])


class RuleValidateIn(BaseModel):
    model_config = {"extra": "forbid"}
    rule_version: int = Field(ge=1)


def _summary(doc: dict) -> dict:
    out = {k: doc.get(k) for k in ("rule_id", "rule_version", "name", "status", "severity", "source",
                                   "policy_domain", "mandatory", "effective_from", "effective_until",
                                   "extraction_confidence", "extraction_model", "supersedes", "approved_by",
                                   "approved_at")}
    out["reason_code"] = (doc.get("outcome") or {}).get("reason_code")
    out["decision"] = (doc.get("outcome") or {}).get("decision")
    return out


def _with_text(doc: dict) -> dict:
    """Rule document plus readable renderings of its typed expressions (for the review UI)."""
    out = dict(doc)
    try:
        out["condition_text"] = evaluator.describe_expression(doc["condition"])
        if doc.get("applies_when"):
            out["applies_when_text"] = evaluator.describe_expression(doc["applies_when"])
    except Exception:  # noqa: BLE001 - rendering is cosmetic; never fail the read
        out.setdefault("condition_text", None)
    return out


@router.get("/api/policy-rules")
def list_rules(status: Optional[RuleStatus] = Query(None),
               rule_id: Optional[str] = Query(None, pattern=RULE_ID_PATTERN),
               limit: int = Query(200, ge=1, le=500),
               _: UserPublic = Depends(require_approver_or_admin)):
    rules = registry.list_rules(status=status, rule_id=rule_id, limit=limit)
    return {"rules": [_with_text(r) for r in rules], "counts": registry.count_rules_by_status(),
            "active_policy_set_version": registry.active_version()}


@router.get("/api/policy-rules/{rule_id}/versions/{version}")
def get_rule(rule_id: str = Path(..., pattern=RULE_ID_PATTERN), version: int = Path(..., ge=1, le=999999),
             _: UserPublic = Depends(require_approver_or_admin)):
    doc = registry.get_rule(rule_id, version)
    if doc is None:
        raise RuleRegistryError(404, registry.MSG_NOT_FOUND)
    pset = registry.active_policy_set()
    active = bool(pset) and registry.rule_ref(doc) in (pset.get("rule_refs") or [])
    events = [{k: e.get(k) for k in ("event_type", "actor", "created_at", "policy_set_version", "status")
               if e.get(k) is not None}
              for e in registry.repo.list_events(rule_id=rule_id, limit=50)
              if e.get("rule_version") in (None, version)]
    return {**_with_text(doc), "active": active, "events": events}


@router.post("/api/policy-rules", status_code=201)
def create_rule(body: PolicyRuleIn, actor: UserPublic = Depends(require_approver_or_admin)):
    return registry.create_candidate(body, actor)


@router.post("/api/policy-rules/{rule_id}/validate")
def validate_rule(body: RuleValidateIn, rule_id: str = Path(..., pattern=RULE_ID_PATTERN),
                  actor: UserPublic = Depends(require_approver_or_admin)):
    return registry.validate(rule_id, body.rule_version, actor)


@router.post("/api/policy-rules/{rule_id}/approve")
def approve_rule(body: RuleReviewIn, rule_id: str = Path(..., pattern=RULE_ID_PATTERN),
                 actor: UserPublic = Depends(require_approver_or_admin)):
    return registry.approve(rule_id, body.rule_version, actor, comment=body.comment,
                            expected_policy_set_version=body.expected_policy_set_version)


@router.post("/api/policy-rules/{rule_id}/reject")
def reject_rule(body: RuleReviewIn, rule_id: str = Path(..., pattern=RULE_ID_PATTERN),
                actor: UserPublic = Depends(require_approver_or_admin)):
    return registry.reject(rule_id, body.rule_version, actor, body.comment)


@router.post("/api/policy-documents/{document_id}/extract-rules")
def extract_rules(document_id: str = Path(..., pattern=DOCUMENT_ID_PATTERN),
                  actor: UserPublic = Depends(require_admin)):
    result = extraction.extract_rules(document_id, actor)
    return {**result, "candidates": [_with_text(c) for c in result["candidates"]]}


@router.get("/api/policy-set/active")
def active_policy_set(_: UserPublic = Depends(require_approver_or_admin)):
    pset = registry.active_policy_set()
    if not pset:
        return {"active": False, "policy_set_version": 0, "rule_refs": [], "rule_hashes": {}, "rules": []}
    refs = set(pset.get("rule_refs") or [])
    rules = [_summary(d) for d in registry.list_rules(status="approved", limit=1000) if registry.rule_ref(d) in refs]
    return {"active": True, **pset, "rules": rules}
