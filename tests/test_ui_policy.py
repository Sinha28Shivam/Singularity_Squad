"""Policy RAG UI tests (AppTest) against an in-memory fake backend (requests.get/post/put are patched)."""
from __future__ import annotations

import json
import re
from unittest.mock import patch

import pytest

pytest.importorskip("streamlit.testing.v1")
import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from test_ui_portal import APP, FakeBackend, FakeResponse, _path, texts  # noqa: E402

APPROVED_TEXT = "Approved internally — purchase order remains a draft; no order or payment has been executed."
EXCERPT_SECRET = "Vendors must carry public liability insurance of at least INR 50 lakh"
DOC_IDS = ["doc_0000000a", "doc_0000000b", "doc_0000000c"]


def _evidence(i: int, page: int, excerpt: str = EXCERPT_SECRET, score: float = 0.82) -> dict:
    return {"evidence_id": f"ev_{i}", "chunk_id": f"chk_{i}", "document_id": DOC_IDS[1],
            "filename": "Vander Policy.pdf", "content_hash": "c" * 64, "version_label": "1.0", "version": 1,
            "page_number": page, "section": "4.2 Insurance", "excerpt": excerpt, "relevance_score": score,
            "document_type": "vendor_policy", "vendor_key": None, "effective_from": None, "effective_to": None}


def evaluation(ticket_id: str, outcome: str = "review_required", mode: str = "hybrid") -> dict:
    return {
        "evaluation_id": "eval_0001", "ticket_id": ticket_id, "run_count": 1,
        "evaluated_at": "2026-10-17T04:00:00+00:00", "rule_set_version": "policy-rules-v1",
        "retrieval_mode": mode, "outcome": outcome,
        "findings": [
            {"finding_id": "f1", "category": "vendor_eligibility", "severity": "review_required",
             "status": "unknown", "summary": "Vendor insurance must be verified",
             "reason": "Event Vendor & Venue Policy requires proof of insurance.", "evidence_ids": ["ev_1"],
             "required_action": "Approver verifies insurance certificate", "rule_id": "vendor_verification",
             "source": "rule"},
            {"finding_id": "f2", "category": "documentation", "severity": "info", "status": "not_applicable",
             "summary": "No rate card applies to catering vendors", "reason": "cat- vendor", "evidence_ids": [],
             "rule_id": "rate_card_applicability", "source": "rule"},
            {"finding_id": "f3", "category": "security", "severity": "warning", "status": "conflict",
             "summary": "Two documents disagree on headcount threshold", "reason": "conflict",
             "evidence_ids": ["ev_2"], "rule_id": "conflict_check", "source": "rule"},
        ],
        "evidence": [_evidence(1, 4), _evidence(2, 7, "Risk assessment needed above 150 attendees.", 0.61)],
        "rate_validation": {"quoted_unit_rate": "500.00", "expected_unit_rate": "450.00", "currency": "INR",
                            "quantity": 100, "quoted_total": "50000.00", "expected_total": "45000.00",
                            "difference_amount": "5000.00", "difference_percentage": "11.11",
                            "rate_id": "rate_0001", "candidate_rate_ids": ["rate_0001"],
                            "source_document_id": DOC_IDS[2], "source_page": 3,
                            "rate_card_version": "SINGULARITY-RAG-MENU", "validation_status": "fail",
                            "reason": "Quoted rate exceeds the verified rate beyond tolerance."},
        "missing_evidence": ["No evidence found for dietary requirements policy"],
        "conflicts": ["Headcount threshold differs between documents"],
        "approval_reason_codes": ["POLICY_REVIEW_REQUIRED", "RATE_CARD_MISMATCH"],
        "blocking_findings": [], "documents_used": [{"document_id": DOC_IDS[1], "content_hash": "c" * 64,
                                                      "version": 1, "version_label": "1.0"}],
        "model_info": None, "summary": None, "evaluation_hash": "abcdef0123456789" + "0" * 48,
        "schema_version": 1,
    }


def redacted(ticket_id: str) -> dict:
    return {"ticket_id": ticket_id, "redacted": True, "outcome": "review_required", "retrieval_mode": "hybrid",
            "findings": [{"summary": "Vendor insurance must be verified", "severity": "review_required",
                          "status": "unknown",
                          "citations": [{"filename": "Vander Policy.pdf", "version_label": "1.0",
                                         "page_number": 4, "section": "4.2 Insurance"}]}],
            "citations": [{"filename": "Vander Policy.pdf", "version_label": "1.0", "page_number": 4,
                           "section": "4.2 Insurance"}]}


class PolicyBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.documents: dict[str, dict] = {}
        self.rates: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}
        self.search_result: dict = {"query_id": "q_1", "evidence": [_evidence(1, 4)], "retrieval_mode": "hybrid",
                                    "insufficient_evidence": False, "warnings": []}
        self.upload_status = 200
        self.upload_bodies: list = []
        for i, (name, dtype, status, extra) in enumerate([
            ("Company Policy.pdf", "company_policy", "ready", {"page_count": 16, "chunk_count": 40}),
            ("Vander Policy.pdf", "vendor_policy", "superseded", {"page_count": 10, "superseded_by": "doc_0000000d"}),
            ("Vander Manu.pdf", "rate_card", "needs_review", {"page_count": 6, "rate_entry_count": 12,
                                                              "low_text_pages": [5]}),
        ]):
            self.documents[DOC_IDS[i]] = {
                "document_id": DOC_IDS[i], "logical_key": name.lower().replace(" ", "-"), "filename": name,
                "content_hash": f"{i}" * 64, "document_type": dtype, "vendor_key": None, "version": 1,
                "version_label": "1.0", "status": status, "uploaded_by": "system",
                "uploaded_at": "2026-10-17T04:00:00+00:00", "page_count": 0, "low_text_pages": [],
                "chunk_count": 0, "rate_entry_count": 0, "error_code": None, **extra}
        self.documents["doc_0000000e"] = dict(self.documents[DOC_IDS[0]], document_id="doc_0000000e",
                                              filename="broken.pdf", status="failed", error_code="corrupt",
                                              page_count=0, chunk_count=0)
        self.rates["rate_0001"] = {
            "rate_id": "rate_0001", "document_id": DOC_IDS[2], "content_hash": "2" * 64, "vendor_key": "evt-ven-001",
            "vendor_name": "Insight Eventz", "item_or_service_code": "team_gathering",
            "description": "Team Gathering package", "unit_rate": "145000.00", "currency": "INR", "unit": "event",
            "tax_treatment": "excluded", "source_page": 2, "verification_status": "needs_review",
            "verified_by": None, "verified_at": None, "extraction_confidence": "high"}

    # ---- transport overrides (policy routes first, then the portal fake)
    def get(self, url, params=None, headers=None, timeout=None, **kw):
        path = _path(url)
        resp = self._policy("GET", path, params or {}, None, headers, kw)
        if resp is not None:
            self.calls.append(("GET", path, params, headers))
            return resp
        return super().get(url, params=params, headers=headers, timeout=timeout, **kw)

    def post(self, url, json=None, headers=None, timeout=None, **kw):
        path = _path(url)
        resp = self._policy("POST", path, {}, json, headers, kw)
        if resp is not None:
            self.calls.append(("POST", path, json if json is not None else kw, headers))
            return resp
        return super().post(url, json=json, headers=headers, timeout=timeout, **kw)

    def _policy(self, method, path, params, body, headers, kw):
        if not (path.startswith("/api/policy") or path.startswith("/api/rate-cards")
                or re.match(r"^/api/requests/[^/]+/evidence$", path)):
            return None
        user = self._user(headers)
        if user is None:
            return FakeResponse(401, {"detail": "Not authenticated."})
        m = re.match(r"^/api/requests/([^/]+)/evidence$", path)
        if m:
            t = self.tickets.get(m.group(1))
            if not t or m.group(1) not in self.evidence:
                return FakeResponse(404, {"detail": "No evidence."})
            if user["role"] in ("admin", "approver"):
                return FakeResponse(200, {"snapshot_id": "snap_1", "ticket_id": t["ticket_id"], "run_count": 1,
                                          "evaluation": self.evidence[t["ticket_id"]]})
            if t["owner_username"] != user["username"]:
                return FakeResponse(404, {"detail": "No evidence."})
            return FakeResponse(200, redacted(t["ticket_id"]))
        if user["role"] not in ("admin", "approver"):
            return FakeResponse(403, {"detail": "Approver or admin access required."})
        if path == "/api/policy-documents/upload" and method == "POST":
            if user["role"] != "admin":
                return FakeResponse(403, {"detail": "Admin access required."})
            self.upload_bodies.append(kw)
            if self.upload_status != 200:
                return FakeResponse(self.upload_status, {"detail": {
                    413: "Upload exceeds the 25 MB limit.", 503: "Database unavailable. Please try again later.",
                    422: [{"loc": ["body", "metadata", 1, "effective_to"],
                           "msg": "effective_to must not be before effective_from"}]}[self.upload_status]})
            metas = json.loads(kw["data"]["metadata"])
            results = []
            for (field, (name, data, mime)), meta in zip(kw["files"], metas):
                assert field == "files" and mime == "application/pdf"
                if not data.startswith(b"%PDF-"):
                    results.append({"filename": name, "ok": False, "status": "failed", "error_code": "not_pdf",
                                    "message": "File is not a valid PDF."})
                    continue
                did = f"doc_{len(self.documents) + 16:08x}"
                self.documents[did] = dict(self.documents[DOC_IDS[0]], document_id=did, filename=name,
                                           document_type=meta["document_type"], status="ready")
                results.append({"filename": name, "ok": True, "document_id": did, "status": "ready"})
            return FakeResponse(200, {"results": results})
        if path == "/api/policy-documents" and method == "GET":
            docs = [d for d in self.documents.values()
                    if all(not params.get(k) or d.get(k) == params[k] for k in ("document_type", "status",
                                                                                "vendor_key"))]
            return FakeResponse(200, {"documents": docs})
        m = re.match(r"^/api/policy-documents/([^/]+)/(reprocess|archive)$", path)
        if m:
            if user["role"] != "admin":
                return FakeResponse(403, {"detail": "Admin access required."})
            d = self.documents[m.group(1)]
            d["status"] = "archived" if m.group(2) == "archive" else "ready"
            return FakeResponse(200, d)
        if path == "/api/policy/search":
            return FakeResponse(200, self.search_result)
        if path == "/api/rate-cards" and method == "GET":
            return FakeResponse(200, {"entries": list(self.rates.values())})
        m = re.match(r"^/api/rate-cards/([^/]+)/verify$", path)
        if m:
            r = self.rates[m.group(1)]
            r.update(verification_status="verified" if body["decision"] == "verify" else "rejected",
                     verified_by=user["username"], verified_at="2026-10-17T05:00:00+00:00")
            return FakeResponse(200, r)
        return FakeResponse(404, {"detail": "Not found"})

    def admin_view(self, t):
        view = super().admin_view(t)
        if t["ticket_id"] in self.evidence and t.get("embed_eval"):
            view["policy_evaluation"] = self.evidence[t["ticket_id"]]
        return view


