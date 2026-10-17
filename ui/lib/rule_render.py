"""Read-only renderers for the Phase 3 dynamic policy engine (typed rules, rule-engine results, retrieval mode).

Nothing here decides anything: statuses are shown exactly as the backend computed them, and the four overall
rule-engine outcomes (pass / review_required / blocked / needs_clarification) are always rendered distinctly.
"""
from __future__ import annotations

import json
from typing import Any, Optional

import streamlit as st

OP_TEXT = {
    "eq": "=", "ne": "≠", "gt": ">", "gte": "≥", "lt": "<", "lte": "≤",
    "in": "in", "not_in": "not in", "contains_any": "contains any of",
    "between": "between", "date_before": "before", "date_after": "after", "date_between": "between",
}
MAX_RENDER_DEPTH = 8

RULE_STATUSES = ["candidate", "needs_review", "validated", "approved", "rejected", "superseded", "expired"]
REVIEWABLE_STATUSES = {"candidate", "needs_review", "validated"}
RULE_STATUS_BADGE = {
    "candidate": "🆕 candidate", "needs_review": "🟠 needs review", "validated": "☑️ validated",
    "approved": "✅ approved", "rejected": "⛔ rejected", "superseded": "↪️ superseded", "expired": "⌛ expired",
}
RULE_SEVERITY_BADGE = {"info": "ℹ️ info", "review": "🟠 review", "block": "⛔ block"}
DECISION_TEXT = {"pass": "pass", "review_required": "review required", "blocked": "blocked",
                 "needs_clarification": "needs clarification"}
RESULT_STATUS_BADGE = {
    "pass": "✅ pass", "fail": "❌ fail", "review_required": "🟠 review required",
    "not_applicable": "➖ not applicable", "evaluation_error": "⚠️ evaluation error",
}
COVERAGE_BADGE = {"covered": "✅ covered", "not_applicable": "➖ not applicable",
                  "missing_coverage": "❔ missing coverage", "evaluation_error": "⚠️ evaluation error"}
ENGINE_OVERALL = {
    "pass": ("✅ Rules: PASS — every applicable active rule passed", "success"),
    "review_required": ("🟠 Rules: REVIEW REQUIRED — a human must review the rule results", "warning"),
    "blocked": ("⛔ Rules: BLOCKED — an approved policy rule blocks this request", "error"),
    "needs_clarification": ("✏️ Rules: NEEDS CLARIFICATION — the requester must provide more information", "info"),
}
STALE_POLICY = "⚠️ Policy set changed since this request was evaluated — re-run the ticket."
RETRIEVAL_BADGE = {
    "hybrid": "🧠 Semantic hybrid",
    "hybrid_local_hash": "#️⃣ Local hashed vectors (not semantic)",
    "keyword_only_degraded": "⚠️ Keyword only (degraded)",
}
LOCAL_HASH_NOTE = ("Retrieval used local hashed vectors plus keywords — this is not semantic search; "
                   "evidence may be incomplete.")
SUSPICIOUS = "suspicious_instruction"


# --------------------------------------------------------------------------- formatting helpers
def rule_badge(status: Optional[str]) -> str:
    return RULE_STATUS_BADGE.get(status or "", f"❔ {(status or 'unknown').replace('_', ' ')}")


def rule_ref(rule: dict) -> str:
    return f"{rule.get('rule_id', '?')}@{rule.get('rule_version', '?')}"


def retrieval_badge(mode: Optional[str]) -> str:
    return RETRIEVAL_BADGE.get(mode or "", f"❔ {mode or 'unknown'}")


def _fmt_scalar(v: Any, currency: Optional[str]) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    text = str(v)
    return f"{currency} {text}" if currency else text


def _fmt_value(cond: dict) -> str:
    v = cond.get("value")
    cur = cond.get("currency")
    unit = cond.get("unit")
    op = cond.get("op")
    if isinstance(v, list):
        if op in ("between", "date_between") and len(v) == 2:
            out = f"{_fmt_scalar(v[0], cur)} and {_fmt_scalar(v[1], cur)}"
        else:
            out = "[" + ", ".join(_fmt_scalar(x, None) for x in v) + "]"
    elif v is None:
        out = "null"
    else:
        out = _fmt_scalar(v, cur)
    return f"{out} {unit}" if unit else out


def condition_text(expr: Any, _depth: int = 0) -> str:
    """Render a typed Expression (Condition / all / any / not) as readable text.

    e.g. ``request.attendees ≥ 150 AND NOT(request.currency = INR)``.
    """
    if _depth > MAX_RENDER_DEPTH:
        return "…"
    if not isinstance(expr, dict):
        return "—" if expr is None else str(expr)
    if "field" in expr:
        op = expr.get("op")
        field = expr.get("field")
        if op == "exists":
            return f"{field} exists"
        if op == "is_missing":
            return f"{field} is missing"
        return f"{field} {OP_TEXT.get(op, str(op))} {_fmt_value(expr)}"
    for key, joiner in (("all", " AND "), ("any", " OR ")):
        if isinstance(expr.get(key), list):
            parts = []
            for child in expr[key]:
                text = condition_text(child, _depth + 1)
                group = child.get("all") or child.get("any") if isinstance(child, dict) else None
                if isinstance(group, list) and len(group) > 1:
                    text = f"({text})"
                parts.append(text)
            return joiner.join(parts) if parts else "—"
    inner = expr.get("not", expr.get("not_"))
    if inner is not None:
        return f"NOT({condition_text(inner, _depth + 1)})"
    return json.dumps(expr, sort_keys=True)


