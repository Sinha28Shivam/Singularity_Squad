"""Deterministic, side-effect-free interpreter for the restricted rule language.

- ``validate_expression`` / ``validate_rule``: static checks (limits, registry fields, type/currency/unit
  compatibility, operator/value shapes). A rule with any validation error can never be approved and is never
  evaluated (``evaluate_rule`` returns ``evaluation_error`` for it).
- ``evaluate_expression``: three-valued (Kleene) logic. ``None`` = unknown because a referenced fact is missing.
  Raises ``RuleEvaluationError`` on type/currency mismatch (``evaluate_rule`` maps it to ``evaluation_error``).
- ``evaluate_rule``: effective window -> applies_when -> condition -> typed ``RuleResult``.
- ``aggregate``: blocked > needs_clarification > review_required > pass; every finding is retained.

Money/decimal use ``Decimal`` only; dates are ISO calendar dates (UTC convention). No regex, no eval, no I/O.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional, Union

from pydantic import TypeAdapter, ValidationError

from app.policy_rules.facts import FACT_CURRENCIES, FACT_TYPES, FactSnapshot, canonical_hash
from app.policy_rules.schemas import (EVALUATOR_VERSION, MAX_EXPRESSION_DEPTH, MAX_EXPRESSION_NODES,
                                      RULE_SCHEMA_VERSION, AllOf, AnyOf, Condition, CoverageItem, Expression,
                                      NotOf, PolicyRule, RuleResult)

__all__ = ["RuleEvaluationError", "validate_expression", "validate_rule", "evaluate_expression", "evaluate_rule",
           "aggregate", "canonical_rule_hash", "canonical_hash", "rule_ref", "expression_fields",
           "describe_expression", "parse_expression", "load_seed_bundle", "LIFECYCLE_FIELDS", "FACT_UNITS",
           "TOKENIZED_FACTS"]

NUMERIC_TYPES = frozenset({"integer", "decimal", "money"})
SCALAR_IN_TYPES = frozenset({"string", "integer"})
TOKENIZED_FACTS = frozenset({"request.free_text_terms"})
# Accepted unit labels per fact (facts without an entry accept no unit).
FACT_UNITS: dict[str, frozenset[str]] = {
    "request.attendees": frozenset({"attendee", "attendees", "people", "persons", "person", "participants",
                                    "participant", "headcount", "guests", "pax"}),
    "po.line_count": frozenset({"lines", "line", "items", "item"}),
}
LIFECYCLE_FIELDS = frozenset({"status", "validation_errors", "conflict_refs", "created_by", "created_at",
                              "reviewed_by", "reviewed_at", "review_comment", "approved_by", "approved_at"})
_TERM = re.compile(r"^[a-z0-9]+$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EXPR_ADAPTER = TypeAdapter(Expression)

MISSING_CODE = "MANDATORY_FACTS_MISSING"
ERROR_CODE = "RULE_EVALUATION_ERROR"
COVERAGE_MISSING_CODE = "POLICY_COVERAGE_MISSING"


class RuleEvaluationError(Exception):
    """Type/currency/shape problem detected while evaluating (never a pass)."""


# ----------------------------------------------------------------------------------------------- helpers
def rule_ref(rule: PolicyRule) -> str:
    return f"{rule.rule_id}@{rule.rule_version}"


def parse_expression(expr: Any) -> Union[Condition, AllOf, AnyOf, NotOf]:
    """Parse a dict into a typed expression (raises pydantic ValidationError)."""
    if isinstance(expr, (Condition, AllOf, AnyOf, NotOf)):
        return expr
    return _EXPR_ADAPTER.validate_python(expr)


def _children(expr) -> list:
    if isinstance(expr, AllOf):
        return list(expr.all)
    if isinstance(expr, AnyOf):
        return list(expr.any)
    if isinstance(expr, NotOf):
        return [expr.not_]
    return []


def expression_fields(expr) -> list[str]:
    """Sorted unique fact names referenced by an expression."""
    out: set[str] = set()
    stack = [expr]
    while stack:
        e = stack.pop()
        if isinstance(e, Condition):
            out.add(e.field)
        else:
            stack.extend(_children(e))
    return sorted(out)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _to_decimal(v, what: str) -> Decimal:
    if isinstance(v, bool) or v is None or isinstance(v, float):
        raise RuleEvaluationError(f"{what} is not an exact number")
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        raise RuleEvaluationError(f"{what} is not a number") from None
    if not d.is_finite():
        raise RuleEvaluationError(f"{what} is not finite")
    return d


def _to_date(v, what: str) -> dt.date:
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if not isinstance(v, str) or not _ISO_DATE.match(v):
        raise RuleEvaluationError(f"{what} is not an ISO date")
    try:
        return dt.date.fromisoformat(v)
    except ValueError:
        raise RuleEvaluationError(f"{what} is not a valid date") from None


def _valid_number(v, vtype: str) -> bool:
    if vtype == "integer":
        return _is_int(v)
    if isinstance(v, bool) or v is None:
        return False
    try:
        return Decimal(str(v).strip()).is_finite()
    except (InvalidOperation, ValueError):
        return False


def _valid_date(v) -> bool:
    try:
        _to_date(v, "value")
        return True
    except RuleEvaluationError:
        return False


def _valid_scalar(v, vtype: str) -> bool:
    if vtype == "string":
        return isinstance(v, str) and len(v) > 0
    if vtype == "boolean":
        return isinstance(v, bool)
    if vtype == "date":
        return _valid_date(v)
    if vtype in NUMERIC_TYPES:
        return _valid_number(v, vtype)
    return False


# -------------------------------------------------------------------------------------------- validation
def _validate_condition(c: Condition, path: str) -> list[str]:
    errs: list[str] = []
    ftype = FACT_TYPES.get(c.field)
    if ftype is None:
        return [f"{path}: unknown field '{c.field}' (not in the fact registry)"]
    if c.value_type != ftype:
        errs.append(f"{path}: value_type '{c.value_type}' incompatible with fact '{c.field}' of type '{ftype}'")
        return errs
    # currency
    if ftype == "money":
        if not c.currency:
            errs.append(f"{path}: money comparison on '{c.field}' requires a currency")
        elif c.currency != FACT_CURRENCIES.get(c.field):
            errs.append(f"{path}: currency {c.currency} does not match fact currency "
                        f"{FACT_CURRENCIES.get(c.field)} of '{c.field}'")
    elif c.currency is not None:
        errs.append(f"{path}: currency is only allowed for money facts")
    # unit
    if c.unit is not None and c.unit.strip().lower() not in FACT_UNITS.get(c.field, frozenset()):
        errs.append(f"{path}: unit '{c.unit}' is not compatible with fact '{c.field}'")

    op, v = c.op, c.value
    if op in ("exists", "is_missing"):
        if v is not None:
            errs.append(f"{path}: operator '{op}' takes no value")
    elif op in ("eq", "ne"):
        if ftype == "string_list":
            errs.append(f"{path}: operator '{op}' not supported on list fact '{c.field}' (use contains_any)")
        elif isinstance(v, list) or not _valid_scalar(v, ftype):
            errs.append(f"{path}: operator '{op}' requires a single {ftype} value")
    elif op in ("gt", "gte", "lt", "lte"):
        if ftype not in NUMERIC_TYPES:
            errs.append(f"{path}: operator '{op}' requires a numeric fact (use date_before/date_after for dates)")
        elif isinstance(v, list) or not _valid_number(v, ftype):
            errs.append(f"{path}: operator '{op}' requires a single {ftype} value")
    elif op == "between":
        if ftype not in NUMERIC_TYPES:
            errs.append(f"{path}: operator 'between' requires a numeric fact")
        elif not (isinstance(v, list) and len(v) == 2 and all(_valid_number(x, ftype) for x in v)):
            errs.append(f"{path}: operator 'between' requires a list of two {ftype} bounds")
        elif Decimal(str(v[0])) > Decimal(str(v[1])):
            errs.append(f"{path}: 'between' lower bound exceeds upper bound")
    elif op in ("in", "not_in"):
        if ftype not in SCALAR_IN_TYPES:
            errs.append(f"{path}: operator '{op}' requires a string or integer fact")
        elif not (isinstance(v, list) and v and all(_valid_scalar(x, ftype) for x in v)):
            errs.append(f"{path}: operator '{op}' requires a non-empty list of {ftype} values")
    elif op == "contains_any":
        if ftype != "string_list":
            errs.append(f"{path}: operator 'contains_any' requires a string_list fact")
        elif not (isinstance(v, list) and v and all(isinstance(x, str) and x for x in v)):
            errs.append(f"{path}: operator 'contains_any' requires a non-empty list of strings")
        elif c.field in TOKENIZED_FACTS and not all(_TERM.match(x) for x in v):
            errs.append(f"{path}: 'contains_any' on '{c.field}' requires single lowercase word tokens")
    elif op in ("date_before", "date_after"):
        if ftype != "date":
            errs.append(f"{path}: operator '{op}' requires a date fact")
        elif isinstance(v, list) or not _valid_date(v):
            errs.append(f"{path}: operator '{op}' requires an ISO date value (YYYY-MM-DD)")
    elif op == "date_between":
        if ftype != "date":
            errs.append(f"{path}: operator 'date_between' requires a date fact")
        elif not (isinstance(v, list) and len(v) == 2 and all(_valid_date(x) for x in v)):
            errs.append(f"{path}: operator 'date_between' requires a list of two ISO dates")
        elif _to_date(v[0], "lower") > _to_date(v[1], "upper"):
            errs.append(f"{path}: 'date_between' start is after end")
    else:  # pragma: no cover - Operator Literal prevents this
        errs.append(f"{path}: unsupported operator '{op}'")
    return errs


def _validate(expr, depth: int, path: str, counter: list[int], errs: list[str]) -> None:
    counter[0] += 1
    if depth > MAX_EXPRESSION_DEPTH:
        errs.append(f"{path}: expression depth exceeds {MAX_EXPRESSION_DEPTH}")
        return
    if counter[0] > MAX_EXPRESSION_NODES:
        if counter[0] == MAX_EXPRESSION_NODES + 1:
            errs.append(f"expression exceeds {MAX_EXPRESSION_NODES} nodes")
        return
    if isinstance(expr, Condition):
        errs.extend(_validate_condition(expr, path))
        return
    if isinstance(expr, AllOf):
        key, kids = "all", expr.all
    elif isinstance(expr, AnyOf):
        key, kids = "any", expr.any
    elif isinstance(expr, NotOf):
        key, kids = "not", [expr.not_]
    else:
        errs.append(f"{path}: unsupported expression node")
        return
    for i, child in enumerate(kids):
        _validate(child, depth + 1, f"{path}.{key}[{i}]" if key != "not" else f"{path}.not", counter, errs)


def validate_expression(expr, *, depth: int = 0) -> list[str]:
    """Static validation of one expression (typed model or dict). Returns error strings ([] = valid)."""
    try:
        parsed = parse_expression(expr)
    except ValidationError as e:
        return [f"invalid expression: {err.get('msg')} at {'.'.join(str(p) for p in err.get('loc', ()))}"
                for err in e.errors()[:10]]
    errs: list[str] = []
    _validate(parsed, depth, "$", [0], errs)
    return errs


def validate_rule(rule: Union[PolicyRule, dict]) -> list[str]:
    """Static validation of a rule (model or dict). Returns error strings ([] = valid)."""
    if not isinstance(rule, PolicyRule):
        try:
            rule = PolicyRule.model_validate(rule)
        except ValidationError as e:
            return [f"invalid rule: {err.get('msg')} at {'.'.join(str(p) for p in err.get('loc', ()))}"
                    for err in e.errors()[:10]]
    errs: list[str] = []
    if rule.schema_version != RULE_SCHEMA_VERSION:
        errs.append(f"unsupported schema_version '{rule.schema_version}'")
    if rule.evaluator_compatibility_version != EVALUATOR_VERSION:
        errs.append(f"unsupported evaluator_compatibility_version '{rule.evaluator_compatibility_version}'")
    if rule.applies_when is not None:
        errs.extend(f"applies_when{e[1:]}" if e.startswith("$") else f"applies_when: {e}"
                    for e in validate_expression(rule.applies_when))
    errs.extend(f"condition{e[1:]}" if e.startswith("$") else e for e in validate_expression(rule.condition))
    if rule.effective_from and rule.effective_until and rule.effective_until < rule.effective_from:
        errs.append("effective_until is before effective_from")
    return errs


# -------------------------------------------------------------------------------------------- evaluation
def _cmp_numeric(a: Decimal, b: Decimal, op: str) -> bool:
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]


def _eval_condition(c: Condition, facts: FactSnapshot) -> Optional[bool]:
    present = c.field in facts.values
    if c.op == "exists":
        return present
    if c.op == "is_missing":
        return not present
    if not present:
        return None
    ftype = facts.types.get(c.field, FACT_TYPES.get(c.field))
    if ftype is None:
        raise RuleEvaluationError(f"unknown field '{c.field}'")
    if c.value_type != ftype:
        raise RuleEvaluationError(f"type mismatch: rule uses {c.value_type}, fact '{c.field}' is {ftype}")
    fv = facts.values[c.field]
    op, v = c.op, c.value
    if ftype == "money":
        fact_cur = facts.currencies.get(c.field) or FACT_CURRENCIES.get(c.field)
        if not c.currency or c.currency != fact_cur:
            raise RuleEvaluationError(f"currency mismatch on '{c.field}': rule {c.currency}, fact {fact_cur}")

    if ftype in NUMERIC_TYPES:
        if ftype == "integer" and not _is_int(fv):
            raise RuleEvaluationError(f"fact '{c.field}' is not an integer")
        a = _to_decimal(fv, f"fact '{c.field}'")
        if op in ("eq", "ne", "gt", "gte", "lt", "lte"):
            if isinstance(v, list):
                raise RuleEvaluationError(f"operator '{op}' needs a single value")
            if ftype == "integer" and not _is_int(v):
                raise RuleEvaluationError("integer comparison needs an integer value")
            b = _to_decimal(v, "rule value")
            if op == "eq":
                return a == b
            if op == "ne":
                return a != b
            return _cmp_numeric(a, b, op)
        if op == "between":
            if not (isinstance(v, list) and len(v) == 2):
                raise RuleEvaluationError("'between' needs two bounds")
            lo, hi = _to_decimal(v[0], "lower bound"), _to_decimal(v[1], "upper bound")
            return lo <= a <= hi
        if op in ("in", "not_in") and ftype == "integer":
            if not (isinstance(v, list) and all(_is_int(x) for x in v)):
                raise RuleEvaluationError(f"'{op}' needs a list of integers")
            hit = int(a) in set(v)
            return hit if op == "in" else not hit
        raise RuleEvaluationError(f"operator '{op}' not supported for {ftype}")

    if ftype == "date":
        a = _to_date(fv, f"fact '{c.field}'")
        if op in ("eq", "ne"):
            r = a == _to_date(v, "rule value")
            return r if op == "eq" else not r
        if op == "date_before":
            return a < _to_date(v, "rule value")
        if op == "date_after":
            return a > _to_date(v, "rule value")
        if op == "date_between":
            if not (isinstance(v, list) and len(v) == 2):
                raise RuleEvaluationError("'date_between' needs two dates")
            return _to_date(v[0], "start") <= a <= _to_date(v[1], "end")
        raise RuleEvaluationError(f"operator '{op}' not supported for dates")

    if ftype == "boolean":
        if not isinstance(fv, bool) or not isinstance(v, bool):
            raise RuleEvaluationError("boolean comparison needs boolean values")
        if op == "eq":
            return fv == v
        if op == "ne":
            return fv != v
        raise RuleEvaluationError(f"operator '{op}' not supported for booleans")

    if ftype == "string":
        if not isinstance(fv, str):
            raise RuleEvaluationError(f"fact '{c.field}' is not a string")
        if op in ("eq", "ne"):
            if not isinstance(v, str):
                raise RuleEvaluationError("string comparison needs a string value")
            return (fv == v) if op == "eq" else (fv != v)
        if op in ("in", "not_in"):
            if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise RuleEvaluationError(f"'{op}' needs a list of strings")
            hit = fv in set(v)
            return hit if op == "in" else not hit
        raise RuleEvaluationError(f"operator '{op}' not supported for strings")

    if ftype == "string_list":
        if not (isinstance(fv, list) and all(isinstance(x, str) for x in fv)):
            raise RuleEvaluationError(f"fact '{c.field}' is not a string list")
        if op == "contains_any":
            if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                raise RuleEvaluationError("'contains_any' needs a list of strings")
            have = {x.lower() for x in fv}
            return any(x.lower() in have for x in v)
        raise RuleEvaluationError(f"operator '{op}' not supported for string lists")

    raise RuleEvaluationError(f"unsupported fact type '{ftype}'")  # pragma: no cover


def _eval(expr, facts: FactSnapshot, missing: set[str], refs: set[str]) -> Optional[bool]:
    if isinstance(expr, Condition):
        refs.add(expr.field)
        r = _eval_condition(expr, facts)
        if r is None:
            missing.add(expr.field)
        return r
    if isinstance(expr, NotOf):
        r = _eval(expr.not_, facts, missing, refs)
        return None if r is None else not r
    if isinstance(expr, (AllOf, AnyOf)):
        vals = [_eval(child, facts, missing, refs) for child in _children(expr)]  # no short-circuit: full refs
        if isinstance(expr, AllOf):
            if any(x is False for x in vals):
                return False
            return None if any(x is None for x in vals) else True
        if any(x is True for x in vals):
            return True
        return None if any(x is None for x in vals) else False
    raise RuleEvaluationError("unsupported expression node")


def evaluate_expression(expr, facts: FactSnapshot) -> tuple[Optional[bool], list[str], list[str]]:
    """Three-valued evaluation -> (True/False/None, missing fact names, referenced fact names).

    ``None`` means the result is unknown because a referenced fact is missing (Kleene logic: ``all`` with a
    False child is False, ``any`` with a True child is True). Raises ``RuleEvaluationError`` on type/currency
    problems and pydantic ``ValidationError`` on malformed dict input.
    """
    parsed = parse_expression(expr)
    missing: set[str] = set()
    refs: set[str] = set()
    result = _eval(parsed, facts, missing, refs)
    if result is not None:
        missing = set()
    return result, sorted(missing), sorted(refs)


def _as_utc_date(now: dt.datetime) -> dt.date:
    if now.tzinfo is not None:
        now = now.astimezone(dt.timezone.utc)
    return now.date()


def evaluate_rule(rule: PolicyRule, facts: FactSnapshot, policy_set_version: int, now: dt.datetime,
                  as_of: dt.date | None = None) -> RuleResult:
    """Evaluate one rule. Never raises; never passes on error.

    ``as_of`` (effective-date check) defaults to fact ``request.event_date``, else ``now`` as a UTC date.
    Status mapping: outside effective window / applies_when False -> not_applicable; missing facts ->
    review_required (mandatory) or not_applicable (with missing_fields); condition True -> fail (blocked),
    review_required (review_required / needs_clarification) or pass (pass); condition False -> pass;
    invalid rule or evaluation problem -> evaluation_error.
    """
    base = dict(rule_id=rule.rule_id, rule_version=rule.rule_version, policy_set_version=policy_set_version,
                severity=rule.severity, evidence_refs=list(rule.evidence_refs), evaluated_at=now)
    refs: list[str] = []
    try:
        errors = validate_rule(rule)
        if errors:
            return RuleResult(**base, status="evaluation_error", decision="review_required", reason_code=ERROR_CODE,
                              explanation="Rule failed validation and was not evaluated; manual review required.",
                              errors=errors, fact_refs=sorted(set(expression_fields(rule.condition)) | (
                                  set(expression_fields(rule.applies_when)) if rule.applies_when else set())))
        if as_of is None:
            ev = facts.values.get("request.event_date")
            as_of = _to_date(ev, "event date") if ev else _as_utc_date(now)
        if rule.effective_from and as_of < rule.effective_from:
            return RuleResult(**base, status="not_applicable", explanation="Rule is not yet effective on "
                              f"{as_of.isoformat()} (effective from {rule.effective_from.isoformat()}).",
                              warnings=[f"rule not effective on {as_of.isoformat()}"])
        if rule.effective_until and as_of > rule.effective_until:
            return RuleResult(**base, status="not_applicable", explanation="Rule expired before "
                              f"{as_of.isoformat()} (effective until {rule.effective_until.isoformat()}).",
                              warnings=[f"rule not effective on {as_of.isoformat()}"])

        missing: list[str] = []
        if rule.applies_when is not None:
            scope, scope_missing, scope_refs = evaluate_expression(rule.applies_when, facts)
            refs = scope_refs
            if scope is False:
                return RuleResult(**base, status="not_applicable", fact_refs=refs,
                                  explanation="Rule scope (applies_when) does not match this request.")
            if scope is None:
                missing = scope_missing
        if not missing:
            cond, cond_missing, cond_refs = evaluate_expression(rule.condition, facts)
            refs = sorted(set(refs) | set(cond_refs))
            if cond is None:
                missing = cond_missing
        if missing:
            if rule.mandatory:
                return RuleResult(**base, status="review_required", decision="review_required",
                                  reason_code=MISSING_CODE, fact_refs=refs, missing_fields=missing,
                                  explanation="Mandatory rule could not be evaluated because required facts are "
                                              f"missing ({', '.join(missing)}); manual review required.")
            return RuleResult(**base, status="not_applicable", fact_refs=refs, missing_fields=missing,
                              warnings=[f"missing facts: {', '.join(missing)}"],
                              explanation="Rule not evaluated: required facts are missing "
                                          f"({', '.join(missing)}).")
        if cond:
            decision = rule.outcome.decision
            status = {"blocked": "fail", "review_required": "review_required",
                      "needs_clarification": "review_required", "pass": "pass"}[decision]
            return RuleResult(**base, status=status, decision=decision, reason_code=rule.outcome.reason_code,
                              fact_refs=refs, explanation=rule.outcome.message)
        return RuleResult(**base, status="pass", fact_refs=refs,
                          explanation="Rule condition not met for this request.")
    except Exception as exc:  # any failure is an evaluation_error, never a pass
        msg = str(exc) if isinstance(exc, RuleEvaluationError) else f"{type(exc).__name__}: evaluation failed"
        return RuleResult(**base, status="evaluation_error", decision="review_required", reason_code=ERROR_CODE,
                          fact_refs=refs, errors=[msg[:300]],
                          explanation="Rule could not be evaluated safely; manual review required.")


# ------------------------------------------------------------------------------------------- aggregation
def _ref(r: RuleResult) -> str:
    return f"{r.rule_id}@{r.rule_version}"


def aggregate(results: list[RuleResult], coverage: list[CoverageItem] | None = None) -> dict:
    """Deterministic aggregation.

    Returns ``{"overall", "reason_codes", "blocking_rule_refs", "review_rule_refs", "clarification_rule_refs",
    "error_rule_refs", "coverage_gaps"}`` (lists sorted, unique). Precedence: blocked > needs_clarification >
    review_required > pass. All refs/codes are retained regardless of the overall status. evaluation_error
    results and missing_coverage / evaluation_error coverage force at least review_required.
    """
    blocking: set[str] = set()
    review: set[str] = set()
    clar: set[str] = set()
    errors: set[str] = set()
    codes: set[str] = set()
    gaps: set[str] = set()
    for r in results or []:
        ref = _ref(r)
        if r.status == "fail":
            if r.decision == "blocked":
                blocking.add(ref)
            else:  # unexpected combination: never weaker than review
                review.add(ref)
            if r.reason_code:
                codes.add(r.reason_code)
        elif r.status == "review_required":
            (clar if r.decision == "needs_clarification" else review).add(ref)
            if r.reason_code:
                codes.add(r.reason_code)
        elif r.status == "evaluation_error":
            errors.add(ref)
            review.add(ref)
            codes.add(r.reason_code or ERROR_CODE)
    for c in coverage or []:
        if c.status in ("missing_coverage", "evaluation_error"):
            gaps.add(c.area)
            codes.add(COVERAGE_MISSING_CODE if c.status == "missing_coverage" else ERROR_CODE)
            review.update(c.rule_refs)
    if blocking:
        overall = "blocked"
    elif clar:
        overall = "needs_clarification"
    elif review or gaps:
        overall = "review_required"
    else:
        overall = "pass"
    return {"overall": overall, "reason_codes": sorted(codes), "blocking_rule_refs": sorted(blocking),
            "review_rule_refs": sorted(review), "clarification_rule_refs": sorted(clar),
            "error_rule_refs": sorted(errors), "coverage_gaps": sorted(gaps)}


# ------------------------------------------------------------------------------------------------ hashes
def canonical_rule_hash(rule: Union[PolicyRule, dict]) -> str:
    """sha256 of the rule's canonical JSON, excluding lifecycle/audit fields (``LIFECYCLE_FIELDS``)."""
    if not isinstance(rule, PolicyRule):
        rule = PolicyRule.model_validate(rule)
    data = rule.model_dump(mode="json", by_alias=True, exclude=set(LIFECYCLE_FIELDS))
    return canonical_hash(data)


