"""Policy library: PDF upload (admin), document inventory, policy search and rate-card verification.

Admins upload/reprocess/archive documents; approvers (and admins) search the library and verify or reject
extracted rate-card rows. Requesters never reach this page. The backend is the only permission check and the
only source of truth: nothing is shown as successful unless the server confirmed it.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any, Optional
from urllib.parse import quote

import streamlit as st

from ui.lib.api import ApiError, api_get, api_post, api_post_files, current_user, friendly_error
from ui.lib.policy_render import DEGRADED_WARNING, render_citations

DOCUMENT_TYPES = ["company_policy", "vendor_policy", "vendor_contract", "privacy_policy", "security_policy",
                  "rate_card", "procurement_guideline", "other"]
DOCUMENT_STATUSES = ["uploaded", "validating", "extracting", "needs_ocr", "indexing", "ready", "needs_review",
                     "failed", "superseded", "archived"]
DOC_STATUS_BADGE = {
    "ready": "✅ ready", "needs_review": "🟠 needs review", "failed": "❌ failed", "superseded": "↪️ superseded",
    "archived": "🗄️ archived", "needs_ocr": "🔤 needs OCR", "uploaded": "⏳ uploaded",
    "validating": "⏳ validating", "extracting": "⚙️ extracting", "indexing": "⚙️ indexing",
}
VERIFICATION_BADGE = {"needs_review": "🟠 needs review", "verified": "✅ verified", "rejected": "⛔ rejected"}
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
INSUFFICIENT = ("Insufficient evidence — no sufficiently relevant passage was found in the ready policy "
                "documents. Do not rely on this search to decide.")
NOT_ALLOWED = "The policy library is available to admins and approvers only."
UPLOAD_UNCONFIRMED = "Upload outcome could not be confirmed — reload the inventory before retrying."


def _role() -> str:
    return str((current_user() or {}).get("role") or "")


def _flash(kind: str, text: str) -> None:
    st.session_state["pl_flash"] = (kind, text)


def doc_badge(status: Optional[str]) -> str:
    return DOC_STATUS_BADGE.get(status or "", f"❔ {(status or 'unknown').replace('_', ' ')}")


def _items(data: Any, *names: str) -> list:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for n in names:
            if isinstance(data.get(n), list):
                return [d for d in data[n] if isinstance(d, dict)]
    return []


# --------------------------------------------------------------------------- upload (admin)
def _parse_date(text: str) -> tuple[Optional[str], Optional[str]]:
    text = (text or "").strip()
    if not text:
        return None, None
    try:
        return date.fromisoformat(text).isoformat(), None
    except ValueError:
        return None, f"invalid date `{text}` (use YYYY-MM-DD)"


def _file_metadata(i: int, name: str) -> tuple[dict, list[str]]:
    """Collect one file's PolicyDocumentMetadataIn from its widgets; return (metadata, client errors)."""
    p = f"pl_meta_{i}"
    meta: dict = {"document_type": st.session_state.get(f"{p}_type") or "other"}
    errors: list[str] = []
    for field in ("vendor_key", "logical_key"):
        v = (st.session_state.get(f"{p}_{field}") or "").strip()
        if v:
            if not KEY_RE.match(v):
                errors.append(f"{field.replace('_', ' ')} must be lowercase letters, digits, '.', '_' or '-'")
            meta[field] = v
    for field in ("version_label", "department", "jurisdiction"):
        v = (st.session_state.get(f"{p}_{field}") or "").strip()
        if v:
            meta[field] = v
    dates = {}
    for field in ("effective_from", "effective_to"):
        val, err = _parse_date(st.session_state.get(f"{p}_{field}") or "")
        if err:
            errors.append(f"{field.replace('_', ' ')}: {err}")
        elif val:
            meta[field] = dates[field] = val
    if len(dates) == 2 and dates["effective_to"] < dates["effective_from"]:
        errors.append("effective to must not be before effective from")
    return meta, errors