def evidence_ref_label(ev: dict) -> str:
    """'📄 filename · v2 · p. 4 · § 3.1' — page shows 'unavailable' when the backend has none."""
    name = ev.get("filename") or ev.get("document_id") or "document"
    parts = [f"📄 {name}"]
    if ev.get("document_version") is not None:
        parts.append(f"v{ev['document_version']}")
    page = ev.get("page_number")
    parts.append(f"p. {page}" if page is not None else "page unavailable")
    if ev.get("section"):
        parts.append(f"§ {ev['section']}")
    return " · ".join(parts)


def _warning_list(rule: dict) -> list[str]:
    out = []
    for w in rule.get("warnings") or []:
        if isinstance(w, dict):
            w = w.get("code") or w.get("message") or json.dumps(w, sort_keys=True)
        out.append(str(w))
    return out


def render_warnings(warnings: list) -> None:
    for w in warnings:
        text = str(w)
        if SUSPICIOUS in text:
            st.error(f"🚩 Suspicious instruction detected in the source text (`{text}`). Document instructions "
                     "are never obeyed — review the excerpt carefully before approving.")
        else:
            st.warning(f"⚠️ {text}")


# --------------------------------------------------------------------------- rule detail
def render_rule_detail(rule: dict) -> None:
    st.markdown(f"### {rule.get('name') or rule.get('rule_id')}")
    st.markdown(f"`{rule_ref(rule)}` · {rule_badge(rule.get('status'))} · severity "
                f"{RULE_SEVERITY_BADGE.get(rule.get('severity'), rule.get('severity') or '—')} · "
                f"{'**mandatory**' if rule.get('mandatory') else 'not mandatory'} · source "
                f"{str(rule.get('source') or '—').replace('_', ' ')}")
    if rule.get("description"):
        st.caption(str(rule["description"]))
    if rule.get("applies_when"):
        st.markdown(f"**Applies when:** `{condition_text(rule['applies_when'])}`")
    st.markdown(f"**Condition:** `{condition_text(rule.get('condition'))}`")
    outcome = rule.get("outcome") if isinstance(rule.get("outcome"), dict) else {}
    st.markdown(f"**Outcome when matched:** {DECISION_TEXT.get(outcome.get('decision'), outcome.get('decision') or '—')}"
                f" · reason `{outcome.get('reason_code') or '—'}`")
    if outcome.get("message"):
        st.markdown(f"> {outcome['message']}")
    with st.expander("Raw rule JSON (condition / outcome)"):
        st.code(json.dumps({"applies_when": rule.get("applies_when"), "condition": rule.get("condition"),
                            "outcome": rule.get("outcome")}, indent=2, ensure_ascii=False), language="json")

    meta = []
    conf = rule.get("extraction_confidence")
    if conf is not None:
        try:
            meta.append(f"confidence {float(conf):.2f}")
        except (TypeError, ValueError):
            meta.append(f"confidence {conf}")
    if rule.get("extraction_model"):
        meta.append(f"extraction model `{rule['extraction_model']}`")
    if rule.get("extraction_prompt_version"):
        meta.append(f"prompt `{rule['extraction_prompt_version']}`")
    meta.append(f"effective {rule.get('effective_from') or '…'} → {rule.get('effective_until') or '…'}")
    if rule.get("supersedes"):
        meta.append(f"supersedes `{rule['supersedes']}`")
    if rule.get("created_by"):
        meta.append(f"created by {rule['created_by']}" + (f" at {rule['created_at']}" if rule.get("created_at") else ""))
    if rule.get("approved_by"):
        meta.append(f"approved by {rule['approved_by']}")
    elif rule.get("reviewed_by"):
        meta.append(f"reviewed by {rule['reviewed_by']}")
    st.caption(" · ".join(meta))
    if rule.get("review_comment"):
        st.caption(f"Review comment: “{rule['review_comment']}”")

    render_warnings(_warning_list(rule))
    if rule.get("validation_errors"):
        st.error("**Validation errors** — this rule cannot be approved until fixed:\n\n"
                 + "\n".join(f"- {e}" for e in rule["validation_errors"]))
    if rule.get("conflict_refs"):
        st.warning("**Conflicts** — resolve before approval:\n\n"
                   + "\n".join(f"- `{c}`" for c in rule["conflict_refs"]))

    refs = [e for e in rule.get("evidence_refs") or [] if isinstance(e, dict)]
    st.markdown(f"**Source evidence ({len(refs)})**")
    if not refs:
        st.caption("No source evidence is attached to this rule.")
    for ev in refs:
        with st.container(border=True):
            st.markdown(f"**{evidence_ref_label(ev)}**")
            if ev.get("excerpt"):
                st.markdown("> " + str(ev["excerpt"]).replace("\n", " "))
            h = str(ev.get("content_hash") or "")
            st.caption(f"document `{ev.get('document_id', '?')}`"
                       + (f" · chunk `{ev['chunk_id']}`" if ev.get("chunk_id") else "")
                       + (f" · hash `{h[:12]}…`" if h else ""))