@pytest.fixture(autouse=True)
def _clear_caches():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.fixture
def backend():
    be = PolicyBackend()
    with patch("requests.get", side_effect=be.get), patch("requests.post", side_effect=be.post), \
            patch("requests.put", side_effect=be.put):
        yield be


def run(at: AppTest) -> AppTest:
    at.run()
    assert not at.exception, at.exception
    return at


def _render_library():
    from ui.pages_portal import policy_library
    policy_library.render()


def library(backend: PolicyBackend, user: str) -> AppTest:
    at = AppTest.from_function(_render_library, default_timeout=60)
    at.session_state["auth"] = backend.login_token(user)
    return run(at)


def logged_in(backend: PolicyBackend, user: str) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["auth"] = backend.login_token(user)
    return run(at)


def keys(at: AppTest) -> set:
    return {b.key for b in at.button}


def page_titles(at: AppTest) -> list:
    return [str(info.get("page_name")) for info in at._registered_pages.values()]


def all_text(at: AppTest) -> str:
    out = [texts(at)]
    for df in at.dataframe:
        out.append(df.value.to_string())
    return "\n".join(out)


# --------------------------------------------------------------------------- navigation
def test_navigation_per_role(backend):
    assert page_titles(logged_in(backend, "admin")) == ["Admin console", "Approvals (read-only)", "Policy library",
                                                         "Payments (Simulation)", "Vendor sourcing"]
    assert page_titles(logged_in(backend, "carol")) == ["Approvals", "Policy library",
                                                         "Payments (Simulation, read-only)"]
    assert page_titles(logged_in(backend, "alice")) == ["My requests"]
    at = library(backend, "alice")
    assert "admins and approvers only" in texts(at)
    assert not backend.find("GET", "/api/policy-documents")


