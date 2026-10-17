"""Read-only renderers for Policy RAG evidence (admin/approver full view, requester redacted view).

Nothing here decides anything. Outcomes and finding statuses are shown exactly as the backend computed
them; ``pass``, ``fail``, ``unknown``, ``conflict``, ``not_applicable``, ``review_required`` and
``hard_block`` are always rendered distinctly (never collapsed into a single green/red status).
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

import streamlit as st

from ui.lib.api import ApiError, api_get, friendly_error
from ui.lib.rule_render import render_retrieval_mode, render_rule_engine

OUTCOME_BADGE = {
    "pass": "✅ PASS — no policy issue found in the retrieved evidence",
    "review_required": "🟠 REVIEW REQUIRED — a human must review the policy findings",
    "hard_block": "⛔ HARD BLOCK — a deterministic policy rule blocks this request",
    "unknown": "❔ UNKNOWN — insufficient policy evidence to decide",
}
FINDING_STATUS_BADGE = {
    "pass": "✅ pass",
    "fail": "❌ fail",
    "unknown": "❔ unknown",
    "conflict": "⚔️ conflict",
    "not_applicable": "➖ not applicable",
}
SEVERITY_BADGE = {
    "hard_block": "⛔ hard block",
    "review_required": "🟠 review required",
    "warning": "⚠️ warning",
    "info": "ℹ️ info",
}
SEVERITY_ORDER = ["hard_block", "review_required", "warning", "info"]
RATE_STATUS_BADGE = {
    "pass": "✅ pass",
    "fail": "❌ fail",
    "unknown": "❔ unknown",
    "conflict": "⚔️ conflict",
    "needs_review": "🟠 needs review",
}
DEGRADED_WARNING = ("⚠️ Retrieval ran in keyword-only degraded mode (semantic search unavailable) — "
                    "evidence may be incomplete.")
REDACTED_NOTE = "Summary only — policy document text is visible to operations staff and approvers."


def outcome_badge(outcome: Optional[str]) -> str:
    return OUTCOME_BADGE.get(outcome or "", f"❔ {(outcome or 'unknown').replace('_', ' ')}")


def finding_status_badge(status: Optional[str]) -> str:
    return FINDING_STATUS_BADGE.get(status or "", f"❔ {status or 'unknown'}")


def severity_badge(severity: Optional[str]) -> str:
    return SEVERITY_BADGE.get(severity or "", severity or "—")


def citation_label(ev: dict) -> str:
    """'filename · v2 (label) · p. 4 · §section' — metadata only, never document text."""
    parts = [f"📄 {ev.get('filename') or ev.get('document_id') or 'document'}"]
    ver = ev.get("version")
    label = ev.get("version_label")
    if ver is not None:
        parts.append(f"v{ver}" + (f" ({label})" if label else ""))
    elif label:
        parts.append(f"version {label}")
    if ev.get("page_number") is not None:
        parts.append(f"p. {ev['page_number']}")
    if ev.get("section"):
        parts.append(f"§ {ev['section']}")
    return " · ".join(parts)


# --------------------------------------------------------------------------- fetch / normalize
def normalize_evaluation(data: Any) -> Optional[dict]:
    """Accept a PolicyEvaluationResult, an EvidenceSnapshot (``{"evaluation": {...}}``) or a redacted view."""
    if not isinstance(data, dict) or not data:
        return None
    inner = data.get("evaluation")
    if isinstance(inner, dict):
        merged = dict(inner)
        for k in ("snapshot_id", "run_count", "ticket_id", "redacted", "stale_against_approval", "rule_engine",
                  "stale_policy", "retrieval_mode", "embedding_provider", "embedding_model", "embedding_available",
                  "degradation_reason", "retrieval_config_version"):
            if k in data and k not in merged:
                merged[k] = data[k]
        return merged
    return data


def fetch_ticket_evidence(ticket_id: str) -> tuple[Optional[dict], Optional[str]]:
    """GET /api/requests/{ticket_id}/evidence → (evaluation | None, error | None). 404 means no evidence yet."""
    try:
        data = api_get(f"/api/requests/{quote(str(ticket_id), safe='')}/evidence")
    except ApiError as exc:
        if exc.status_code == 404:
            return None, None
        return None, friendly_error(exc)
    except Exception as exc:
        return None, friendly_error(exc)
    return normalize_evaluation(data), None


def embedded_or_fetch(view: dict, key: str, fetch: bool = True) -> tuple[Optional[dict], Optional[str]]:
    """Use the evaluation embedded in a ticket view under ``key`` if the backend provides it, else fetch."""
    if key in view:
        ev = normalize_evaluation(view.get(key))
    else:
        tid = view.get("ticket_id")
        if not tid or not fetch:
            ev = None
        else:
            ev, err = fetch_ticket_evidence(tid)
            if err:
                return None, err
    if isinstance(view.get("rule_engine"), dict):
        ev = dict(ev or {})
        ev.setdefault("rule_engine", view["rule_engine"])
    return ev, None


# --------------------------------------------------------------------------- full view
def _outcome_box(outcome: Optional[str]) -> None:
    text = f"**Policy outcome:** {outcome_badge(outcome)}"
    {"pass": st.success, "review_required": st.warning, "hard_block": st.error}.get(outcome or "", st.info)(text)


def _evidence_index(ev_list: list) -> dict:
    return {e.get("evidence_id"): e for e in ev_list if isinstance(e, dict) and e.get("evidence_id")}


def render_findings(findings: list, evidence_by_id: dict, *, redacted: bool = False) -> None:
    if not findings:
        st.caption("No policy findings recorded.")
        return
    order = {s: i for i, s in enumerate(SEVERITY_ORDER)}
    groups: dict = {}
    for f in findings:
        groups.setdefault(f.get("severity") or "info", []).append(f)
    for sev in sorted(groups, key=lambda s: order.get(s, len(order))):
        st.markdown(f"**{severity_badge(sev)}** — {len(groups[sev])} finding(s)")
        for f in groups[sev]:
            line = f"- {finding_status_badge(f.get('status'))} · {severity_badge(f.get('severity'))}"
            if f.get("category") and not redacted:
                line += f" · {str(f['category']).replace('_', ' ')}"
            line += f" — {f.get('summary') or '(no summary)'}"
            if f.get("source") == "llm":
                line += " _(AI advisory — never decisive)_"
            st.markdown(line)
            if redacted:
                labels = [citation_label(c) for c in f.get("citations") or [] if isinstance(c, dict)]
                if labels:
                    st.caption("Sources: " + "; ".join(labels))
                continue
            details = []
            if f.get("reason"):
                details.append(f"Reason: {f['reason']}")
            if f.get("required_action"):
                details.append(f"Required action: {f['required_action']}")
            if f.get("rule_id"):
                details.append(f"Rule `{f['rule_id']}`")
            cited = [evidence_by_id[i] for i in f.get("evidence_ids") or [] if i in evidence_by_id]
            if cited:
                details.append("Cites: " + "; ".join(citation_label(c) for c in cited))
            if details:
                st.caption(" · ".join(details))


def render_citations(evidence: list) -> None:
    if not evidence:
        st.caption("No supporting citations were retrieved.")
        return
    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        with st.container(border=True):
            st.markdown(f"**{citation_label(ev)}**")
            if ev.get("excerpt"):
                st.markdown("> " + str(ev["excerpt"]).replace("\n", " "))
            meta = []
            if ev.get("relevance_score") is not None:
                try:
                    meta.append(f"score {float(ev['relevance_score']):.3f}")
                except (TypeError, ValueError):
                    pass
            if ev.get("document_type"):
                meta.append(str(ev["document_type"]).replace("_", " "))
            if ev.get("vendor_key"):
                meta.append(f"vendor `{ev['vendor_key']}`")
            if ev.get("effective_from") or ev.get("effective_to"):
                meta.append(f"effective {ev.get('effective_from') or '…'} → {ev.get('effective_to') or '…'}")
            if meta:
                st.caption(" · ".join(meta))


def render_rate_validation(rv: Optional[dict]) -> None:
    if not isinstance(rv, dict) or not rv:
        st.caption("No rate-card validation applies to this request.")
        return
    cur = rv.get("currency") or "INR"
    rows = [
        ("Validation status", RATE_STATUS_BADGE.get(rv.get("validation_status"), rv.get("validation_status"))),
        ("Reason", rv.get("reason")),
        ("Quoted unit rate", f"{cur} {rv.get('quoted_unit_rate')}" if rv.get("quoted_unit_rate") else None),
        ("Expected unit rate", f"{cur} {rv.get('expected_unit_rate')}" if rv.get("expected_unit_rate") else None),
        ("Quantity", rv.get("quantity")),
        ("Quoted total", f"{cur} {rv.get('quoted_total')}" if rv.get("quoted_total") else None),
        ("Expected total", f"{cur} {rv.get('expected_total')}" if rv.get("expected_total") else None),
        ("Difference", (f"{cur} {rv.get('difference_amount')}" if rv.get("difference_amount") else None)
         if not rv.get("difference_percentage") else
         f"{cur} {rv.get('difference_amount')} ({rv.get('difference_percentage')}%)"),
        ("Rate entry", rv.get("rate_id")),
        ("Candidate entries", ", ".join(rv.get("candidate_rate_ids") or []) or None),
        ("Source", (f"{rv.get('source_document_id') or '?'} · p. {rv.get('source_page')}"
                    if rv.get("source_page") is not None else rv.get("source_document_id"))),
        ("Rate-card version", rv.get("rate_card_version")),
    ]
    st.dataframe([{"Field": k, "Value": "—" if v in (None, "") else str(v)} for k, v in rows],
                 width="stretch", hide_index=True)


def render_policy_evidence(evaluation: Optional[dict], *, redacted: bool = False,
                           heading: str = "📑 Policy evidence", stale_policy: Optional[bool] = None) -> None:
    """Policy evidence panel. ``redacted=True`` shows finding summaries and citation labels only."""
    st.subheader(heading)
    ev = normalize_evaluation(evaluation)
    if ev is None:
        st.caption("No policy evaluation is stored for this request yet.")
        return
    redacted = redacted or bool(ev.get("redacted"))
    engine_only = "outcome" not in ev and isinstance(ev.get("rule_engine"), dict)
    if not engine_only:
        _outcome_box(ev.get("outcome"))
    if ev.get("retrieval_mode") == "keyword_only_degraded":
        st.warning(DEGRADED_WARNING)
    if not redacted:
        render_retrieval_mode(ev)
    render_rule_engine(ev.get("rule_engine"), redacted=redacted,
                       stale_policy=bool(ev.get("stale_policy")) if stale_policy is None else stale_policy)
    if engine_only:
        return
    findings = [f for f in ev.get("findings") or [] if isinstance(f, dict)]

    if redacted:
        st.caption(REDACTED_NOTE)
        render_findings(findings, {}, redacted=True)
        labels = []
        for c in (ev.get("citations") or ev.get("evidence") or []):
            if isinstance(c, dict):
                lab = citation_label(c)
                if lab not in labels:
                    labels.append(lab)
        if labels:
            st.markdown("**Sources consulted**")
            for lab in labels:
                st.markdown(f"- {lab}")
        return

    evidence = [e for e in ev.get("evidence") or [] if isinstance(e, dict)]
    by_id = _evidence_index(evidence)
    st.markdown("**Findings (grouped by severity)**")
    render_findings(findings, by_id)
    if ev.get("missing_evidence"):
        st.markdown("**Missing evidence**")
        for m in ev["missing_evidence"]:
            st.markdown(f"- ❔ {m}")
    if ev.get("conflicts"):
        st.markdown("**Conflicting evidence**")
        for c in ev["conflicts"]:
            st.markdown(f"- ⚔️ {c}")
    if ev.get("approval_reason_codes"):
        st.caption("Adds approval reasons: " + ", ".join(f"`{c}`" for c in ev["approval_reason_codes"]))
    st.markdown("**Rate-card validation**")
    render_rate_validation(ev.get("rate_validation"))
    st.markdown(f"**Supporting citations ({len(evidence)})**")
    render_citations(evidence)
    if ev.get("summary"):
        st.markdown("**AI explanation (advisory — does not change the outcome)**")
        st.caption(str(ev["summary"]))
    docs = [d for d in ev.get("documents_used") or [] if isinstance(d, dict)]
    if docs:
        st.caption("Documents used: " + "; ".join(
            f"{d.get('document_id', '?')} v{d.get('version', '?')}"
            + (f" ({d['version_label']})" if d.get("version_label") else "") for d in docs))
    h = str(ev.get("evaluation_hash") or "")
    st.caption(f"Evaluation `{h[:12] + '…' if h else '—'}` · rule set {ev.get('rule_set_version') or '—'} · "
               f"retrieval {ev.get('retrieval_mode') or '—'} · run {ev.get('run_count', '—')}"
               + (f" · evaluated {ev['evaluated_at']}" if ev.get("evaluated_at") else ""))
    if ev.get("stale_against_approval"):
        st.warning("⚠️ This evaluation differs from the evidence bound to the pending approval request.")