# --------------------------------------------------------------------------- rule engine evaluation
def _overall_box(overall: Optional[str]) -> None:
    text, kind = ENGINE_OVERALL.get(overall or "", (f"❔ Rules: {overall or 'unknown'}", "info"))
    {"success": st.success, "warning": st.warning, "error": st.error}.get(kind, st.info)(f"**Rule engine:** {text}")


def render_rule_engine(engine: Any, *, redacted: bool = False, stale_policy: bool = False) -> None:
    """Rule-engine section. ``redacted=True`` shows overall status and per-rule explanations only."""
    if not isinstance(engine, dict) or not engine:
        return
    st.markdown("#### ⚙️ Rule engine")
    if stale_policy and not redacted:
        st.warning(STALE_POLICY)
    _overall_box(engine.get("overall"))
    results = [r for r in engine.get("results") or [] if isinstance(r, dict)]
    if redacted:
        for r in results:
            if r.get("explanation"):
                st.markdown(f"- {RESULT_STATUS_BADGE.get(r.get('status'), r.get('status') or '—')} — "
                            f"{r['explanation']}")
        return
    if results:
        st.dataframe([
            {
                "Rule": rule_ref(r),
                "Status": RESULT_STATUS_BADGE.get(r.get("status"), r.get("status") or "—"),
                "Severity": RULE_SEVERITY_BADGE.get(r.get("severity"), r.get("severity") or "—"),
                "Decision": DECISION_TEXT.get(r.get("decision"), r.get("decision") or "—"),
                "Reason code": r.get("reason_code") or "—",
                "Explanation": r.get("explanation") or "—",
                "Missing fields": ", ".join(r.get("missing_fields") or []) or "—",
                "Errors": "; ".join(str(e) for e in r.get("errors") or []) or "—",
            }
            for r in results
        ], width="stretch", hide_index=True)
        for r in results:
            refs = [e for e in r.get("evidence_refs") or [] if isinstance(e, dict)]
            if refs:
                st.caption(f"{rule_ref(r)} evidence: " + "; ".join(evidence_ref_label(e) for e in refs))
    else:
        st.caption("No active rules were evaluated for this request.")
    coverage = [c for c in engine.get("coverage") or [] if isinstance(c, dict)]
    if coverage:
        st.markdown("**Coverage**")
        st.dataframe([{"Area": c.get("area") or "—",
                       "Status": COVERAGE_BADGE.get(c.get("status"), c.get("status") or "—"),
                       "Rules": ", ".join(c.get("rule_refs") or []) or "—",
                       "Detail": c.get("detail") or "—"} for c in coverage], width="stretch", hide_index=True)
    for label, key in (("Blocking rules", "blocking_rule_refs"), ("Review rules", "review_rule_refs"),
                       ("Clarification rules", "clarification_rule_refs")):
        if engine.get(key):
            st.caption(f"{label}: " + ", ".join(f"`{x}`" for x in engine[key]))
    if engine.get("reason_codes"):
        st.caption("Rule reason codes: " + ", ".join(f"`{c}`" for c in engine["reason_codes"]))
    h = str(engine.get("engine_hash") or "")
    st.caption(f"Policy set v{engine.get('policy_set_version', '—')} · evaluator "
               f"`{engine.get('evaluator_version') or '—'}` · engine `{h[:12] + '…' if h else '—'}`"
               + (f" · evaluated {engine['evaluated_at']}" if engine.get("evaluated_at") else ""))


def render_retrieval_mode(info: dict) -> None:
    """Accurate retrieval badge: semantic hybrid vs local hashed vectors vs keyword-only degraded."""
    mode = info.get("retrieval_mode")
    if not mode:
        return
    extra = []
    if info.get("embedding_provider") or info.get("embedding_model"):
        extra.append(f"embeddings {info.get('embedding_provider') or '?'}"
                     + (f"/{info['embedding_model']}" if info.get("embedding_model") else ""))
    if info.get("retrieval_config_version"):
        extra.append(f"config {info['retrieval_config_version']}")
    st.caption(f"Retrieval: **{retrieval_badge(mode)}** (`{mode}`)" + (" · " + " · ".join(extra) if extra else ""))
    if mode == "hybrid_local_hash":
        st.info(f"#️⃣ {LOCAL_HASH_NOTE}")
    if info.get("degradation_reason"):
        st.caption(f"Degradation reason: {info['degradation_reason']}")