# --------------------------------------------------------------------------- upload
def _upload(at: AppTest, files: list) -> AppTest:
    at.file_uploader(key="pl_files").set_value(files)
    run(at)
    return at


def test_admin_uploads_two_files_one_fails(backend):
    at = library(backend, "admin")
    assert "pl_upload_btn" in keys(at)
    _upload(at, [("procurement.pdf", b"%PDF-1.4 good", "application/pdf"),
                 ("broken.pdf", b"not a pdf", "application/pdf")])
    at.selectbox(key="pl_meta_0_type").select("procurement_guideline")
    at.text_input(key="pl_meta_0_version_label").input("3.0")
    at.text_input(key="pl_meta_0_effective_from").input("2026-01-01")
    at.selectbox(key="pl_meta_1_type").select("vendor_contract")
    at.text_input(key="pl_meta_1_vendor_key").input("evt-ven-001")
    run(at)
    at.button(key="pl_upload_btn").click()
    run(at)
    sent = backend.upload_bodies[-1]
    assert [f[1][0] for f in sent["files"]] == ["procurement.pdf", "broken.pdf"]
    metas = json.loads(sent["data"]["metadata"])
    assert metas[0] == {"document_type": "procurement_guideline", "version_label": "3.0",
                        "effective_from": "2026-01-01"}
    assert metas[1] == {"document_type": "vendor_contract", "vendor_key": "evt-ven-001"}
    body = all_text(at)
    assert "1 accepted, 1 failed" in body
    assert "broken.pdf** failed: not_pdf" in body
    assert "✅ accepted" in body and "❌ failed" in body
    assert not at.main.success  # partial failure is never shown as plain success


def test_client_metadata_errors_per_file_send_nothing(backend):
    at = library(backend, "admin")
    _upload(at, [("a.pdf", b"%PDF-1.4", "application/pdf"), ("b.pdf", b"%PDF-1.4", "application/pdf")])
    at.text_input(key="pl_meta_1_effective_from").input("2026-05-01")
    at.text_input(key="pl_meta_1_effective_to").input("2026-01-01")
    at.text_input(key="pl_meta_0_vendor_key").input("Bad Key!")
    run(at)
    at.button(key="pl_upload_btn").click()
    run(at)
    errors = " ".join(str(e.value) for e in at.error)
    assert "**a.pdf** — metadata error" in errors and "vendor key" in errors
    assert "**b.pdf** — metadata error" in errors and "effective to must not be before" in errors
    assert not backend.upload_bodies


@pytest.mark.parametrize("status,expected", [
    (503, "Database unavailable"), (413, "File too large"), (422, "effective_to must not be before")])
def test_upload_errors_never_show_success(backend, status, expected):
    backend.upload_status = status
    at = library(backend, "admin")
    _upload(at, [("a.pdf", b"%PDF-1.4", "application/pdf"), ("b.pdf", b"%PDF-1.4", "application/pdf")])
    at.button(key="pl_upload_btn").click()
    run(at)
    errors = " ".join(str(e.value) for e in at.error)
    assert expected in errors
    assert not at.main.success and "accepted" not in all_text(at)
    if status == 422:
        assert "**b.pdf** — metadata error" in errors


# --------------------------------------------------------------------------- inventory
def test_inventory_renders_statuses_and_markers(backend):
    at = library(backend, "admin")
    df = next(d.value for d in at.dataframe if "Document" in d.value.columns and "Chunks" in d.value.columns)
    by_id = {r["Document"]: r for r in df.to_dict("records")}
    assert by_id[DOC_IDS[0]]["Status"] == "✅ ready" and by_id[DOC_IDS[0]]["Pages"] == 16
    assert by_id[DOC_IDS[1]]["Status"] == "↪️ superseded" and "superseded by doc_0000000d" in by_id[DOC_IDS[1]][
        "Versions"]
    assert by_id[DOC_IDS[2]]["Status"] == "🟠 needs review" and by_id[DOC_IDS[2]]["Rate entries"] == 12
    assert by_id["doc_0000000e"]["Status"] == "❌ failed" and by_id["doc_0000000e"]["Error"] == "corrupt"