# ------------------------------------------------------------------------------------------- seed bundle
SEED_PATH = Path(__file__).resolve().parents[2] / "data" / "policy_rules_seed.json"


def load_seed_bundle(path=None) -> tuple[str, list[PolicyRule]]:
    """Load ``data/policy_rules_seed.json`` -> (bundle_version, [PolicyRule candidates])."""
    data = json.loads(Path(path or SEED_PATH).read_text(encoding="utf-8"))
    return str(data["bundle_version"]), [PolicyRule.model_validate(r) for r in data["rules"]]


# --------------------------------------------------------------------------------------------- rendering
_OP_TEXT = {"eq": "=", "ne": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "in": "in", "not_in": "not in",
            "contains_any": "contains any of", "between": "between", "date_before": "before",
            "date_after": "after", "date_between": "between"}


def describe_expression(expr) -> str:
    """Readable text for an expression (UI / explanations)."""
    e = parse_expression(expr)
    if isinstance(e, Condition):
        if e.op == "exists":
            return f"{e.field} is present"
        if e.op == "is_missing":
            return f"{e.field} is missing"
        v = e.value
        if isinstance(v, list):
            v = (" and ".join(str(x) for x in v) if e.op in ("between", "date_between")
                 else "[" + ", ".join(str(x) for x in v) + "]")
        elif isinstance(v, bool):
            v = "true" if v else "false"
        suffix = f" {e.currency}" if e.currency else (f" {e.unit}" if e.unit else "")
        return f"{e.field} {_OP_TEXT[e.op]} {v}{suffix}"
    if isinstance(e, NotOf):
        return f"NOT ({describe_expression(e.not_)})"
    joiner = " AND " if isinstance(e, AllOf) else " OR "
    return "(" + joiner.join(describe_expression(c) for c in _children(e)) + ")"