def _metadata_widgets(i: int, name: str) -> None:
    p = f"pl_meta_{i}"
    with st.container(border=True):
        st.markdown(f"**{i + 1}. {name}**")
        c1, c2, c3 = st.columns(3)
        c1.selectbox("Document type", DOCUMENT_TYPES, key=f"{p}_type",
                     format_func=lambda t: t.replace("_", " "))
        c2.text_input("Vendor key (optional)", key=f"{p}_vendor_key", max_chars=64, placeholder="evt-ven-001")
        c3.text_input("Version label (optional)", key=f"{p}_version_label", max_chars=64, placeholder="2.1")
        c4, c5, c6 = st.columns(3)
        c4.text_input("Logical key (optional)", key=f"{p}_logical_key", max_chars=96,
                      help="Links versions of the same document; a new upload with the same key supersedes the "
                           "previous ready version.")
        c5.text_input("Effective from (YYYY-MM-DD)", key=f"{p}_effective_from", max_chars=10)
        c6.text_input("Effective to (YYYY-MM-DD)", key=f"{p}_effective_to", max_chars=10)
        c7, c8 = st.columns(2)
        c7.text_input("Department (optional)", key=f"{p}_department", max_chars=64)
        c8.text_input("Jurisdiction (optional)", key=f"{p}_jurisdiction", max_chars=16)


def _validation_errors_by_file(exc: ApiError, names: list[str]) -> dict[int, list[str]]:
    """Map FastAPI 422 entries like loc=["body","metadata",1,"effective_to"] to file indexes."""
    out: dict[int, list[str]] = {}
    body = exc.body if isinstance(exc.body, dict) else {}
    detail = body.get("detail")
    entries = detail if isinstance(detail, list) else []
    if isinstance(detail, dict) and isinstance(detail.get("errors"), list):
        entries = detail["errors"]
    for d in entries:
        if not isinstance(d, dict):
            continue
        idx = d.get("index")
        loc = [x for x in d.get("loc") or [] if x not in ("body", "metadata", "files")]
        if idx is None:
            idx = next((x for x in loc if isinstance(x, int)), None)
        if idx is None and d.get("filename") in names:
            idx = names.index(d["filename"])
        field = ".".join(str(x) for x in loc if not isinstance(x, int))
        msg = str(d.get("msg") or d.get("message") or d)
        out.setdefault(idx if isinstance(idx, int) and 0 <= idx < len(names) else -1, []).append(
            f"{field}: {msg}" if field else msg)
    return out


def upload_section() -> None:
    st.subheader("⬆️ Upload policy PDFs")
    st.caption("Upload one or more PDFs. Each file is validated, extracted and indexed independently; one bad "
               "file never blocks the others. Rate-card rows always start as *needs review*.")
    files = st.file_uploader("PDF files", type=["pdf"], accept_multiple_files=True, key="pl_files")
    files = list(files or [])
    for i, f in enumerate(files):
        _metadata_widgets(i, f.name)
    inflight = bool(st.session_state.get("pl_upload_inflight"))
    if st.button(f"Upload {len(files)} file(s)" if files else "Upload", key="pl_upload_btn", type="primary",
                 disabled=not files or inflight):
        names = [f.name for f in files]
        metas, client_errors = [], {}
        for i, f in enumerate(files):
            meta, errs = _file_metadata(i, f.name)
            metas.append(meta)
            if errs:
                client_errors[i] = errs
        if client_errors:
            st.session_state["pl_upload_view"] = {"names": names, "errors": client_errors}
            st.rerun()
        payload = [("files", (f.name, f.getvalue(), "application/pdf")) for f in files]
        st.session_state.pop("pl_upload_view", None)
        st.session_state["pl_upload_inflight"] = True
        try:
            data = api_post_files("/api/policy-documents/upload", payload, {"metadata": json.dumps(metas)})
        except ApiError as exc:
            view: dict = {"names": names}
            if exc.status_code == 413:
                view["error"] = f"File too large — nothing was uploaded. {exc.detail}".strip()
            elif exc.status_code == 422:
                view["errors"] = _validation_errors_by_file(exc, names) or {-1: [exc.detail]}
            elif exc.status_code >= 500:
                view["error"] = f"{friendly_error(exc)} {UPLOAD_UNCONFIRMED}"
            else:
                view["error"] = friendly_error(exc)
            st.session_state["pl_upload_view"] = view
        except Exception as exc:
            st.session_state["pl_upload_view"] = {"names": names,
                                                  "error": f"{friendly_error(exc)} {UPLOAD_UNCONFIRMED}"}
        else:
            results = _items(data, "results")
            if not results:
                st.session_state["pl_upload_view"] = {"names": names, "error": UPLOAD_UNCONFIRMED}
            else:
                st.session_state["pl_upload_view"] = {"names": names, "results": results}
        finally:
            st.session_state.pop("pl_upload_inflight", None)
        st.rerun()
    render_upload_view(st.session_state.get("pl_upload_view"))