def test_admin_archive_requires_confirmation(backend):
    at = library(backend, "admin")
    at.selectbox(key="pl_doc_pick").select(DOC_IDS[0])
    run(at)
    at.button(key="pl_archive_btn").click()
    run(at)
    assert not backend.find("POST", f"/api/policy-documents/{DOC_IDS[0]}/archive")
    assert at.button(key="pl_doc_confirm_btn").disabled
    at.checkbox(key="pl_doc_confirm_check").check()
    run(at)
    at.button(key="pl_doc_confirm_btn").click()
    run(at)
    assert backend.find("POST", f"/api/policy-documents/{DOC_IDS[0]}/archive")
    assert backend.documents[DOC_IDS[0]]["status"] == "archived"
    assert "Server confirmed archive" in " ".join(str(s.value) for s in at.main.success)


def test_approver_read_only_and_verifies_rate_row(backend):
    at = library(backend, "carol")
    k = keys(at)
    assert not at.file_uploader and "pl_upload_btn" not in k
    assert "pl_archive_btn" not in k and "pl_reprocess_btn" not in k
    assert "Read-only: only admins" in texts(at)
    assert "pl_rate_verify_rate_0001" in k
    assert "page 2" in texts(at)
    at.button(key="pl_rate_verify_rate_0001").click()
    run(at)
    posts = backend.find("POST", "/api/rate-cards/rate_0001/verify")
    assert posts and posts[-1][2] == {"decision": "verify"}
    assert backend.rates["rate_0001"]["verification_status"] == "verified"
    assert "verified by carol" in " ".join(str(s.value) for s in at.main.success)
    assert "pl_rate_verify_rate_0001" not in keys(at)
    assert not backend.upload_bodies


# --------------------------------------------------------------------------- search
def _search(at: AppTest, query: str) -> AppTest:
    at.text_input(key="pl_search_query").input(query)
    btn = next(b for b in at.get("form_submit_button") if b.form_id == "pl_search_form")
    btn.click()
    return run(at)


def test_search_shows_citations_with_pages_and_degraded_warning(backend):
    at = library(backend, "carol")
    _search(at, "insurance")
    body = texts(at)
    assert backend.find("POST", "/api/policy/search")[-1][2]["query"] == "insurance"
    assert "Vander Policy.pdf · v1 (1.0) · p. 4 · § 4.2 Insurance" in body
    assert EXCERPT_SECRET in body and "score 0.820" in body
    assert "keyword-only degraded" not in body

    backend.search_result = dict(backend.search_result, retrieval_mode="keyword_only_degraded",
                                 warnings=["Embedding model unavailable"])
    _search(at, "insurance")
    warnings = " ".join(str(w.value) for w in at.warning)
    assert "keyword-only degraded mode" in warnings and "Embedding model unavailable" in warnings


def test_search_insufficient_evidence_message(backend):
    backend.search_result = {"query_id": "q_2", "evidence": [], "retrieval_mode": "hybrid",
                             "insufficient_evidence": True, "warnings": []}
    at = library(backend, "admin")
    _search(at, "parking reimbursement")
    assert "Insufficient evidence" in " ".join(str(i.value) for i in at.info)


# --------------------------------------------------------------------------- evidence panels
def test_admin_ticket_detail_shows_evidence_panel_badges(backend):
    tid = backend.add_ticket(status="reviewed", run_count=1)
    backend.evidence[tid] = evaluation(tid)
    at = logged_in(backend, "admin")
    at.selectbox(key="admin_pick").select(tid)
    run(at)
    assert backend.find("GET", f"/api/requests/{tid}/evidence")
    body = texts(at)
    assert "📑 Policy evidence" in body
    assert "REVIEW REQUIRED" in " ".join(str(w.value) for w in at.warning)
    assert "❔ unknown · 🟠 review required" in body
    assert "➖ not applicable · ℹ️ info" in body
    assert "⚔️ conflict · ⚠️ warning" in body
    assert "Vander Policy.pdf · v1 (1.0) · p. 4 · § 4.2 Insurance" in body and EXCERPT_SECRET in body
    assert "No evidence found for dietary requirements policy" in body
    assert "Headcount threshold differs between documents" in body
    assert "Evaluation `abcdef012345…`" in body and "policy-rules-v1" in body
    rate = next(d.value for d in at.dataframe if "Field" in d.value.columns
                and "Validation status" in list(d.value["Field"]))
    rows = {r["Field"]: r["Value"] for r in rate.to_dict("records")}
    assert rows["Validation status"] == "❌ fail" and rows["Expected total"] == "INR 45000.00"
    assert "p. 3" in rows["Source"]


