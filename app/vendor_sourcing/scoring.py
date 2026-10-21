"""Versioned, explainable quote scoring (Phase 2C-lite).

The score is EXPLANATORY ONLY. Eligibility gates (verification, currency, delivery, warranty, compliance, budget)
and the deterministic ranking (lowest total cost, then earliest delivery, then quote ID) are computed elsewhere and
are never changed by the score. Components are relative to the eligible quotes of one comparison:

- cost:     lowest eligible total / this total                (1.0 = cheapest)
- delivery: days delivered before the requested date / max     (1.0 = earliest; all 1.0 when no one is early)
- warranty: warranty months / max eligible warranty months     (1.0 = longest; all 1.0 when max is 0)

score = sum(weight_i * component_i), Decimal, rounded to 4 places. Weights must sum to exactly 1.0.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from app.data_loader import DATA_DIR

POLICY_FILE = "vendor_scoring_policy.json"
COMPONENTS = ("cost", "delivery", "warranty")
_Q = Decimal("0.0001")


class ScoringPolicyError(ValueError):
    """The scoring policy file is missing or invalid."""


def _policy_path(path: Optional[Path] = None) -> Path:
    return Path(path) if path is not None else DATA_DIR / POLICY_FILE


def load_policy(path: Optional[Path] = None) -> dict[str, Any]:
    """Load and validate the policy. Returns {"version", "weights": {name: Decimal}, "policy_hash"}."""
    p = _policy_path(path)
    try:
        raw_text = p.read_text(encoding="utf-8")
        raw = json.loads(raw_text, parse_float=Decimal, parse_int=Decimal)
    except (OSError, ValueError) as exc:
        raise ScoringPolicyError("Scoring policy could not be read.") from exc
    if not isinstance(raw, dict):
        raise ScoringPolicyError("Scoring policy must be a JSON object.")
    version = raw.get("version")
    if not isinstance(version, str) or not version.strip() or len(version) > 40:
        raise ScoringPolicyError("Scoring policy needs a non-empty string 'version'.")
    weights = raw.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise ScoringPolicyError("Scoring policy needs a 'weights' object.")
    unknown = set(weights) - set(COMPONENTS)
    if unknown:
        raise ScoringPolicyError("Unknown scoring components: " + ", ".join(sorted(unknown)))
    parsed: dict[str, Decimal] = {}
    for name in COMPONENTS:
        value = weights.get(name, Decimal("0"))
        if isinstance(value, bool) or not isinstance(value, Decimal):
            raise ScoringPolicyError(f"Weight '{name}' must be a number.")
        try:
            if not value.is_finite() or value < 0 or value > 1:
                raise ScoringPolicyError(f"Weight '{name}' must be between 0 and 1.")
        except InvalidOperation as exc:  # pragma: no cover - defensive
            raise ScoringPolicyError(f"Weight '{name}' is invalid.") from exc
        parsed[name] = value
    if sum(parsed.values(), Decimal("0")) != Decimal("1"):
        raise ScoringPolicyError("Scoring weights must sum to exactly 1.0.")
    canonical = json.dumps({"version": version, "weights": {k: str(v) for k, v in parsed.items()}},
                           sort_keys=True, separators=(",", ":"))
    return {"version": version, "weights": parsed,
            "policy_hash": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def _ratio(num: Decimal, den: Decimal) -> Decimal:
    if den <= 0:
        return Decimal("1")
    return min(Decimal("1"), max(Decimal("0"), num / den))


def score_rows(rows: list[dict], required_delivery: date, policy: dict[str, Any]) -> None:
    """Attach ``score`` to every eligible row in place (``None`` for ineligible rows)."""
    eligible = [r for r in rows if r.get("eligible")]
    if not eligible:
        for r in rows:
            r["score"] = None
        return
    totals = {r["quote_id"]: Decimal(r["total_cost"]) for r in eligible}
    early = {r["quote_id"]: max(0, (required_delivery - date.fromisoformat(r["delivery_date"])).days)
             for r in eligible}
    months = {r["quote_id"]: int(r.get("warranty_months") or 0) for r in eligible}
    min_total = min(totals.values())
    max_early = max(early.values())
    max_months = max(months.values())
    weights = policy["weights"]
    for r in rows:
        if not r.get("eligible"):
            r["score"] = None
            continue
        qid = r["quote_id"]
        components = {
            "cost": _ratio(min_total, totals[qid]) if totals[qid] > 0 else Decimal("1"),
            "delivery": Decimal("1") if max_early == 0 else _ratio(Decimal(early[qid]), Decimal(max_early)),
            "warranty": Decimal("1") if max_months == 0 else _ratio(Decimal(months[qid]), Decimal(max_months)),
        }
        total = sum((weights[k] * components[k] for k in COMPONENTS), Decimal("0"))
        r["score"] = {
            "total": str(total.quantize(_Q)),
            "components": {k: str(components[k].quantize(_Q)) for k in COMPONENTS},
            "weights": {k: str(weights[k]) for k in COMPONENTS},
            "raw": {"total_cost": r["total_cost"], "days_before_required": early[qid],
                    "warranty_months": months[qid]},
            "explanatory_only": True,
        }
