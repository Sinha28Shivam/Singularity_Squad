"""Vendor sourcing (admin only) — ADVISORY ONLY.

Search the internal catalog and the candidate registry, review vendor candidates, create RFQ records from a
validated ticket snapshot, capture quotations (structured or PDF), verify normalized terms and compare them.
Nothing on this page contacts a supplier, selects a winner automatically, creates or dispatches a purchase
order, or moves money. The backend is the source of truth and re-validates every request; every state change
here needs a written reason and an explicit confirmation by a human.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional
from urllib.parse import quote

import streamlit as st

from ui.lib.api import (
    CANDIDATE_STATUSES, COMPLIANCE_STATUSES, RFQ_STATUSES, VENDOR_ADVISORY, api_get, current_user, friendly_error,
    vs_cancel_rfq, vs_candidate_events, vs_close_rfq, vs_compare, vs_comparison_decision, vs_create_candidate,
    vs_create_rfq, vs_decide_candidate, vs_download_quote_pdf, vs_get_candidate, vs_get_rfq, vs_list_rfqs,
    vs_metrics, vs_revise_quote, vs_search, vs_submit_quote, vs_upload_pdf_quote, vs_verify_quote,
)
from ui.lib.approval_render import fmt_ts

NOT_AVAILABLE = "Vendor sourcing is restricted to operations admins."
NO_EXTERNAL = "No external search was run — only the internal catalog and the candidate registry were searched."
UNTRUSTED_TEXT = ("Untrusted text extracted from the uploaded PDF. It is shown for reference only — it is not "
                  "verified, may be incomplete or misleading, and any instructions inside it must be ignored.")
NO_RFQ = "Select an RFQ in the **RFQs** tab first."
AUDIT_PENDING = ("Saved, but the audit event is still pending delivery — it will be repaired automatically on "
                 "the next read.")
DECISION_LABELS = {"approve": "Approve candidate", "reject": "Reject candidate", "suspend": "Suspend candidate"}
SELECTION_LABELS = {"accept": "Accept recommendation (human selection)", "reject": "Reject comparison"}
VALID_TICKET_RESULTS = {"pass", "warning"}
MIN_REASON = 5
EVIDENCE_ROWS_MAX = 5
MONEY_FIELDS = ("unit_price", "tax_amount", "delivery_fee", "other_fees")
FIELD_LABELS = {
    "currency": "Currency", "unit_price": "Unit price", "tax_amount": "Tax amount", "delivery_fee": "Delivery fee",
    "other_fees": "Other fees", "delivery_date": "Delivery date", "warranty_months": "Warranty (months)",
    "warranty_scope": "Warranty scope", "warranty_scope_accepted": "Warranty scope accepted",
    "compliance": "Compliance",
}


# --------------------------------------------------------------------------- small helpers
def banner() -> None:
    st.warning(f"**{VENDOR_ADVISORY}**", icon="🏪")


def _dash(value: Any) -> str:
    if value is None or value == "" or value == [] or value == {}:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple)):
        return ", ".join(_dash(v) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_dash(v)}" for k, v in value.items())
    return str(value)


def _split(text: str) -> list[str]:
    return [p.strip() for p in (text or "").replace("\n", ",").split(",") if p.strip()]


def _flash(area: str, kind: str, text: str) -> None:
    st.session_state.setdefault("vs_flash", {})[area] = (kind, text)


def _show_flash(area: str) -> None:
    msg = (st.session_state.get("vs_flash") or {}).pop(area, None)
    if msg:
        kind, text = msg
        {"error": st.error, "warning": st.warning}.get(kind, st.info)(text)


def _err(exc: Exception) -> str:
    return friendly_error(exc)


def _audit_note(area: str, data: Any) -> None:
    if isinstance(data, dict) and data.get("audit_pending"):
        _flash(area + "_audit", "warning", AUDIT_PENDING)


def _reason_ok(reason: str) -> bool:
    return len((reason or "").strip()) >= MIN_REASON


def _table(rows: list[dict]) -> None:
    st.dataframe(rows, width="stretch", hide_index=True)


def _kv(rows: list[tuple]) -> None:
    _table([{"Field": k, "Value": _dash(v)} for k, v in rows])


# --------------------------------------------------------------------------- per-run cache
class _Ctx:
    """Avoids repeating the same GET across tabs within one script run."""

    def __init__(self) -> None:
        self._rfq: dict[str, Any] = {}

    def rfq(self, rfq_id: str) -> tuple[Optional[dict], Optional[str]]:
        if rfq_id not in self._rfq:
            try:
                self._rfq[rfq_id] = (vs_get_rfq(rfq_id), None)
            except Exception as exc:  # noqa: BLE001
                self._rfq[rfq_id] = (None, _err(exc))
        return self._rfq[rfq_id]

    def invalidate(self) -> None:
        self._rfq.clear()


def _active_rfq_id() -> Optional[str]:
    return st.session_state.get("vs_rfq_pick")


# --------------------------------------------------------------------------- tab: vendor search
def _internal_rows(vendors: list) -> list[dict]:
    return [{"Vendor ID": v.get("vendor_id"), "Name": v.get("name"), "Approved": _dash(v.get("approved")),
             "Services": _dash(v.get("services")), "Capacity": _dash(v.get("capacity")),
             "Rate / person": _dash(v.get("rate_per_person")), "Source": v.get("source") or "internal_catalog"}
            for v in vendors if isinstance(v, dict)]


def _candidate_rows(cands: list) -> list[dict]:
    return [{"Candidate ID": c.get("candidate_id"), "Name": c.get("name"), "Status": _dash(c.get("status")),
             "Services": _dash(c.get("services")), "Regions": _dash(c.get("regions")),
             "Source": _dash(c.get("source")), "Source reference": _dash(c.get("source_reference")),
             "Evidence items": len(c.get("compliance_evidence") or c.get("compliance_documents") or []),
             "Created by": _dash(c.get("created_by"))}
            for c in cands if isinstance(c, dict)]


def tab_search() -> None:
    banner()
    st.caption("Searches the read-only internal vendor catalog and the onboarding candidate registry. "
               "Candidates are not approved vendors until a human approves them.")
    c1, c2, c3 = st.columns(3)
    q = c1.text_input("Search text", key="vs_q", max_chars=120)
    service = c2.text_input("Service (optional)", key="vs_service", max_chars=80)
    status = c3.selectbox("Candidate status", [None] + CANDIDATE_STATUSES, key="vs_search_status",
                          format_func=lambda s: "(any)" if s is None else s.replace("_", " "))
    try:
        data = vs_search(q, service or None, status)
    except Exception as exc:  # noqa: BLE001
        st.error(_err(exc))
        return
    st.info(f"ℹ️ {NO_EXTERNAL}")
    if data.get("note"):
        st.caption(str(data["note"])[:300])
    st.subheader("Internal catalog (read-only)")
    internal = _internal_rows(data.get("internal_vendors") or [])
    if internal:
        _table(internal)
    else:
        st.caption("No internal catalog vendors match.")
    st.subheader("Candidate registry (onboarding — not yet approved unless marked)")
    cands = _candidate_rows(data.get("candidates") or [])
    if cands:
        _table(cands)
    else:
        st.caption("No candidates match.")


# --------------------------------------------------------------------------- tab: candidates
def _register_form() -> None:
    st.subheader("Register a vendor candidate")
    st.caption("New candidates always start as **pending review**. Registering does not approve or contact them.")
    n = int(st.number_input("Compliance evidence rows", min_value=0, max_value=EVIDENCE_ROWS_MAX, value=1,
                            step=1, key="vs_ev_count"))
    with st.form("vs_cand_form", clear_on_submit=False):
        name = st.text_input("Vendor name *", key="vs_c_name", max_chars=160)
        services = st.text_input("Services * (comma-separated)", key="vs_c_services")
        regions = st.text_input("Regions (comma-separated)", key="vs_c_regions")
        email = st.text_input("Contact email", key="vs_c_email", max_chars=254)
        source_ref = st.text_input("Source / provenance reference", key="vs_c_source_ref", max_chars=500)
        notes = st.text_area("Notes", key="vs_c_notes", max_chars=2000)
        evidence = []
        for i in range(n):
            e1, e2, e3 = st.columns(3)
            dtype = e1.text_input(f"Evidence {i + 1} document type", key=f"vs_c_ev_{i}_type")
            ref = e2.text_input(f"Evidence {i + 1} reference", key=f"vs_c_ev_{i}_ref")
            exp = e3.text_input(f"Evidence {i + 1} expires on (YYYY-MM-DD)", key=f"vs_c_ev_{i}_exp")
            evidence.append((dtype, ref, exp))
        submitted = st.form_submit_button("Register candidate (pending review)")
    if not submitted:
        return
    svc = _split(services)
    if len(name.strip()) < 2 or not svc:
        _flash("cand_form", "error", "Please provide a vendor name and at least one service.")
        st.rerun()
    rows = []
    for dtype, ref, exp in evidence:
        if not (dtype.strip() or ref.strip()):
            continue
        if not (dtype.strip() and ref.strip()):
            _flash("cand_form", "error", "Each evidence row needs both a document type and a reference.")
            st.rerun()
        row = {"document_type": dtype.strip(), "reference": ref.strip(), "verified": False}
        if exp.strip():
            try:
                row["expires_on"] = date.fromisoformat(exp.strip()).isoformat()
            except ValueError:
                _flash("cand_form", "error", f"Invalid evidence expiry date `{exp.strip()[:20]}` (use YYYY-MM-DD).")
                st.rerun()
        rows.append(row)
    body: dict = {"name": name.strip(), "services": svc, "regions": _split(regions), "source": "manual",
                  "notes": notes.strip()}
    if email.strip():
        body["contact_email"] = email.strip()
    if source_ref.strip():
        body["source_reference"] = source_ref.strip()
    if rows:
        body["compliance_evidence"] = rows
    try:
        data = vs_create_candidate(body)
        cid = data.get("candidate_id") if isinstance(data, dict) else None
        _flash("cand_form", "info", f"Candidate {cid or ''} registered as pending review. No vendor was contacted.")
        _audit_note("cand_form", data)
    except Exception as exc:  # noqa: BLE001
        _flash("cand_form", "error", _err(exc))
    st.rerun()


def _render_candidate_detail(cand: dict) -> None:
    _kv([
        ("Candidate ID", cand.get("candidate_id")), ("Name", cand.get("name")),
        ("Status", (cand.get("status") or "unknown").replace("_", " ")),
        ("Services", cand.get("services")), ("Regions", cand.get("regions")),
        ("Contact email", cand.get("contact_email")), ("Source", cand.get("source")),
        ("Source reference (provenance)", cand.get("source_reference")), ("Notes", cand.get("notes")),
        ("Compliance documents", cand.get("compliance_documents")),
        ("Registered by", cand.get("created_by")), ("Registered at", fmt_ts(cand.get("created_at"))),
        ("Last reviewed by", cand.get("reviewed_by")), ("Last reviewed at", fmt_ts(cand.get("reviewed_at"))),
        ("Last review reason", cand.get("review_reason")),
    ])
    evidence = [e for e in cand.get("compliance_evidence") or [] if isinstance(e, dict)]
    st.markdown("**Compliance evidence**")
    if evidence:
        _table([{"Document type": e.get("document_type"), "Reference": e.get("reference"),
                 "Expires on": _dash(e.get("expires_on")), "Verified": _dash(bool(e.get("verified")))}
                for e in evidence])
    else:
        st.caption("No structured evidence recorded.")
    history = [h for h in cand.get("status_history") or [] if isinstance(h, dict)]
    if history:
        st.markdown("**Status history**")
        _table([{"At": fmt_ts(h.get("at") or h.get("created_at")), "From": _dash(h.get("from_status")),
                 "To": _dash(h.get("to_status") or h.get("status")), "By": _dash(h.get("actor") or h.get("by")),
                 "Reason": _dash(h.get("reason"))} for h in history])
    if cand.get("audit_pending"):
        st.warning(AUDIT_PENDING)


def _on_candidate_decide(cid: str) -> None:
    decision = st.session_state.get("vs_cand_decision")
    reason = st.session_state.get("vs_cand_reason") or ""
    if not st.session_state.get("vs_cand_confirm"):
        _flash("cand", "error", "Please confirm the decision first.")
        return
    if not _reason_ok(reason):
        _flash("cand", "error", f"A reason of at least {MIN_REASON} characters is required.")
        return
    try:
        data = vs_decide_candidate(cid, decision, reason.strip())
        status = data.get("status") if isinstance(data, dict) else None
        _flash("cand", "info", f"Decision recorded for {cid}: status is now "
                               f"**{(status or decision).replace('_', ' ')}**. No vendor was contacted.")
        _audit_note("cand", data)
        st.session_state["vs_cand_reason"] = ""
    except Exception as exc:  # noqa: BLE001
        _flash("cand", "error", _err(exc))
    st.session_state["vs_cand_confirm"] = False


def _review_queue() -> None:
    st.subheader("Review queue")
    status = st.selectbox("Show candidates with status", CANDIDATE_STATUSES, key="vs_cand_status",
                          format_func=lambda s: s.replace("_", " "))
    try:
        cands = [c for c in vs_search("", None, status).get("candidates") or [] if isinstance(c, dict)]
    except Exception as exc:  # noqa: BLE001
        st.error(_err(exc))
        return
    if not cands:
        st.caption("No candidates in this state.")
        return
    _table(_candidate_rows(cands))
    by_id = {c["candidate_id"]: c for c in cands if c.get("candidate_id")}
    cid = st.selectbox("Review candidate", [None] + list(by_id), key="vs_cand_pick",
                       format_func=lambda c: "(select a candidate)" if c is None
                       else f"{c} · {by_id[c].get('name') or ''}")
    if not cid:
        return
    cand = by_id[cid]
    try:
        fresh = vs_get_candidate(cid)
        if isinstance(fresh, dict) and fresh.get("candidate_id"):
            cand = fresh
    except Exception:  # noqa: BLE001 — detail endpoint optional; fall back to the list row
        pass
    _render_candidate_detail(cand)
    st.markdown("**Human decision**")
    st.selectbox("Decision", list(DECISION_LABELS), key="vs_cand_decision", format_func=DECISION_LABELS.get)
    st.text_area(f"Reason (required, at least {MIN_REASON} characters)", key="vs_cand_reason", max_chars=1000)
    confirmed = st.checkbox("I have reviewed the provenance and evidence and confirm this decision.",
                            key="vs_cand_confirm")
    st.button("Record decision", key="vs_cand_decide_btn", disabled=not confirmed,
              on_click=_on_candidate_decide, args=(cid,))


def tab_candidates() -> None:
    banner()
    _show_flash("cand_form")
    _show_flash("cand_form_audit")
    _register_form()
    st.divider()
    _show_flash("cand")
    _show_flash("cand_audit")
    _review_queue()


# --------------------------------------------------------------------------- tab: RFQs
def _validated_tickets() -> list:
    try:
        data = api_get("/api/admin/tickets", params={"limit": 100})
    except Exception as exc:  # noqa: BLE001
        st.error(_err(exc))
        return []
    rows = data.get("tickets") if isinstance(data, dict) else None
    return [t for t in rows or [] if isinstance(t, dict) and t.get("ticket_id")
            and t.get("final_status") in VALID_TICKET_RESULTS]


def _ticket_snapshot(ticket_id: str) -> tuple[Optional[dict], Optional[str]]:
    try:
        view = api_get(f"/api/admin/tickets/{quote(ticket_id, safe='')}")
    except Exception as exc:  # noqa: BLE001
        return None, friendly_error(exc, ticket=True)
    wr = view.get("workflow_result") if isinstance(view, dict) else None
    if not isinstance(wr, dict) or wr.get("final_status") not in VALID_TICKET_RESULTS:
        return None, "This ticket has no completed pass/warning workflow result."
    req = wr.get("request")
    if not isinstance(req, dict):
        return None, "This ticket has no validated request snapshot."
    missing = [f for f in ("service", "attendees", "budget_limit", "date") if req.get(f) in (None, "")]
    if missing or wr.get("missing_fields"):
        return None, "The validated snapshot is missing: " + ", ".join(missing or wr.get("missing_fields"))
    return req, None


def _recipients() -> dict[str, str]:
    try:
        data = vs_search("", None, "approved")
    except Exception as exc:  # noqa: BLE001
        st.error(_err(exc))
        return {}
    out: dict[str, str] = {}
    for v in data.get("internal_vendors") or []:
        if isinstance(v, dict) and v.get("approved") and v.get("vendor_id"):
            out[v["vendor_id"]] = f"{v['vendor_id']} · {v.get('name') or ''} (internal catalog)"
    for c in data.get("candidates") or []:
        if isinstance(c, dict) and c.get("status") == "approved" and c.get("candidate_id"):
            out[c["candidate_id"]] = f"{c['candidate_id']} · {c.get('name') or ''} (approved candidate)"
    return out


def _default_deadline(delivery: date) -> str:
    return f"{(delivery - timedelta(days=1)).isoformat()}T17:00:00+05:30"


def _create_rfq_section() -> None:
    st.subheader("Create an RFQ from a validated ticket")
    st.caption("Scope is copied from the ticket's validated request snapshot; the backend re-checks it. "
               "Creating an RFQ records a draft sourcing request only — no supplier is contacted.")
    tickets = _validated_tickets()
    if not tickets:
        st.info("No tickets with a completed pass/warning workflow result yet.")
        return
    labels = {t["ticket_id"]: f"{t['ticket_id']} · {t.get('title') or ''} ({t.get('final_status')})"
              for t in tickets}
    tid = st.selectbox("Validated ticket", [None] + list(labels), key="vs_rfq_ticket",
                       format_func=lambda t: "(select a validated ticket)" if t is None else labels.get(t, t))
    if not tid:
        return
    snap, err = _ticket_snapshot(tid)
    if err:
        st.warning(err)
        return
    try:
        delivery = date.fromisoformat(str(snap.get("date"))[:10])
    except ValueError:
        st.warning("The validated snapshot has an invalid delivery date.")
        return
    prefill = {
        "request_id": tid, "service": str(snap.get("service")), "quantity": str(snap.get("attendees")),
        "budget_limit": str(snap.get("budget_limit")), "currency": str(snap.get("currency") or "INR").upper(),
        "delivery_date": delivery.isoformat(), "delivery_location": str(snap.get("location") or ""),
    }
    st.markdown("**Prefilled from the validated ticket snapshot (read-only)**")
    _kv([("Ticket", tid), ("Service", prefill["service"]), ("Quantity", prefill["quantity"]),
         ("Budget limit", f"{prefill['budget_limit']} {prefill['currency']}"),
         ("Delivery date", prefill["delivery_date"]), ("Delivery location", prefill["delivery_location"])])
    recipients = _recipients()
    with st.form(f"vs_rfq_form_{tid}"):
        location = prefill["delivery_location"]
        if not location:
            location = st.text_input("Delivery location *", key=f"vs_rfq_location_{tid}")
        warranty = st.number_input("Warranty required (months)", min_value=0, max_value=120, value=0, step=1,
                                   key=f"vs_rfq_warranty_{tid}")
        warranty_scope = st.text_input("Warranty scope required (optional)", key=f"vs_rfq_wscope_{tid}")
        compliance = st.text_input("Compliance requirements (comma-separated, e.g. FSSAI)",
                                   key=f"vs_rfq_compliance_{tid}")
        deadline = st.text_input("Response deadline (ISO 8601 with timezone)", value=_default_deadline(delivery),
                                 key=f"vs_rfq_deadline_{tid}")
        picked = st.multiselect("Recipients (approved vendors / approved candidates only)", list(recipients),
                                format_func=lambda v: recipients.get(v, v), key=f"vs_rfq_recipients_{tid}")
        submitted = st.form_submit_button("Create RFQ record (no supplier is contacted)")
    if not submitted:
        return
    try:
        parsed = datetime.fromisoformat(deadline.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except ValueError:
        _flash("rfq_create", "error", "Response deadline must be ISO 8601 with a timezone, e.g. "
                                      "2026-10-22T17:00:00+05:30.")
        st.rerun()
    if not picked:
        _flash("rfq_create", "error", "Select at least one approved recipient.")
        st.rerun()
    if len((location or "").strip()) < 2:
        _flash("rfq_create", "error", "A delivery location is required.")
        st.rerun()
    body = dict(prefill, delivery_location=location.strip(), warranty_months_required=int(warranty),
                compliance_requirements=_split(compliance), response_deadline=deadline.strip(),
                candidate_ids=list(picked))
    if warranty_scope.strip():
        body["warranty_scope_required"] = warranty_scope.strip()
    try:
        data = vs_create_rfq(body)
        rid = data.get("rfq_id") if isinstance(data, dict) else None
        _flash("rfq_create", "info", f"RFQ {rid or ''} recorded as **open**. No supplier has been contacted.")
        _audit_note("rfq_create", data)
        if rid:
            st.session_state["vs_rfq_pending_pick"] = rid
    except Exception as exc:  # noqa: BLE001
        _flash("rfq_create", "error", _err(exc))
    st.rerun()


def _rfq_rows(rfqs: list) -> list[dict]:
    return [{"RFQ": r.get("rfq_id"), "Ticket": _dash(r.get("request_id")), "Service": _dash(r.get("service")),
             "Status": _dash(r.get("status")), "Response deadline": fmt_ts(r.get("response_deadline")),
             "Delivery date": _dash(r.get("delivery_date")), "Quotes received": _dash(r.get("quotes_received")),
             "Budget": f"{_dash(r.get('budget_limit'))} {r.get('currency') or ''}".strip()}
            for r in rfqs]


def _on_rfq_action(rfq_id: str, ctx: _Ctx) -> None:
    action = st.session_state.get("vs_rfq_action")
    reason = st.session_state.get("vs_rfq_reason") or ""
    if not st.session_state.get("vs_rfq_confirm"):
        _flash("rfq", "error", "Please confirm first.")
        return
    if not _reason_ok(reason):
        _flash("rfq", "error", f"A reason of at least {MIN_REASON} characters is required.")
        return
    try:
        data = (vs_close_rfq if action == "close" else vs_cancel_rfq)(rfq_id, reason.strip())
        _flash("rfq", "info", f"RFQ {rfq_id} is now **{'closed' if action == 'close' else 'cancelled'}**.")
        _audit_note("rfq", data)
        st.session_state["vs_rfq_reason"] = ""
    except Exception as exc:  # noqa: BLE001
        _flash("rfq", "error", _err(exc))
    st.session_state["vs_rfq_confirm"] = False
    ctx.invalidate()


def _quote_rows(quotes: list) -> list[dict]:
    return [{"Quote": q.get("quote_id"), "Vendor": _dash(q.get("vendor_id")),
             "Reference": _dash(q.get("quote_reference")), "Type": _dash(q.get("submission_type")),
             "Status": _dash(q.get("status")).replace("_", " "), "Revision": _dash(q.get("revision")),
             "Submitted": fmt_ts(q.get("submitted_at")), "Verified by": _dash(q.get("verified_by"))}
            for q in quotes if isinstance(q, dict)]


def _rfq_list_and_detail(ctx: _Ctx) -> None:
    st.subheader("RFQ records")
    status = st.selectbox("Status filter", [None] + RFQ_STATUSES, key="vs_rfq_status_filter",
                          format_func=lambda s: "(all)" if s is None else s)
    try:
        rfqs = [r for r in vs_list_rfqs(status=status) if r.get("rfq_id")]
    except Exception as exc:  # noqa: BLE001
        st.error(_err(exc))
        rfqs = []
    if rfqs:
        _table(_rfq_rows(rfqs))
    else:
        st.caption("No RFQ records.")
    ids = [r["rfq_id"] for r in rfqs]
    pending = st.session_state.pop("vs_rfq_pending_pick", None)
    if pending and pending in ids:
        st.session_state["vs_rfq_pick"] = pending
    if st.session_state.get("vs_rfq_pick") not in ids + [None]:
        st.session_state["vs_rfq_pick"] = None
    rid = st.selectbox("Active RFQ (used by the Quotes and Compare tabs)", [None] + ids, key="vs_rfq_pick",
                       format_func=lambda r: "(select an RFQ)" if r is None else r)
    if not rid:
        return
    detail, err = ctx.rfq(rid)
    if err:
        st.error(err)
        return
    rfq = detail.get("rfq") or {}
    st.markdown(f"**RFQ {rid}** — status **{_dash(rfq.get('status'))}**")
    if detail.get("notice"):
        st.caption(str(detail["notice"])[:300])
    _kv([("Ticket", rfq.get("request_id")), ("Service", rfq.get("service")), ("Quantity", rfq.get("quantity")),
         ("Budget limit", f"{_dash(rfq.get('budget_limit'))} {rfq.get('currency') or ''}"),
         ("Delivery date", rfq.get("delivery_date")), ("Delivery location", rfq.get("delivery_location")),
         ("Warranty required (months)", rfq.get("warranty_months_required")),
         ("Warranty scope required", rfq.get("warranty_scope_required")),
         ("Compliance requirements", rfq.get("compliance_requirements")),
         ("Response deadline", fmt_ts(rfq.get("response_deadline"))), ("Recipients", rfq.get("candidate_ids")),
         ("Quotes received", rfq.get("quotes_received")), ("Created by", rfq.get("created_by")),
         ("Created at", fmt_ts(rfq.get("created_at"))), ("Status reason", rfq.get("status_reason")),
         ("Snapshot hash", rfq.get("requirements_snapshot_hash"))])
    if rfq.get("audit_pending"):
        st.warning(AUDIT_PENDING)
    quotes = [q for q in detail.get("quotes") or [] if isinstance(q, dict)]
    st.markdown("**Quotations**")
    if quotes:
        _table(_quote_rows(quotes))
    else:
        st.caption("No quotations recorded yet.")
    if rfq.get("status") == "open":
        st.markdown("**Close or cancel this RFQ**")
        st.selectbox("Action", ["close", "cancel"], key="vs_rfq_action",
                     format_func=lambda a: "Close (stop accepting quotes)" if a == "close"
                     else "Cancel (no comparison allowed)")
        st.text_area(f"Reason (required, at least {MIN_REASON} characters)", key="vs_rfq_reason", max_chars=1000)
        ok = st.checkbox("I confirm this RFQ status change.", key="vs_rfq_confirm")
        st.button("Apply status change", key="vs_rfq_action_btn", disabled=not ok, on_click=_on_rfq_action,
                  args=(rid, ctx))


def tab_rfqs(ctx: _Ctx) -> None:
    banner()
    _show_flash("rfq_create")
    _show_flash("rfq_create_audit")
    _create_rfq_section()
    st.divider()
    _show_flash("rfq")
    _show_flash("rfq_audit")
    _rfq_list_and_detail(ctx)


# --------------------------------------------------------------------------- tab: quotes
def _submitted(q: dict) -> dict:
    sub = q.get("submitted")
    return sub if isinstance(sub, dict) else q


def _verified(q: dict) -> Optional[dict]:
    ver = q.get("verified")
    if isinstance(ver, dict):
        return ver
    return q if q.get("status") == "verified" else None


def _diff_rows(q: dict) -> list[dict]:
    verification = q.get("verification") if isinstance(q.get("verification"), dict) else {}
    diff = verification.get("diff") if verification else q.get("diff")
    rows: list[dict] = []
    if isinstance(diff, dict):
        for field, change in diff.items():
            if isinstance(change, dict):
                rows.append({"Field": FIELD_LABELS.get(field, field), "Submitted": _dash(change.get("submitted")),
                             "Verified": _dash(change.get("verified"))})
    elif isinstance(diff, list):
        for change in diff:
            if isinstance(change, dict):
                field = change.get("field") or "?"
                rows.append({"Field": FIELD_LABELS.get(field, field), "Submitted": _dash(change.get("submitted")),
                             "Verified": _dash(change.get("verified"))})
    if rows or diff is not None:
        return rows
    sub, ver = q.get("submitted"), q.get("verified")
    if isinstance(sub, dict) and isinstance(ver, dict):
        for field in FIELD_LABELS:
            if field in ver and str(sub.get(field)) != str(ver.get(field)):
                rows.append({"Field": FIELD_LABELS[field], "Submitted": _dash(sub.get(field)),
                             "Verified": _dash(ver.get(field))})
    return rows


def _structured_quote_form(rfq_id: str, rfq: dict, quotes: list) -> None:
    st.subheader("Structured quotation entry")
    vendors = list(rfq.get("candidate_ids") or [])
    if not vendors:
        st.caption("This RFQ has no recipients.")
        return
    revisable = [q["quote_id"] for q in quotes if q.get("quote_id") and q.get("status") != "superseded"]
    try:
        default_date = date.fromisoformat(str(rfq.get("delivery_date"))[:10])
    except ValueError:
        default_date = date.today()
    reqs = list(rfq.get("compliance_requirements") or [])
    with st.form(f"vs_quote_form_{rfq_id}"):
        revise_of = st.selectbox("Revision of (optional — creates a new revision, the previous one is superseded)",
                                 [None] + revisable, key="vs_q_revise_of",
                                 format_func=lambda x: "(new quotation)" if x is None else x)
        vendor = st.selectbox("Vendor *", vendors, key="vs_q_vendor")
        ref = st.text_input("Quote reference *", key="vs_q_ref", max_chars=120)
        c1, c2, c3 = st.columns(3)
        currency = c1.text_input("Currency *", value=str(rfq.get("currency") or "INR"), key="vs_q_currency")
        unit_price = c2.text_input("Unit price *", key="vs_q_unit_price")
        tax = c3.text_input("Tax amount", value="0", key="vs_q_tax_amount")
        c4, c5, c6 = st.columns(3)
        dfee = c4.text_input("Delivery fee", value="0", key="vs_q_delivery_fee")
        ofee = c5.text_input("Other fees", value="0", key="vs_q_other_fees")
        ddate = c6.date_input("Delivery date *", value=default_date, key="vs_q_delivery_date")
        warranty = st.number_input("Warranty (months)", min_value=0, max_value=120, value=0, step=1,
                                   key="vs_q_warranty_months")
        scope = st.text_input("Warranty scope *", key="vs_q_warranty_scope", max_chars=1000)
        comp = {r: st.checkbox(f"Vendor declares: {r}", key=f"vs_q_comp_{i}") for i, r in enumerate(reqs)}
        notes = st.text_area("Notes", key="vs_q_notes", max_chars=2000)
        revision_reason = st.text_input("Revision reason (required for a revision)", key="vs_q_revision_reason",
                                        max_chars=1000)
        submitted = st.form_submit_button("Record quotation (pending verification)")
    if not submitted:
        return
    if not ref.strip() or not unit_price.strip() or not scope.strip():
        _flash("quote", "error", "Quote reference, unit price and warranty scope are required.")
        st.rerun()
    if revise_of and not _reason_ok(revision_reason):
        _flash("quote", "error", f"A revision reason of at least {MIN_REASON} characters is required.")
        st.rerun()
    body = {"quote_reference": ref.strip(), "currency": currency.strip().upper(),
            "unit_price": unit_price.strip(), "tax_amount": tax.strip() or "0", "delivery_fee": dfee.strip() or "0",
            "other_fees": ofee.strip() or "0", "delivery_date": ddate.isoformat(),
            "warranty_months": int(warranty), "warranty_scope": scope.strip(), "compliance": comp,
            "notes": notes.strip()}
    if revise_of:  # the vendor is taken from the quotation being revised
        body["revision_reason"] = revision_reason.strip()
    else:
        body["vendor_id"] = vendor
    try:
        data = vs_revise_quote(rfq_id, revise_of, body) if revise_of else vs_submit_quote(rfq_id, body)
        qid = data.get("quote_id") if isinstance(data, dict) else None
        _flash("quote", "info", f"Quotation {qid or ''} recorded as **pending verification**.")
        _audit_note("quote", data)
    except Exception as exc:  # noqa: BLE001
        _flash("quote", "error", _err(exc))
    st.rerun()


def _on_pdf_upload(rfq_id: str) -> None:
    f = st.session_state.get("vs_pdf_file")
    vendor = st.session_state.get("vs_pdf_vendor")
    ref = (st.session_state.get("vs_pdf_ref") or "").strip()
    if f is None or not vendor or not ref:
        _flash("pdf", "error", "Choose a PDF, a vendor and a quote reference first.")
        return
    try:
        data = vs_upload_pdf_quote(rfq_id, vendor, ref, getattr(f, "name", "quotation.pdf") or "quotation.pdf",
                                   f.getvalue())
        qid = data.get("quote_id") if isinstance(data, dict) else None
        _flash("pdf", "info", f"PDF quotation {qid or ''} stored. Its terms stay **unverified** until a reviewer "
                              "verifies them below.")
        _audit_note("pdf", data)
    except Exception as exc:  # noqa: BLE001
        _flash("pdf", "error", _err(exc))


def _pdf_upload(rfq_id: str, rfq: dict) -> None:
    st.subheader("PDF quotation upload")
    st.caption("The PDF is sent as a raw `application/pdf` body. Extracted text is untrusted and never "
               "becomes quote terms until a human verifies them.")
    _show_flash("pdf")
    _show_flash("pdf_audit")
    st.file_uploader("Quotation PDF", type=["pdf"], key="vs_pdf_file")
    st.selectbox("Vendor", list(rfq.get("candidate_ids") or []), key="vs_pdf_vendor")
    st.text_input("Quote reference", key="vs_pdf_ref", max_chars=120)
    st.button("Upload PDF quotation", key="vs_pdf_btn", on_click=_on_pdf_upload, args=(rfq_id,))


def _verify_form(rfq_id: str, rfq: dict, q: dict) -> None:
    qid = q["quote_id"]
    sub = _submitted(q)
    st.markdown("**Verify normalized terms**")
    st.caption("Enter the terms exactly as confirmed against the source document. Only verified terms are "
               "compared. Differences from the submitted values are recorded.")
    try:
        ddefault = date.fromisoformat(str(sub.get("delivery_date") or rfq.get("delivery_date"))[:10])
    except ValueError:
        ddefault = date.today()
    sub_comp = sub.get("compliance") if isinstance(sub.get("compliance"), dict) else {}
    reqs = list(dict.fromkeys(list(rfq.get("compliance_requirements") or []) + list(sub_comp)))
    k = f"vs_v_{qid}_"
    with st.form(f"vs_verify_form_{qid}"):
        c1, c2, c3 = st.columns(3)
        currency = c1.text_input("Currency", value=str(sub.get("currency") or rfq.get("currency") or "INR"),
                                 key=k + "currency")
        money = {}
        cols = [c2, c3] + list(st.columns(2))
        for col, field in zip(cols, MONEY_FIELDS):
            default = sub.get(field)
            money[field] = col.text_input(FIELD_LABELS[field], value="" if default is None else str(default),
                                          key=k + field)
        ddate = st.date_input("Delivery date", value=ddefault, key=k + "delivery_date")
        warranty = st.number_input("Warranty (months)", min_value=0, max_value=120,
                                   value=int(sub.get("warranty_months") or 0), step=1, key=k + "warranty_months")
        scope = st.text_input("Warranty scope", value=str(sub.get("warranty_scope") or ""),
                              key=k + "warranty_scope")
        required_scope = rfq.get("warranty_scope_required")
        if required_scope:
            st.caption(f"RFQ warranty scope required: {required_scope}")
        scope_ok = st.checkbox("Warranty scope accepted (meets the RFQ requirement)", key=k + "warranty_scope_ok")
        compliance = {}
        for i, req in enumerate(reqs):
            e1, e2, e3 = st.columns(3)
            claimed = sub_comp.get(req)
            hint = "declared" if claimed is True else (claimed.get("status") if isinstance(claimed, dict)
                                                       else "missing")
            idx = COMPLIANCE_STATUSES.index(hint) if hint in COMPLIANCE_STATUSES else 2
            status = e1.selectbox(f"{req} status", COMPLIANCE_STATUSES, index=idx, key=f"{k}comp_{i}_status")
            ev = e2.text_input(f"{req} evidence reference", key=f"{k}comp_{i}_evidence")
            exp = e3.text_input(f"{req} expires on (YYYY-MM-DD)", key=f"{k}comp_{i}_expires")
            compliance[req] = (status, ev, exp)
        note = st.text_area(f"Reviewer note (required, at least {MIN_REASON} characters)", key=k + "note",
                            max_chars=1000)
        confirm = st.checkbox("I confirm I checked these terms against the source quotation.", key=k + "confirm")
        submitted = st.form_submit_button("Verify quotation terms")
    if not submitted:
        return
    if not confirm:
        _flash("verify", "error", "Please confirm you checked the terms against the source quotation.")
        st.rerun()
    if not _reason_ok(note):
        _flash("verify", "error", f"A reviewer note of at least {MIN_REASON} characters is required.")
        st.rerun()
    if not money["unit_price"].strip() or not scope.strip():
        _flash("verify", "error", "Unit price and warranty scope are required.")
        st.rerun()
    comp_body = {}
    for req, (status, ev, exp) in compliance.items():
        entry: dict = {"status": status}
        if status == "verified" and not ev.strip():
            _flash("verify", "error", f"Verified compliance for {req} needs an evidence reference.")
            st.rerun()
        if ev.strip():
            entry["evidence_ref"] = ev.strip()
        if exp.strip():
            try:
                entry["expires_on"] = date.fromisoformat(exp.strip()).isoformat()
            except ValueError:
                _flash("verify", "error", f"Invalid expiry date for {req} (use YYYY-MM-DD).")
                st.rerun()
        comp_body[req] = entry
    body = {"currency": currency.strip().upper(), **{f: (v.strip() or "0") for f, v in money.items()},
            "delivery_date": ddate.isoformat(), "warranty_months": int(warranty), "warranty_scope": scope.strip(),
            "warranty_scope_accepted": bool(scope_ok), "compliance": comp_body, "reviewer_note": note.strip()}
    try:
        data = vs_verify_quote(rfq_id, qid, body)
        _flash("verify", "info", f"Quotation {qid} terms verified by a human reviewer.")
        _audit_note("verify", data)
    except Exception as exc:  # noqa: BLE001
        _flash("verify", "error", _err(exc))
    st.rerun()


def _on_prepare_pdf(rfq_id: str, qid: str) -> None:
    try:
        st.session_state["vs_pdf_bytes"] = (qid, vs_download_quote_pdf(rfq_id, qid))
    except Exception as exc:  # noqa: BLE001
        _flash("verify", "error", _err(exc))


def _quote_review(rfq_id: str, rfq: dict, quotes: list) -> None:
    st.subheader("Review and verify a quotation")
    _show_flash("verify")
    _show_flash("verify_audit")
    by_id = {q["quote_id"]: q for q in quotes if q.get("quote_id")}
    if not by_id:
        st.caption("No quotations to review yet.")
        return
    qid = st.selectbox("Quotation", [None] + list(by_id), key="vs_quote_pick",
                       format_func=lambda x: "(select a quotation)" if x is None
                       else f"{x} · {by_id[x].get('vendor_id')} · {_dash(by_id[x].get('status')).replace('_', ' ')}")
    if not qid:
        return
    q = by_id[qid]
    sub = _submitted(q)
    st.markdown("**Submitted values**")
    _kv([("Vendor", q.get("vendor_id")), ("Quote reference", q.get("quote_reference")),
         ("Submission type", q.get("submission_type")), ("Status", _dash(q.get("status")).replace("_", " ")),
         ("Revision", q.get("revision")), ("Revision of", q.get("revision_of")),
         ("Currency", sub.get("currency")), ("Unit price", sub.get("unit_price")),
         ("Tax amount", sub.get("tax_amount")), ("Delivery fee", sub.get("delivery_fee")),
         ("Other fees", sub.get("other_fees")), ("Delivery date", sub.get("delivery_date")),
         ("Warranty (months)", sub.get("warranty_months")), ("Warranty scope", sub.get("warranty_scope")),
         ("Compliance (vendor claim)", sub.get("compliance")), ("PDF file", q.get("pdf_filename")),
         ("PDF SHA-256", q.get("pdf_sha256")), ("PDF pages", q.get("page_count"))])
    if q.get("submission_type") == "pdf" or q.get("extracted_text") is not None:
        st.warning(f"⚠️ {UNTRUSTED_TEXT}")
        st.text_area("Extracted PDF text (untrusted, read-only)", value=str(q.get("extracted_text") or ""),
                     disabled=True, height=200, key=f"vs_extracted_{qid}")
        st.button("Fetch original PDF", key="vs_pdf_fetch_btn", on_click=_on_prepare_pdf, args=(rfq_id, qid))
        held = st.session_state.get("vs_pdf_bytes")
        if isinstance(held, tuple) and held[0] == qid and held[1]:
            st.download_button("⬇️ Download original PDF", data=held[1], file_name=q.get("pdf_filename") or
                               f"{qid}.pdf", mime="application/pdf", key="vs_pdf_dl")
    ver = _verified(q)
    if q.get("status") == "pending_verification":
        _verify_form(rfq_id, rfq, q)
    elif ver is not None:
        verification = q.get("verification") if isinstance(q.get("verification"), dict) else {}
        st.markdown("**Verified terms**")
        _kv([("Verified by", verification.get("reviewer") or q.get("verified_by")),
             ("Verified at", fmt_ts(verification.get("verified_at") or q.get("verified_at"))),
             ("Reviewer note", verification.get("reviewer_note") or q.get("reviewer_note")),
             *[(FIELD_LABELS[f], ver.get(f)) for f in FIELD_LABELS if f in ver]])
        st.markdown("**Submitted vs verified (changes)**")
        rows = _diff_rows(q)
        if rows:
            _table(rows)
        else:
            st.caption("No differences between submitted and verified values.")


def tab_quotes(ctx: _Ctx) -> None:
    banner()
    rid = _active_rfq_id()
    if not rid:
        st.info(NO_RFQ)
        return
    detail, err = ctx.rfq(rid)
    if err:
        st.error(err)
        return
    rfq = detail.get("rfq") or {}
    quotes = [q for q in detail.get("quotes") or [] if isinstance(q, dict)]
    st.markdown(f"**Active RFQ:** `{rid}` — status **{_dash(rfq.get('status'))}**")
    _show_flash("quote")
    _show_flash("quote_audit")
    if rfq.get("status") == "open":
        _structured_quote_form(rid, rfq, quotes)
        _pdf_upload(rid, rfq)
    else:
        st.info(f"This RFQ is {_dash(rfq.get('status'))}; new quotations are not accepted.")
    st.divider()
    _quote_review(rid, rfq, quotes)


# --------------------------------------------------------------------------- tab: compare
def _score_text(score: Any) -> str:
    if isinstance(score, dict):
        parts = []
        total = score.get("total", score.get("composite"))
        if total is not None:
            parts.append(f"total {total}")
        comps = score.get("components") if isinstance(score.get("components"), dict) else {
            k: v for k, v in score.items() if k not in ("total", "composite")}
        parts += [f"{k} {_dash(v)}" for k, v in comps.items()]
        return "; ".join(parts) or "—"
    return _dash(score)


def _compliance_text(comp: Any) -> str:
    if not isinstance(comp, dict) or not comp:
        return "—"
    out = []
    for req, val in comp.items():
        if isinstance(val, dict):
            extra = ", ".join(str(x) for x in (val.get("evidence_ref"), val.get("expires_on")) if x)
            out.append(f"{req}: {val.get('status') or '?'}" + (f" ({extra})" if extra else ""))
        else:
            out.append(f"{req}: {'declared' if val is True else 'missing' if val is False else _dash(val)}")
    return "; ".join(out)


def _reasons_text(row: dict) -> str:
    reasons = list(row.get("reasons") or [])
    if row.get("reason") and row["reason"] not in reasons:
        reasons.insert(0, row["reason"])
    return "; ".join(str(r) for r in reasons) or "—"


def _compare_rows(rows: list) -> list[dict]:
    def key(r):
        return (r.get("rank") is None, r.get("rank") or 0, str(r.get("quote_id")))
    return [{"Rank": _dash(r.get("rank")), "Quote": r.get("quote_id"), "Vendor": _dash(r.get("vendor_id")),
             "Status": _dash(r.get("status")).replace("_", " "), "Eligible": _dash(bool(r.get("eligible"))),
             "Unit price": _dash(r.get("unit_price")), "Quantity": _dash(r.get("quantity")),
             "Tax": _dash(r.get("tax_amount")), "Delivery fee": _dash(r.get("delivery_fee")),
             "Other fees": _dash(r.get("other_fees")), "Total cost": _dash(r.get("total_cost")),
             "Delivery date": _dash(r.get("delivery_date")), "Warranty (months)": _dash(r.get("warranty_months")),
             "Compliance": _compliance_text(r.get("compliance")),
             "Missing compliance": _dash(r.get("missing_compliance")),
             "Eligibility reasons": _reasons_text(r), "Score (explanatory)": _score_text(r.get("score"))}
            for r in sorted([r for r in rows if isinstance(r, dict)], key=key)]


def _on_compare(rfq_id: str, ctx: _Ctx) -> None:
    try:
        data = vs_compare(rfq_id)
        if isinstance(data, dict):
            st.session_state.setdefault("vs_compare", {})[rfq_id] = data
            _audit_note("compare", data)
        st.session_state["vs_cmp_confirm"] = False
    except Exception as exc:  # noqa: BLE001
        _flash("compare", "error", _err(exc))
    ctx.invalidate()


def _on_selection(rfq_id: str, comparison_id: str, ctx: _Ctx) -> None:
    decision = st.session_state.get("vs_cmp_decision")
    quote_id = st.session_state.get("vs_cmp_quote") if decision == "accept" else None
    reason = st.session_state.get("vs_cmp_reason") or ""
    if not st.session_state.get("vs_cmp_confirm"):
        _flash("compare", "error", "Please confirm first.")
        return
    if not _reason_ok(reason):
        _flash("compare", "error", f"A reason of at least {MIN_REASON} characters is required.")
        return
    if decision == "accept" and not quote_id:
        _flash("compare", "error", "Select the eligible quotation you are accepting.")
        return
    try:
        data = vs_comparison_decision(rfq_id, comparison_id, decision, quote_id, reason.strip())
        what = f"quotation {quote_id} accepted" if decision == "accept" else "comparison rejected"
        _flash("compare", "info", f"Human selection recorded ({what}). This is a sourcing record only — no "
                                  "supplier has been contacted and no purchase order has been created or dispatched.")
        _audit_note("compare", data)
        cmp = (st.session_state.get("vs_compare") or {}).get(rfq_id)
        if isinstance(cmp, dict):
            cmp["human_decision"] = decision
            cmp["selected_quote_id"] = quote_id
        st.session_state["vs_cmp_reason"] = ""
    except Exception as exc:  # noqa: BLE001
        _flash("compare", "error", _err(exc))
    st.session_state["vs_cmp_confirm"] = False
    ctx.invalidate()


def tab_compare(ctx: _Ctx) -> None:
    banner()
    rid = _active_rfq_id()
    if not rid:
        st.info(NO_RFQ)
        return
    detail, err = ctx.rfq(rid)
    if err:
        st.error(err)
        return
    rfq = detail.get("rfq") or {}
    st.markdown(f"**Active RFQ:** `{rid}` — status **{_dash(rfq.get('status'))}**")
    _show_flash("compare")
    _show_flash("compare_audit")
    cancelled = rfq.get("status") == "cancelled"
    if cancelled:
        st.info("This RFQ is cancelled; comparisons are not allowed.")
    st.button("Run comparison", key="vs_compare_btn", disabled=cancelled, on_click=_on_compare, args=(rid, ctx))
    cmp = (st.session_state.get("vs_compare") or {}).get(rid)
    if not isinstance(cmp, dict):
        st.caption("Run a comparison to see the gate results and ranking for verified quotations.")
        return
    if cmp.get("notice"):
        st.info(str(cmp["notice"])[:300])
    _kv([("Comparison ID", cmp.get("comparison_id")), ("Scoring policy version", cmp.get("scoring_policy_version")),
         ("Ranking method", cmp.get("ranking_method")),
         ("Budget limit", f"{_dash(cmp.get('budget_limit'))} {cmp.get('currency') or ''}"),
         ("Eligible quotations", cmp.get("eligible_count")),
         ("Recommended quotation (advisory)", cmp.get("recommended_quote_id")),
         ("RFQ snapshot hash", cmp.get("rfq_snapshot_hash")),
         ("Human decision", cmp.get("human_decision") or "pending")])
    rows = [r for r in cmp.get("quotes") or cmp.get("rows") or [] if isinstance(r, dict)]
    if rows:
        _table(_compare_rows(rows))
    else:
        st.caption("No quotations to compare.")
    st.caption("Ranking: hard gates first (verification, currency, delivery, warranty, compliance, budget), then "
               "lowest total cost. The score is explanatory only and does not change the ranking.")
    cid = cmp.get("comparison_id")
    if not cid:
        st.caption("This comparison has no ID, so a human selection cannot be recorded against it.")
        return
    if (cmp.get("human_decision") or "pending") != "pending":
        return
    eligible = [r["quote_id"] for r in rows if r.get("eligible") and r.get("quote_id")]
    st.markdown("**Human selection (required — nothing is selected automatically)**")
    decision = st.selectbox("Decision", list(SELECTION_LABELS), key="vs_cmp_decision",
                            format_func=SELECTION_LABELS.get)
    if decision == "accept":
        rec = cmp.get("recommended_quote_id")
        st.selectbox("Selected quotation (eligible only)", eligible, key="vs_cmp_quote",
                     index=eligible.index(rec) if rec in eligible else 0 if eligible else None)
    st.text_area(f"Reason (required, at least {MIN_REASON} characters)", key="vs_cmp_reason", max_chars=1000)
    ok = st.checkbox("I confirm this human selection. I understand no supplier is contacted and no purchase "
                     "order is created or dispatched.", key="vs_cmp_confirm")
    st.button("Record human selection", key="vs_cmp_decide_btn",
              disabled=not ok or (decision == "accept" and not eligible), on_click=_on_selection,
              args=(rid, cid, ctx))


# --------------------------------------------------------------------------- tab: audit
def _events_table(events: list) -> None:
    events = sorted([e for e in events if isinstance(e, dict)], key=lambda e: str(e.get("created_at") or ""))
    rows = []
    for e in events:
        details = e.get("details") if isinstance(e.get("details"), dict) else {}
        rows.append({"At": fmt_ts(e.get("created_at") or e.get("at")),
                     "Event": _dash(e.get("event_type") or e.get("action")).replace("_", " "),
                     "Actor": _dash(e.get("actor")),
                     "From": _dash(e.get("from_status") or details.get("from_status")),
                     "To": _dash(e.get("to_status") or details.get("to_status")),
                     "Reason": _dash(e.get("reason") or details.get("reason")),
                     "Details": _dash(details)})
    _table(rows)


def tab_audit(ctx: _Ctx) -> None:
    banner()
    st.subheader("Candidate audit history")
    try:
        cands = [c for c in vs_search("").get("candidates") or [] if isinstance(c, dict) and c.get("candidate_id")]
    except Exception as exc:  # noqa: BLE001
        st.error(_err(exc))
        cands = []
    by_id = {c["candidate_id"]: c for c in cands}
    cid = st.selectbox("Candidate", [None] + list(by_id), key="vs_audit_cand",
                       format_func=lambda c: "(select a candidate)" if c is None
                       else f"{c} · {by_id[c].get('name') or ''}")
    if cid:
        try:
            events = vs_candidate_events(cid)
            if events:
                _events_table(events)
            else:
                st.caption("No events recorded.")
        except Exception as exc:  # noqa: BLE001
            st.error(_err(exc))
    st.subheader("RFQ audit history")
    rid = _active_rfq_id()
    if not rid:
        st.caption("Select an RFQ in the RFQs tab to see its events.")
    else:
        detail, err = ctx.rfq(rid)
        if err:
            st.error(err)
        else:
            events = [e for e in (detail or {}).get("events") or [] if isinstance(e, dict)]
            if events:
                _events_table(events)
            else:
                st.caption("No RFQ events were returned with this RFQ.")
    st.subheader("Sourcing metrics (this process)")
    try:
        metrics = vs_metrics()
    except Exception as exc:  # noqa: BLE001
        st.caption(f"Metrics unavailable: {_err(exc)}")
        return
    rows = [{"Metric": str(k).replace("_", " "), "Value": _dash(v)} for k, v in metrics.items()]
    if rows:
        _table(rows)
    else:
        st.caption("No metrics recorded yet.")


# --------------------------------------------------------------------------- page
def render() -> None:
    st.title("🏪 Vendor sourcing")
    if (current_user() or {}).get("role") != "admin":
        banner()
        st.error(NOT_AVAILABLE)
        return
    ctx = _Ctx()
    tabs = st.tabs(["🔎 Vendor search", "🧾 Candidates", "📨 RFQs", "💬 Quotes", "⚖️ Compare", "📜 Audit"])
    with tabs[0]:
        tab_search()
    with tabs[1]:
        tab_candidates()
    with tabs[2]:
        tab_rfqs(ctx)
    with tabs[3]:
        tab_quotes(ctx)
    with tabs[4]:
        tab_compare(ctx)
    with tabs[5]:
        tab_audit(ctx)