@pytest.mark.parametrize("outcome,where,marker", [
    ("pass", "success", "✅ PASS"), ("hard_block", "error", "⛔ HARD BLOCK"), ("unknown", "info", "❔ UNKNOWN"),
    ("review_required", "warning", "🟠 REVIEW REQUIRED")])
def test_outcome_badges_are_distinct(backend, outcome, where, marker):
    tid = backend.add_ticket(status="reviewed", run_count=1, embed_eval=True)
    backend.evidence[tid] = evaluation(tid, outcome=outcome, mode="keyword_only_degraded")
    at = logged_in(backend, "admin")
    at.selectbox(key="admin_pick").select(tid)
    run(at)
    assert not backend.find("GET", f"/api/requests/{tid}/evidence")  # embedded policy_evaluation used
    assert marker in " ".join(str(e.value) for e in getattr(at, where))
    others = [m for m in ("✅ PASS", "⛔ HARD BLOCK", "❔ UNKNOWN", "🟠 REVIEW REQUIRED") if m != marker]
    assert not any(o in texts(at) for o in others)
    assert "keyword-only degraded mode" in " ".join(str(w.value) for w in at.warning)


def test_approvals_detail_shows_evidence_and_confirmation_text(backend):
    tid = backend.add_ticket(status="reviewed", run_count=1)
    aid = backend.add_approval(tid)
    backend.evidence[tid] = evaluation(tid)
    backend.approvals[aid]["review_snapshot"]["policy_evaluation"] = {
        "evaluation_id": "eval_0001", "evaluation_hash": backend.evidence[tid]["evaluation_hash"],
        "rule_set_version": "policy-rules-v1", "documents_used": []}
    at = logged_in(backend, "carol")
    at.selectbox(key="apr_pick").select(aid)
    run(at)
    body = texts(at)
    assert "📑 Policy evidence" in body and "Vendor insurance must be verified" in body
    assert "p. 4" in body and "bound to policy evaluation `abcdef012345…`" in body
    assert "differs from the evidence" not in body
    at.radio(key="apr_action").set_value("approve")
    at.button(key="apr_prepare_btn").click()
    run(at)
    assert APPROVED_TEXT in texts(at)
    at.checkbox(key="apr_confirm_check").check()
    run(at)
    at.button(key="apr_confirm_btn").click()
    run(at)
    success = " ".join(str(s.value) for s in at.main.success)
    assert "Server recorded" in success and APPROVED_TEXT in success


def test_approval_503_shows_no_approved_text(backend):
    tid = backend.add_ticket(status="reviewed", run_count=1)
    aid = backend.add_approval(tid)
    backend.decision_faults = [503]
    at = logged_in(backend, "carol")
    at.selectbox(key="apr_pick").select(aid)
    run(at)
    at.radio(key="apr_action").set_value("approve")
    at.button(key="apr_prepare_btn").click()
    run(at)
    at.checkbox(key="apr_confirm_check").check()
    run(at)
    at.button(key="apr_confirm_btn").click()
    run(at)
    assert not at.main.success
    assert APPROVED_TEXT not in " ".join(str(s.value) for s in at.success)


def test_user_portal_shows_redacted_evidence(backend):
    tid = backend.add_ticket(status="reviewed", run_count=1)
    backend.evidence[tid] = evaluation(tid)
    at = logged_in(backend, "alice")
    body = all_text(at)
    assert "📑 Policy check" in body
    assert "Vendor insurance must be verified" in body
    assert "Vander Policy.pdf · version 1.0 · p. 4 · § 4.2 Insurance" in body
    assert EXCERPT_SECRET not in body and "Risk assessment needed" not in body
    assert "abcdef0123" not in body and "rate_0001" not in body
    assert not any(c[1].startswith("/api/policy") or c[1].startswith("/api/rate-cards") for c in backend.calls)
