"""Streamlit frontend for WorkplaceOps AI — Request-to-Payment Copilot.

Prototype on synthetic data. Draft-only: nothing is purchased, approved, or paid.
Run the backend first:  python -m uvicorn app.main:app --reload
Then:                   python -m streamlit run ui/streamlit_app.py
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from typing import Any, Optional

import requests
import streamlit as st

API_BASE = os.environ.get("WORKPLACEOPS_API", "http://127.0.0.1:8000").rstrip("/")
DEFAULT_TODAY = date(2026, 10, 17)
TIMEOUT = 60

STATUS_ICON = {
    "pass": "✅",
    "match": "✅",
    "warn": "⚠️",
    "warning": "⚠️",
    "fail": "⛔",
    "blocked": "⛔",
    "mismatch": "⛔",
    "duplicate": "⛔",
    "uncertain": "❔",
    "needs_clarification": "❔",
    "info": "ℹ️",
    None: "❔",
}
SEVERITY = {"pass": 0, "info": 0, "uncertain": 1, "warn": 2, "fail": 3}

INVOICE_LABELS = {
    None: "None — skip invoice check",
    "INV-5001": "INV-5001 — clean match",
    "INV-5002": "INV-5002 — rate + qty mismatch",
    "INV-4987": "INV-4987 — duplicate",
}

FALLBACK_SAMPLES = {
    "happy_path": "Organize lunch for 100 employees next Friday. Keep the total under Rs 1,00,000, "
    "use an approved vendor, and follow the workplace event policy.",
    "over_budget": "Organize lunch for 120 employees next Friday at the Bengaluru office. "
    "Budget is Rs 60,000. Approved vendors only.",
    "missing_info": "Can you arrange lunch for the team sometime soon?",
    "too_soon": "We need lunch for 40 people on Monday. Budget 40000 INR.",
}

BACKEND_HELP = (
    f"Cannot reach the WorkplaceOps AI backend at `{API_BASE}`.\n\n"
    "Start it from the project root:\n\n"
    "```\npython -m uvicorn app.main:app --reload\n```\n\n"
    "Or point the UI elsewhere with the `WORKPLACEOPS_API` environment variable."
)


# --------------------------------------------------------------------------- helpers
def format_inr(amount: Any, symbol: bool = True) -> str:
    """Format a number with Indian digit grouping, e.g. 7612500.5 -> ₹76,12,500.50."""
    if amount is None:
        return "—"
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{sign}{'₹' if symbol else ''}{whole}.{frac}"


def icon(status: Optional[str]) -> str:
    return STATUS_ICON.get(status, "❔")


def worst(statuses: list[Optional[str]]) -> Optional[str]:
    statuses = [s for s in statuses if s]
    if not statuses:
        return None
    return max(statuses, key=lambda s: SEVERITY.get(s, 1))


def api_get(path: str) -> Any:
    resp = requests.get(f"{API_BASE}{path}", timeout=10)
    resp.raise_for_status()
    return resp.json()


def api_post(path: str, payload: dict) -> Any:
    resp = requests.post(f"{API_BASE}{path}", json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def show_api_error(exc: Exception) -> None:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        st.error(BACKEND_HELP)
    elif isinstance(exc, requests.HTTPError) and exc.response is not None:
        detail = exc.response.text[:1500]
        st.error(f"Backend returned HTTP {exc.response.status_code}:\n\n```\n{detail}\n```")
    else:
        st.error(f"Unexpected error talking to backend: {exc}")


@st.cache_data(ttl=15, show_spinner=False)
def fetch_health() -> Optional[dict]:
    try:
        return api_get("/api/health")
    except Exception:
        return None


@st.cache_data(ttl=60, show_spinner=False)
def fetch_samples() -> dict:
    try:
        data = api_get("/api/samples")
        if isinstance(data, dict) and data:
            return data
    except Exception:
        pass
    return FALLBACK_SAMPLES


@st.cache_data(ttl=60, show_spinner=False)
def fetch_invoices() -> list:
    try:
        data = api_get("/api/invoices")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def evidence_md(evidence: list[dict]) -> str:
    if not evidence:
        return "_No evidence attached._"
    return "\n".join(f"- `{e.get('source', '?')}` — {e.get('detail', '')}" for e in evidence)


def checks_table(checks: list[dict]) -> None:
    if not checks:
        st.caption("No checks recorded.")
        return
    rows = [
        {
            "": icon(c.get("status")),
            "Check": c.get("name", ""),
            "Status": c.get("status", ""),
            "Detail": c.get("detail", ""),
            "Evidence": "; ".join(e.get("source", "") for e in c.get("evidence") or []),
        }
        for c in checks
    ]
    st.dataframe(rows, width="stretch", hide_index=True)
    with st.expander("Evidence per check"):
        for c in checks:
            st.markdown(f"**{icon(c.get('status'))} {c.get('name', '')}** — {c.get('detail', '')}")
            st.markdown(evidence_md(c.get("evidence") or []))


# --------------------------------------------------------------------------- stage status
def stage_statuses(result: dict) -> list[tuple[str, Optional[str], str]]:
    """Return (agent, status, note) for each pipeline stage."""
    stages: list[tuple[str, Optional[str], str]] = []

    req = result.get("request")
    if not req:
        stages.append(("Intake", None, "not run"))
    elif result.get("missing_fields"):
        stages.append(("Intake", "warn", f"missing: {', '.join(result['missing_fields'])}"))
    else:
        stages.append(("Intake", "pass", "fields extracted"))

    policy = result.get("policy")
    if policy:
        s = worst([c.get("status") for c in policy.get("checks") or []]) or "pass"
        stages.append(("Policy", s, f"{len(policy.get('clauses') or [])} clauses cited"))
    else:
        stages.append(("Policy", None, "not run"))

    vendors = result.get("vendors")
    if vendors:
        if not vendors.get("selected"):
            stages.append(("Vendor Matcher", "fail", "no eligible vendor"))
        elif vendors.get("replanned"):
            stages.append(("Vendor Matcher", "warn", "replanned to alternative"))
        else:
            stages.append(("Vendor Matcher", "pass", vendors["selected"].get("name", "")))
    else:
        stages.append(("Vendor Matcher", None, "not run"))

    proc = result.get("procurement")
    if proc:
        s = worst([c.get("status") for c in proc.get("checks") or []])
        if not proc.get("po"):
            s = "fail" if s in (None, "pass") else s
            note = "no PO drafted"
        else:
            s = s or "pass"
            note = f"draft {proc['po'].get('po_id', '')}"
        stages.append(("Procurement", s, note))
    else:
        stages.append(("Procurement", None, "not run"))

    rec = result.get("reconciliation")
    if rec:
        rmap = {"match": "pass", "mismatch": "fail", "duplicate": "fail"}
        stages.append(("Billing Guardian", rmap.get(rec.get("status"), "uncertain"), rec.get("status", "")))
    else:
        stages.append(("Billing Guardian", None, "no invoice selected"))

    critic = result.get("critic")
    if critic:
        cmap = {"pass": "pass", "warning": "warn", "blocked": "fail", "needs_clarification": "uncertain"}
        stages.append(("Critic", cmap.get(critic.get("status"), "uncertain"), critic.get("status", "")))
    else:
        stages.append(("Critic", None, "not run"))
    return stages


def render_pipeline(result: dict) -> None:
    stages = stage_statuses(result)
    cols = st.columns(len(stages) * 2 - 1)
    for i, (name, status, note) in enumerate(stages):
        with cols[i * 2]:
            st.markdown(
                f"<div style='text-align:center;border:1px solid #ddd;border-radius:10px;padding:8px 4px;'>"
                f"<div style='font-size:1.6rem'>{icon(status)}</div>"
                f"<div style='font-weight:600'>{name}</div>"
                f"<div style='font-size:0.75rem;color:#666'>{note}</div></div>",
                unsafe_allow_html=True,
            )
        if i < len(stages) - 1:
            with cols[i * 2 + 1]:
                st.markdown(
                    "<div style='text-align:center;font-size:1.6rem;padding-top:18px;color:#999'>→</div>",
                    unsafe_allow_html=True,
                )


def render_status_and_gate(result: dict) -> None:
    final = result.get("final_status", "needs_clarification")
    colors = {
        "pass": ("#1b7f3b", "PASS — ready for human approval"),
        "warning": ("#b7791f", "WARNING — review findings before approving"),
        "blocked": ("#c53030", "BLOCKED — human review required"),
        "needs_clarification": ("#4a5568", "NEEDS CLARIFICATION — answer the questions below"),
    }
    color, label = colors.get(final, ("#4a5568", final.upper()))
    left, right = st.columns([1, 1])
    with left:
        st.markdown(
            f"<div style='background:{color};color:white;padding:14px 18px;border-radius:10px;"
            f"font-size:1.2rem;font-weight:700'>{icon(final)} Final status: {label}</div>",
            unsafe_allow_html=True,
        )
        critic = result.get("critic") or {}
        if critic.get("summary"):
            st.caption(critic["summary"])

    with right:
        proc = result.get("procurement") or {}
        po = proc.get("po")
        approver = po.get("required_approver") if po else None
        with st.container(border=True):
            if result.get("human_approval_required", True):
                st.markdown(f"**🧑‍⚖️ Human approval required:** {approver or 'reviewer (no draft PO yet)'}")
            else:
                st.markdown("**Human approval:** not flagged by backend (still draft-only).")
            key = (po or {}).get("po_id") or "no-po"
            decisions = st.session_state.setdefault("decisions", {})
            can_approve = bool(po) and final in ("pass", "warning")
            c1, c2 = st.columns(2)
            if c1.button("✅ Approve draft", disabled=not can_approve, width="stretch",
                         help=None if can_approve else "Approval is only possible for a non-blocked draft PO."):
                decisions[key] = ("approved", approver, datetime.now(timezone.utc).isoformat())
            if c2.button("❌ Reject", width="stretch"):
                decisions[key] = ("rejected", approver, datetime.now(timezone.utc).isoformat())
            if key in decisions:
                decision, who, ts = decisions[key]
                st.info(
                    f"Decision **{decision.upper()}** (as {who or 'reviewer'}) at {ts}. "
                    "Decision recorded locally — no PO sent, no payment made."
                )


# --------------------------------------------------------------------------- tabs
def tab_request(result: dict) -> None:
    req = result.get("request")
    if not req:
        st.warning("Intake did not produce a structured request.")
    else:
        fields = [
            ("Request ID", req.get("request_id")),
            ("Category", req.get("category")),
            ("Service", req.get("service")),
            ("Date", req.get("date")),
            ("Attendees", req.get("attendees")),
            ("Budget limit", format_inr(req.get("budget_limit")) if req.get("budget_limit") is not None else None),
            ("Currency", req.get("currency")),
            ("Location", req.get("location")),
            ("Urgency", req.get("urgency")),
            ("Requirements", ", ".join(req.get("requirements") or []) or None),
        ]
        missing = set(result.get("missing_fields") or [])
        rows = [
            {"Field": k, "Value": "—" if v in (None, "") else str(v)}
            for k, v in fields
        ]
        st.dataframe(rows, width="stretch", hide_index=True)
        if req.get("raw_text"):
            st.caption(f"Raw request: “{req['raw_text']}”")
        if missing:
            st.warning(f"Missing fields: {', '.join(sorted(missing))}")
    questions = result.get("clarification_questions") or []
    if questions:
        st.subheader("❔ Clarification needed")
        for q in questions:
            st.markdown(f"- {q}")


def tab_policy(result: dict) -> None:
    policy = result.get("policy")
    if not policy:
        st.info("Policy check did not run (request incomplete or workflow stopped earlier).")
        return
    if policy.get("summary"):
        st.markdown(f"**Summary:** {policy['summary']}")
    st.subheader("Policy checks")
    checks_table(policy.get("checks") or [])
    st.subheader("Retrieved clauses (citations)")
    clauses = policy.get("clauses") or []
    if not clauses:
        st.caption("No clauses retrieved.")
    for cl in clauses:
        title = f"📜 {cl.get('clause_id', '?')} — {cl.get('title', '')}"
        with st.expander(title):
            st.markdown(cl.get("text", ""))
            st.caption(f"Source: {cl.get('source_file', '?')} · relevance score {cl.get('score', 0):.2f}")
    rules = policy.get("rules")
    if rules:
        with st.expander("Structured policy rules used"):
            st.json(rules)


def tab_vendors(result: dict) -> None:
    vendors = result.get("vendors")
    if not vendors:
        st.info("Vendor matching did not run.")
        return
    selected = vendors.get("selected") or {}
    sel_id = selected.get("vendor_id")
    if vendors.get("replanned"):
        st.warning(f"🔁 **Replanned (planted conflict detected):** {vendors.get('explanation', '')}")
    elif vendors.get("explanation"):
        st.info(vendors["explanation"])
    if selected:
        st.success(
            f"Selected: **{selected.get('name')}** ({sel_id}) · rate {format_inr(selected.get('rate_per_person'))}/person "
            f"· capacity {selected.get('capacity')} · rating {selected.get('rating')}"
        )
    else:
        st.error("No eligible vendor found — human review required.")
    rows = [
        {
            "Selected": "⭐" if ev.get("vendor_id") == sel_id else "",
            "Vendor": f"{ev.get('name')} ({ev.get('vendor_id')})",
            "Eligible": "✅" if ev.get("eligible") else "⛔",
            "Est. total": format_inr(ev.get("estimated_total")),
            "Reasons": "; ".join(ev.get("reasons") or []),
        }
        for ev in vendors.get("evaluations") or []
    ]
    if rows:
        st.dataframe(rows, width="stretch", hide_index=True)


def tab_po(result: dict) -> None:
    proc = result.get("procurement")
    if not proc:
        st.info("Procurement did not run.")
        return
    po = proc.get("po")
    if not po:
        st.error("No draft PO was created (e.g. budget or policy check failed). See checks below.")
    else:
        st.markdown(
            f"**{po.get('po_id')}** · status `{po.get('status', 'draft').upper()}` · vendor {po.get('vendor_name')} "
            f"({po.get('vendor_id')}) · event date {po.get('event_date')}"
        )
        rows = [
            {
                "Description": li.get("description"),
                "Qty": li.get("quantity"),
                "Unit price": format_inr(li.get("unit_price")),
                "Line total": format_inr(li.get("line_total")),
            }
            for li in po.get("line_items") or []
        ]
        st.dataframe(rows, width="stretch", hide_index=True)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Subtotal", format_inr(po.get("subtotal")))
        m2.metric("Tax (GST)", format_inr(po.get("tax")))
        m3.metric("Total", format_inr(po.get("total")))
        budget = (result.get("request") or {}).get("budget_limit")
        if budget is not None and po.get("total") is not None:
            headroom = float(budget) - float(po["total"])
            m4.metric("Budget headroom", format_inr(headroom), delta=f"of {format_inr(budget)}",
                      delta_color="normal" if headroom >= 0 else "inverse")
        st.markdown(f"**Required approver:** {po.get('required_approver')}")
        st.download_button(
            "⬇️ Download draft PO (JSON)",
            data=json.dumps(po, indent=2, ensure_ascii=False),
            file_name=f"{po.get('po_id', 'draft_po')}.json",
            mime="application/json",
        )
    steps = proc.get("calculation_steps") or []
    if steps:
        with st.expander("🧮 Calculation steps (deterministic)", expanded=True):
            for s in steps:
                st.markdown(f"- {s}")
    st.subheader("Budget & procurement checks")
    checks_table(proc.get("checks") or [])


def render_reconciliation(rec: dict) -> None:
    status = rec.get("status")
    msg = f"{icon(status)} Invoice **{rec.get('invoice_id')}** — status **{str(status).upper()}**"
    (st.success if status == "match" else st.error)(msg)
    m1, m2, m3 = st.columns(3)
    m1.metric("Expected (PO)", format_inr(rec.get("expected_total")))
    m2.metric("Invoiced", format_inr(rec.get("invoiced_total")))
    variance = rec.get("variance") or 0
    m3.metric("Variance", format_inr(variance), delta=None if not variance else "needs review",
              delta_color="inverse")
    checks_table(rec.get("checks") or [])
    if rec.get("recommendation"):
        st.markdown(f"**Recommendation:** {rec['recommendation']}")


def tab_invoice(result: dict) -> None:
    po = (result.get("procurement") or {}).get("po")
    invoices = fetch_invoices()
    inv_ids = [i.get("invoice_id") for i in invoices if i.get("invoice_id")] or [
        k for k in INVOICE_LABELS if k
    ]
    if po:
        c1, c2 = st.columns([3, 1])
        choice = c1.selectbox(
            "Re-check an invoice against the current draft PO",
            inv_ids,
            format_func=lambda x: INVOICE_LABELS.get(x, x),
            key="recheck_invoice",
        )
        c2.write("")
        if c2.button("🔍 Reconcile", width="stretch"):
            try:
                with st.spinner("Billing Guardian comparing invoice ↔ PO ↔ delivery…"):
                    st.session_state["recon_override"] = api_post(
                        "/api/reconcile", {"po": po, "invoice_id": choice}
                    )
            except Exception as exc:
                show_api_error(exc)
    else:
        st.info("No draft PO available — invoice reconciliation needs a PO to compare against.")

    rec = st.session_state.get("recon_override") or result.get("reconciliation")
    if rec:
        if st.session_state.get("recon_override"):
            st.caption("Showing ad-hoc reconciliation result (not part of the original workflow audit trail).")
        render_reconciliation(rec)
    elif po:
        st.caption("No invoice was checked in this run. Pick one above or in the sidebar.")


def tab_critic_audit(result: dict) -> None:
    critic = result.get("critic")
    st.subheader("Critic findings")
    if critic:
        st.markdown(f"{icon(critic.get('status'))} **{critic.get('status', '')}** — {critic.get('summary', '')}")
        checks_table(critic.get("findings") or [])
    else:
        st.caption("Critic did not run.")

    st.subheader("Audit trail")
    audit = result.get("audit_trail") or []
    if not audit:
        st.caption("No audit entries.")
        return
    rows = [
        {
            "Step": a.get("step"),
            "": icon(a.get("status")),
            "Agent": a.get("agent"),
            "Action": a.get("action"),
            "Status": a.get("status"),
            "Detail": a.get("detail"),
            "LLM used": "🤖" if a.get("llm_used") else "—",
            "Evidence #": len(a.get("evidence") or []),
        }
        for a in audit
    ]
    st.dataframe(rows, width="stretch", hide_index=True)
    for a in audit:
        with st.expander(f"Step {a.get('step')} · {a.get('agent')} · {a.get('action')}"):
            st.markdown(f"{icon(a.get('status'))} {a.get('detail', '')}")
            st.markdown(evidence_md(a.get("evidence") or []))
            st.caption(f"{a.get('timestamp', '')} · LLM used: {bool(a.get('llm_used'))}")
    st.download_button(
        "⬇️ Download audit report (JSON)",
        data=json.dumps(
            {"final_status": result.get("final_status"), "critic": critic, "audit_trail": audit,
             "disclaimer": result.get("disclaimer")},
            indent=2, ensure_ascii=False,
        ),
        file_name="workplaceops_audit.json",
        mime="application/json",
    )


# --------------------------------------------------------------------------- page
def sidebar() -> tuple[date, Optional[str]]:
    with st.sidebar:
        st.header("⚙️ Settings")
        health = fetch_health()
        if health is None:
            st.error("Backend offline")
            st.caption(f"`{API_BASE}` — start with `python -m uvicorn app.main:app --reload`")
            if st.button("Retry connection"):
                fetch_health.clear()
                fetch_samples.clear()
                fetch_invoices.clear()
                st.rerun()
        else:
            st.success(f"Backend: {health.get('status', 'ok')}")
            if health.get("llm_enabled"):
                st.caption(f"🤖 LLM enabled · model `{health.get('model', '?')}`")
            else:
                st.caption("🧮 Deterministic fallback (no LLM)")

        today = st.date_input("Demo date (today)", value=DEFAULT_TODAY)

        st.subheader("Sample requests")
        samples = fetch_samples()
        names = ["(custom)"] + list(samples.keys())

        def _apply_sample() -> None:
            name = st.session_state.get("sample_choice")
            if name and name != "(custom)":
                st.session_state["request_text"] = samples[name]

        st.selectbox(
            "Fill request from sample",
            names,
            key="sample_choice",
            on_change=_apply_sample,
            format_func=lambda n: n.replace("_", " "),
        )

        st.subheader("Invoice to check")
        invoice_id = st.selectbox(
            "Invoice", list(INVOICE_LABELS.keys()), index=1, format_func=lambda x: INVOICE_LABELS[x]
        )
        st.divider()
        st.caption("All data is synthetic. Outputs are drafts; humans make every decision.")
    return today, invoice_id


def main() -> None:
    st.set_page_config(page_title="WorkplaceOps AI — Request-to-Payment Copilot", page_icon="🧾", layout="wide")
    st.title("🧾 WorkplaceOps AI — Request-to-Payment Copilot")
    st.warning(
        "**Prototype on synthetic data · Draft-only.** No purchase order is issued, nothing is approved, "
        "and no payment is executed. Final approvals stay with humans."
    )

    if "request_text" not in st.session_state:
        st.session_state["request_text"] = FALLBACK_SAMPLES["happy_path"]

    today, invoice_id = sidebar()

    st.text_area("Workplace event request", key="request_text", height=110)
    run = st.button("▶️ Run workflow", type="primary")

    if run:
        text = (st.session_state.get("request_text") or "").strip()
        if not text:
            st.warning("Please enter a request first.")
        else:
            try:
                with st.spinner("Agents working: Intake → Policy → Vendor → Procurement → Billing → Critic…"):
                    st.session_state["result"] = api_post(
                        "/api/workflow",
                        {"text": text, "today": today.isoformat() if today else None, "invoice_id": invoice_id},
                    )
                st.session_state.pop("recon_override", None)
            except Exception as exc:
                show_api_error(exc)

    result = st.session_state.get("result")
    if not result:
        st.info("Pick a sample (sidebar) or type a request, then click **Run workflow**.")
        return

    st.subheader("Agent pipeline")
    render_pipeline(result)
    st.write("")
    render_status_and_gate(result)

    tabs = st.tabs(["1. Request", "2. Policy", "3. Vendors", "4. Draft PO", "5. Invoice check", "6. Critic & Audit"])
    renderers = [tab_request, tab_policy, tab_vendors, tab_po, tab_invoice, tab_critic_audit]
    for tab, render in zip(tabs, renderers):
        with tab:
            try:
                render(result)
            except Exception as exc:  # keep the demo alive if a section has an unexpected shape
                st.error(f"Could not render this section: {exc}")

    if result.get("disclaimer"):
        st.caption(result["disclaimer"])


main()