def render_upload_view(view: Optional[dict]) -> None:
    if not isinstance(view, dict):
        return
    names = view.get("names") or []
    if view.get("error"):
        st.error(view["error"])
    errors = view.get("errors") or {}
    for idx, msgs in sorted(errors.items(), key=lambda kv: kv[0]):
        who = names[idx] if isinstance(idx, int) and 0 <= idx < len(names) else "Request"
        st.error(f"❌ **{who}** — metadata error: " + "; ".join(str(m) for m in msgs))
    results = view.get("results") or []
    if not results:
        return
    ok = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    summary = f"Server processed {len(results)} file(s): {len(ok)} accepted, {len(failed)} failed."
    (st.success if ok and not failed else st.warning if ok else st.error)(summary)
    st.dataframe([
        {
            "File": r.get("filename") or "—",
            "Result": "✅ accepted" if r.get("ok") else "❌ failed",
            "Status": doc_badge(r.get("status")) if r.get("status") else "—",
            "Document": r.get("document_id") or "—",
            "Duplicate of": r.get("duplicate_of") or "—",
            "Error": r.get("error_code") or "—",
            "Message": r.get("message") or "—",
        }
        for r in results
    ], width="stretch", hide_index=True)
    for r in results:
        if r.get("duplicate_of"):
            st.info(f"♻️ **{r.get('filename')}** is a duplicate of `{r['duplicate_of']}` — not re-ingested.")
        if not r.get("ok"):
            st.error(f"❌ **{r.get('filename')}** failed: {r.get('error_code') or 'error'}"
                     + (f" — {r['message']}" if r.get("message") else ""))


# --------------------------------------------------------------------------- inventory
def _version_marker(d: dict) -> str:
    marks = []
    if d.get("supersedes"):
        marks.append(f"supersedes {d['supersedes']}")
    if d.get("superseded_by"):
        marks.append(f"↪️ superseded by {d['superseded_by']}")
    if d.get("duplicate_of"):
        marks.append(f"♻️ duplicate of {d['duplicate_of']}")
    return "; ".join(marks) or "—"


def inventory_section(is_admin: bool) -> None:
    top1, top2 = st.columns([5, 1])
    top1.subheader("📚 Document inventory")
    if top2.button("🔄 Refresh", key="pl_inv_refresh"):
        st.rerun()
    c1, c2, c3 = st.columns(3)
    dtype = c1.selectbox("Type", [None] + DOCUMENT_TYPES, key="pl_inv_type",
                         format_func=lambda t: "All types" if t is None else t.replace("_", " "))
    dstatus = c2.selectbox("Status", [None] + DOCUMENT_STATUSES, key="pl_inv_status",
                           format_func=lambda s: "All statuses" if s is None else doc_badge(s))
    vendor = c3.text_input("Vendor key", key="pl_inv_vendor", max_chars=64).strip()
    params = {k: v for k, v in (("document_type", dtype), ("status", dstatus), ("vendor_key", vendor)) if v}
    try:
        data = api_get("/api/policy-documents", params=params or None)
    except Exception as exc:
        st.error(friendly_error(exc))
        return
    docs = _items(data, "documents", "items")
    if not docs:
        st.info("No policy documents match these filters.")
        return
    st.dataframe([
        {
            "Document": d.get("document_id"),
            "File": d.get("filename") or "—",
            "Type": str(d.get("document_type") or "—").replace("_", " "),
            "Vendor": d.get("vendor_key") or "—",
            "Version": f"v{d.get('version', '?')}" + (f" ({d['version_label']})" if d.get("version_label") else ""),
            "Effective": (f"{d.get('effective_from') or '…'} → {d.get('effective_to') or '…'}"
                          if d.get("effective_from") or d.get("effective_to") else "—"),
            "Status": doc_badge(d.get("status")),
            "Pages": d.get("page_count", 0),
            "Low-text pages": ", ".join(str(p) for p in d.get("low_text_pages") or []) or "—",
            "Chunks": d.get("chunk_count", 0),
            "Rate entries": d.get("rate_entry_count", 0),
            "Versions": _version_marker(d),
            "Error": d.get("error_code") or "—",
        }
        for d in docs
    ], width="stretch", hide_index=True)
    if is_admin:
        actions_section(docs)
    else:
        st.caption("Read-only: only admins can upload, reprocess or archive documents.")


