"""Workflow integration of the deterministic rule engine.

``evaluate_ticket_rules`` evaluates the ACTIVE approved policy set (registry pointer, effective on ``as_of`` =
event date, UTC calendar-date convention) against the typed fact snapshot of one ticket run and returns a
``RuleEngineEvaluation``. It is pure apart from reading the registry: no writes, no LLM, no external calls.
Any registry/evaluator failure raises (callers fail closed: no approval request, HTTP 503).

Coverage: every policy domain of the active rules gets a typed coverage item; an empty active set is
``missing_coverage`` (never an implicit pass).
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
from typing import Any, Iterable, Optional

from app.policy_rules import evaluator
from app.policy_rules.facts import build_facts, canonical_hash
from app.policy_rules.schemas import EVALUATOR_VERSION, CoverageItem, PolicyRule, RuleEngineEvaluation, RuleResult

_HASH_EXCLUDE = {"evaluation_id", "evaluated_at", "engine_hash"}
DEFAULT_DOMAIN = "general"


def policy_rules_enabled() -> bool:
    """Production default: enabled. Tests default to disabled via conftest (POLICY_RULES_ENABLED=0)."""
    return os.getenv("POLICY_RULES_ENABLED", "1").strip() != "0"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _registry():
    from app.policy_rules import registry
    return registry


def _as_rule(obj: Any) -> PolicyRule:
    return obj if isinstance(obj, PolicyRule) else PolicyRule.model_validate(obj)


def active_policy_set(as_of: Optional[dt.date]) -> tuple[int, list[PolicyRule]]:
    """(active policy_set_version, approved rules effective on ``as_of``). Raises on any registry error."""
    reg = _registry()
    version = int(reg.active_version())
    pset = reg.active_policy_set() if version else None
    allowed = set((pset or {}).get("rule_refs") or [])
    rules = [_as_rule(r) for r in (reg.active_rules(as_of) or [])]
    if any(r.status != "approved" for r in rules):
        raise RuntimeError("Registry returned a rule that is not approved")
    if any(evaluator.rule_ref(r) not in allowed for r in rules):
        raise RuntimeError("Registry returned a rule outside the active policy set")
    if int(reg.active_version()) != version:
        raise RuntimeError("Active policy set changed during evaluation")  # caller fails closed; re-run
    return version, rules


def active_version() -> int:
    return int(_registry().active_version())


def _coverage(rules: list[PolicyRule], results: list[RuleResult]) -> list[CoverageItem]:
    if not rules:
        return [CoverageItem(area="policy_rules", status="missing_coverage",
                             detail="No approved policy rules are active; nothing was checked.")]
    by_area: dict[str, list[tuple[PolicyRule, RuleResult]]] = {}
    for rule, res in zip(rules, results):
        by_area.setdefault(rule.policy_domain or DEFAULT_DOMAIN, []).append((rule, res))
    items = []
    for area in sorted(by_area):
        pairs = by_area[area]
        refs = sorted(evaluator.rule_ref(r) for r, _ in pairs)
        statuses = {res.status for _, res in pairs}
        if "evaluation_error" in statuses:
            status, detail = "evaluation_error", "At least one rule could not be evaluated."
        elif statuses == {"not_applicable"}:
            status, detail = "not_applicable", "No rule in this area applies to the request."
        else:
            status, detail = "covered", None
        items.append(CoverageItem(area=area, status=status, rule_refs=refs, detail=detail))
    return items


def compute_engine_hash(ev: RuleEngineEvaluation) -> str:
    data = ev.model_dump(mode="json", by_alias=True)
    canonical = {k: v for k, v in data.items() if k not in _HASH_EXCLUDE}
    canonical["results"] = [{k: v for k, v in r.items() if k != "evaluated_at"} for r in canonical["results"]]
    return canonical_hash(canonical)


def _fields(rules: Iterable[PolicyRule]) -> set[str]:
    out: set[str] = set()
    for r in rules:
        out.update(evaluator.expression_fields(r.condition))
        if r.applies_when is not None:
            out.update(evaluator.expression_fields(r.applies_when))
    return out


def evaluate_ticket_rules(ticket: dict, workflow_result: dict, rate_validations: Optional[list] = None,
                          retrieval_info: Optional[dict] = None, as_of: Optional[dt.date] = None, *,
                          now: Optional[dt.datetime] = None, rules: Optional[list] = None,
                          policy_set_version: Optional[int] = None) -> RuleEngineEvaluation:
    """Evaluate the active policy set (or the explicit ``rules``/``policy_set_version`` for tests/replay)."""
    now = now or _now()
    if as_of is None:
        as_of = now.astimezone(dt.timezone.utc).date()
    if rules is None:
        policy_set_version, active = active_policy_set(as_of)
    else:
        active = [_as_rule(r) for r in rules]
        policy_set_version = int(policy_set_version or 0)
    rv_json = [r.model_dump(mode="json") if hasattr(r, "model_dump") else dict(r) for r in (rate_validations or [])]
    facts = build_facts(ticket, workflow_result, rv_json if rate_validations is not None else None, retrieval_info)
    results = [evaluator.evaluate_rule(r, facts, policy_set_version, now, as_of=as_of) for r in active]
    coverage = _coverage(active, results)
    agg = evaluator.aggregate(results, coverage)
    ev = RuleEngineEvaluation(
        evaluation_id=f"reng_{secrets.token_hex(8)}", ticket_id=str(ticket.get("ticket_id") or ""),
        run_count=int(ticket.get("run_count") or 0), policy_set_version=policy_set_version,
        evaluator_version=EVALUATOR_VERSION, evaluated_at=now, fact_snapshot_hash=facts.hash,
        facts_used=facts.subset(_fields(active)), results=results, coverage=coverage, overall=agg["overall"],
        reason_codes=list(agg["reason_codes"]), blocking_rule_refs=list(agg["blocking_rule_refs"]),
        review_rule_refs=list(agg["review_rule_refs"]),
        clarification_rule_refs=list(agg["clarification_rule_refs"]), rate_validations=rv_json)
    ev.engine_hash = compute_engine_hash(ev)
    return ev


def binding(engine: Optional[dict]) -> Optional[dict]:
    """Fields bound into the approval review snapshot (covered by snapshot_hash)."""
    if not isinstance(engine, dict) or not engine.get("engine_hash"):
        return None
    refs = sorted({f"{r.get('rule_id')}@{r.get('rule_version')}" for r in engine.get("results") or []})
    return {"policy_set_version": engine.get("policy_set_version"), "engine_hash": engine.get("engine_hash"),
            "evaluator_version": engine.get("evaluator_version"), "rule_refs": refs}


def redact(engine: Optional[dict]) -> Optional[dict]:
    """Requester-safe projection: overall + per-rule status/explanation only."""
    if not isinstance(engine, dict):
        return None
    return {"overall": engine.get("overall"),
            "results": [{"status": r.get("status"), "explanation": r.get("explanation")}
                        for r in engine.get("results") or [] if r.get("status") != "not_applicable"]}
