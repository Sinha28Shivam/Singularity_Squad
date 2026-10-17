"""Vendor matching agent: deterministic eligibility filtering and ranking (no LLM)."""
from __future__ import annotations

from app.agents.procurement import calculate_cost
from app.data_loader import load_json
from app.schemas import EventRequest, PolicyRules, Vendor, VendorEvaluation, VendorMatchResult


def load_vendors() -> list[Vendor]:
    return [Vendor(**v) for v in load_json("vendors.json")]


def _evaluate(v: Vendor, req: EventRequest, rules: PolicyRules) -> tuple[VendorEvaluation, list[str], float | None]:
    fails: list[str] = []
    oks: list[str] = []
    attendees = req.attendees or 0
    if rules.approved_vendor_only and not v.approved:
        fails.append("not an approved vendor (PROC-2.1)")
    else:
        oks.append("approved vendor")
    offers = req.service in v.services
    if not offers:
        fails.append(f"not offering {req.service}")
    if v.capacity < attendees:
        fails.append(f"over capacity ({v.capacity} < {attendees} attendees)")
    else:
        oks.append(f"capacity {v.capacity}")
    if req.date is None or req.date not in v.available_dates:
        fails.append(f"unavailable on {req.date.isoformat() if req.date else 'unspecified date'}")
    else:
        oks.append(f"available on {req.date.isoformat()}")
    if v.rate_per_person > rules.per_head_catering_cap:
        fails.append(f"priced above per-head cap (rate {v.rate_per_person:g} > cap {rules.per_head_catering_cap:g}, EVT-3.1)")
    else:
        oks.append(f"rate {v.rate_per_person:g} within cap {rules.per_head_catering_cap:g}")
    total = calculate_cost(v, attendees, req.service)[3] if offers else None
    eligible = not fails
    reasons = (oks + [f"rating {v.rating:g}"]) if eligible else fails
    return (
        VendorEvaluation(vendor_id=v.vendor_id, name=v.name, eligible=eligible, reasons=reasons, estimated_total=total),
        fails,
        total,
    )


def match_vendors(req: EventRequest, rules: PolicyRules, vendors: list[Vendor] | None = None) -> VendorMatchResult:
    vendors = load_vendors() if vendors is None else vendors
    evals: list[VendorEvaluation] = []
    fails_by_id: dict[str, list[str]] = {}
    totals: dict[str, float | None] = {}
    for v in vendors:
        ev, fails, total = _evaluate(v, req, rules)
        evals.append(ev)
        fails_by_id[v.vendor_id] = fails
        totals[v.vendor_id] = total

    eligible = [v for v in vendors if not fails_by_id[v.vendor_id]]
    # PROC-3.2: preferred first, then highest rating, then lowest total.
    ranked = sorted(eligible, key=lambda v: (not v.preferred, -v.rating, totals[v.vendor_id] or 0.0, v.vendor_id))

    if not ranked:
        return VendorMatchResult(
            selected=None,
            evaluations=evals,
            replanned=False,
            explanation=f"No eligible vendor for {req.service} on "
            f"{req.date.isoformat() if req.date else 'unspecified date'} for {req.attendees} attendees; "
            "human review required (PROC-2.1, PROC-3.2, EVT-3.1).",
        )

    selected = ranked[0]
    notes: list[str] = []
    budget = req.budget_limit
    if budget is not None and (totals[selected.vendor_id] or 0) > budget:
        within = [v for v in ranked if (totals[v.vendor_id] or 0) <= budget]
        if within:
            notes.append(
                f"Top-ranked {selected.name} (INR {totals[selected.vendor_id]:,.2f}) exceeds budget "
                f"INR {budget:,.2f}; chose {within[0].name} within budget."
            )
            selected = within[0]

    offering_approved = [v for v in vendors if req.service in v.services and v.approved]
    benchmarks: list[Vendor] = [v for v in vendors if v.preferred and req.service in v.services]
    if offering_approved:
        top_rated = max(offering_approved, key=lambda v: v.rating)
        if top_rated not in benchmarks:
            benchmarks.append(top_rated)
    rejected_bench = [b for b in benchmarks if fails_by_id[b.vendor_id]]
    replanned = bool(rejected_bench)

    parts: list[str] = []
    for b in rejected_bench:
        label = "Preferred vendor" if b.preferred else "Highest-rated approved vendor"
        parts.append(f"{label} {b.name} is {'; '.join(fails_by_id[b.vendor_id])}.")
    parts.extend(notes)
    parts.append(
        f"Selected {selected.name} (approved, capacity {selected.capacity}, rating {selected.rating:g}, "
        f"estimated total INR {totals[selected.vendor_id]:,.2f}) per PROC-3.2."
    )
    return VendorMatchResult(selected=selected, evaluations=evals, replanned=replanned, explanation=" ".join(parts))