def actions_section(docs: list) -> None:
    ids = [d["document_id"] for d in docs if d.get("document_id")]
    labels = {d["document_id"]: f"{d['document_id']} · {d.get('filename', '')} · {doc_badge(d.get('status'))}"
              for d in docs if d.get("document_id")}
    pending = st.session_state.get("pl_doc_pending")
    with st.container(border=True):
        st.markdown("**🛠️ Document actions**")
        if isinstance(pending, dict):
            verb = "Reprocess" if pending["action"] == "reprocess" else "Archive"
            st.warning(f"Confirm: **{verb.lower()}** `{pending['document_id']}`?"
                       + (" Archived documents are excluded from search; audit history is kept."
                          if pending["action"] == "archive" else
                          " The document is re-extracted and re-indexed from the stored file."))
            checked = st.checkbox(f"Yes, {verb.lower()} this document.", key="pl_doc_confirm_check")
            b1, b2 = st.columns(2)
            if b1.button(f"Confirm {verb.lower()}", key="pl_doc_confirm_btn", type="primary", disabled=not checked):
                _doc_action(pending["document_id"], pending["action"])
                st.rerun()
            if b2.button("Cancel", key="pl_doc_cancel_btn"):
                st.session_state.pop("pl_doc_pending", None)
                st.session_state.pop("pl_doc_confirm_check", None)
                st.rerun()
            return
        doc_id = st.selectbox("Document", [None] + ids, key="pl_doc_pick",
                              format_func=lambda x: "(select a document)" if x is None else labels.get(x, x))
        b1, b2 = st.columns(2)
        for col, action, label in ((b1, "reprocess", "🔁 Reprocess…"), (b2, "archive", "🗄️ Archive…")):
            if col.button(label, key=f"pl_{action}_btn", disabled=not doc_id):
                st.session_state["pl_doc_pending"] = {"document_id": doc_id, "action": action}
                st.rerun()


def _doc_action(document_id: str, action: str) -> None:
    st.session_state.pop("pl_doc_pending", None)
    st.session_state.pop("pl_doc_confirm_check", None)
    try:
        data = api_post(f"/api/policy-documents/{quote(document_id, safe='')}/{action}", None, timeout=300)
    except Exception as exc:
        _flash("error", friendly_error(exc))
        return
    status = data.get("status") if isinstance(data, dict) else None
    if status is None:
        _flash("warning", f"{action.capitalize()} outcome could not be confirmed — refresh the inventory.")
        return
    _flash("success", f"Server confirmed {action} of `{document_id}` — status {doc_badge(status)}.")


# --------------------------------------------------------------------------- search
def search_section() -> None:
    st.subheader("🔎 Policy search")
    with st.form("pl_search_form"):
        query = st.text_input("Question or keywords", key="pl_search_query", max_chars=500,
                              placeholder="insurance requirements for event venues")
        c1, c2, c3 = st.columns(3)
        vendor = c1.text_input("Vendor key (optional)", key="pl_search_vendor", max_chars=64)
        types = c2.multiselect("Document types", DOCUMENT_TYPES, key="pl_search_types",
                               format_func=lambda t: t.replace("_", " "))
        top_k = c3.number_input("Results", min_value=1, max_value=20, value=8, step=1, key="pl_search_topk")
        historical = st.checkbox("Include superseded/historical versions", key="pl_search_hist")
        submitted = st.form_submit_button("Search", type="primary")
    if submitted:
        q = (query or "").strip()
        if len(q) < 2:
            st.error("Enter at least 2 characters to search.")
        else:
            body: dict = {"query": q, "top_k": int(top_k), "include_historical": bool(historical)}
            if (vendor or "").strip():
                body["vendor_key"] = vendor.strip()
            if types:
                body["document_types"] = list(types)
            try:
                st.session_state["pl_search_result"] = api_post("/api/policy/search", body, timeout=60)
            except Exception as exc:
                st.session_state.pop("pl_search_result", None)
                st.error(friendly_error(exc))
    render_search_result(st.session_state.get("pl_search_result"))


def render_search_result(res: Any) -> None:
    if not isinstance(res, dict):
        return
    mode = res.get("retrieval_mode") or "unknown"
    st.caption(f"Retrieval mode: **{mode}** · query `{res.get('query_id', '—')}`")
    if mode == "keyword_only_degraded":
        st.warning(DEGRADED_WARNING)
    for w in res.get("warnings") or []:
        st.warning(f"⚠️ {w}")
    evidence = [e for e in res.get("evidence") or [] if isinstance(e, dict)]
    if res.get("insufficient_evidence") or not evidence:
        st.info(f"❔ {INSUFFICIENT}")
    if evidence:
        st.markdown(f"**{len(evidence)} citation(s)**")
        render_citations(evidence)


# --------------------------------------------------------------------------- rate cards
def rate_cards_section() -> None:
    top1, top2 = st.columns([5, 1])
    top1.subheader("💱 Rate-card verification")
    if top2.button("🔄 Refresh", key="pl_rate_refresh"):
        st.rerun()
    st.caption("Extracted rate rows are candidates. Only verified rows are used to validate quotes. Compare each "
               "row against its source page before verifying.")
    c1, c2 = st.columns(2)
    vendor = c1.text_input("Vendor key", key="pl_rate_vendor", max_chars=64).strip()
    vstatus = c2.selectbox("Verification", [None, "needs_review", "verified", "rejected"], key="pl_rate_status",
                           format_func=lambda s: "All" if s is None else VERIFICATION_BADGE.get(s, s))
    params = {k: v for k, v in (("vendor_key", vendor), ("verification_status", vstatus)) if v}
    try:
        data = api_get("/api/rate-cards", params=params or None)
    except Exception as exc:
        st.error(friendly_error(exc))
        return
    rows = _items(data, "entries", "rate_cards", "items")
    if not rows:
        st.info("No rate-card entries match these filters.")
        return
    st.dataframe([
        {
            "Rate": r.get("rate_id"),
            "Vendor": r.get("vendor_key"),
            "Vendor name": r.get("vendor_name") or "—",
            "Item": r.get("item_or_service_code"),
            "Description": r.get("description") or "—",
            "Unit rate": f"{r.get('currency', 'INR')} {r.get('unit_rate')} / {r.get('unit', '?')}",
            "Tax": str(r.get("tax_treatment") or "—").replace("_", " "),
            "Source": f"{r.get('document_id', '?')} · p. {r.get('source_page', '?')}",
            "Confidence": r.get("extraction_confidence") or "—",
            "Verification": VERIFICATION_BADGE.get(r.get("verification_status"), r.get("verification_status")),
            "Verified by": r.get("verified_by") or "—",
        }
        for r in rows
    ], width="stretch", hide_index=True)
    for r in rows:
        if r.get("verification_status") != "needs_review" or not r.get("rate_id"):
            continue
        rid = r["rate_id"]
        with st.container(border=True):
            st.markdown(f"**{r.get('description') or r.get('item_or_service_code')}** · `{rid}` · "
                        f"{r.get('currency', 'INR')} {r.get('unit_rate')} / {r.get('unit', '?')}")
            st.caption(f"Source: document `{r.get('document_id', '?')}`, page {r.get('source_page', '?')} · "
                       f"extraction confidence {r.get('extraction_confidence', '?')}")
            b1, b2 = st.columns(2)
            if b1.button("✅ Verify", key=f"pl_rate_verify_{rid}"):
                _verify(rid, "verify")
                st.rerun()
            if b2.button("⛔ Reject", key=f"pl_rate_reject_{rid}"):
                _verify(rid, "reject")
                st.rerun()


def _verify(rate_id: str, decision: str) -> None:
    try:
        data = api_post(f"/api/rate-cards/{quote(rate_id, safe='')}/verify", {"decision": decision}, timeout=30)
    except Exception as exc:
        _flash("error", friendly_error(exc))
        return
    status = data.get("verification_status") if isinstance(data, dict) else None
    if isinstance(data, dict) and status is None and isinstance(data.get("entry"), dict):
        status = data["entry"].get("verification_status")
    expected = "verified" if decision == "verify" else "rejected"
    if status != expected:
        _flash("warning", f"Rate `{rate_id}`: the server did not confirm the change — refresh before retrying.")
        return
    by = data.get("verified_by") or (data.get("entry") or {}).get("verified_by") or "you"
    _flash("success", f"Server recorded rate `{rate_id}` as {VERIFICATION_BADGE.get(status, status)} by {by}.")


# --------------------------------------------------------------------------- page
def render() -> None:
    st.title("📚 Policy library")
    role = _role()
    if role not in ("admin", "approver"):
        st.error(NOT_ALLOWED)
        return
    is_admin = role == "admin"
    st.caption("Policy PDFs, vendor policies and rate cards used as cited evidence. RAG finds evidence; "
               "deterministic rules validate amounts; humans approve. Nothing here places an order or pays.")
    msg = st.session_state.pop("pl_flash", None)
    if msg:
        kind, text = msg
        {"error": st.error, "warning": st.warning}.get(kind, st.success)(text)
    if is_admin:
        t_up, t_inv, t_search, t_rate = st.tabs(["Upload", "Inventory", "Search", "Rate cards"])
        with t_up:
            upload_section()
    else:
        t_inv, t_search, t_rate = st.tabs(["Inventory", "Search", "Rate cards"])
    with t_inv:
        inventory_section(is_admin)
    with t_search:
        search_section()
    with t_rate:
        rate_cards_section()
