"""Phase 2 Policy RAG: spec section 18.6 end-to-end acceptance against a real uvicorn + real MongoDB.

Generates synthetic policy PDFs (company procurement policy, Vendor A contract, Vendor A rate card, privacy /
security policy), uploads them in ONE batch together with a corrupt file, verifies rate rows as an approver,
searches with citations, validates quotes with Decimal arithmetic, runs a portal ticket through the
workflow + policy evaluation + approval, and proves everything survives an API restart. A second scenario
ingests the three bundled real PDFs from data/policies. Each scenario uses its own uniquely named,
disposable database (dropped in teardown) and a temporary PDF store. Skipped when MongoDB is unreachable.
Override the server with WORKPLACEOPS_TEST_MONGODB_URI (never commit credentials).
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.real_mongo

pymongo = pytest.importorskip("pymongo")
httpx = pytest.importorskip("httpx")

from tests.pdf_factory import make_corrupt_pdf, make_pdf, text_page  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICY_DIR = REPO_ROOT / "data" / "policies"
MONGO_URI = os.getenv("WORKPLACEOPS_TEST_MONGODB_URI") or "mongodb://localhost:27017/"

# test-only credentials for the disposable database
ADMIN_USERNAME = "rag-admin"
ADMIN_PASSWORD = "rag-admin-pass-123"
APPROVER_USERNAME = "rag-approver"
APPROVER_PASSWORD = "rag-approver-pass-1"
APPROVER_ACCOUNTS = f"{APPROVER_USERNAME}:{APPROVER_PASSWORD}"

RUN_BODY = {"invoice_id": "INV-5001", "today": "2026-10-17"}
HAPPY = {
    "title": "Quarterly team lunch",
    "event_category": "team_lunch",
    "service": "lunch",
    "event_date": "2026-10-23",
    "attendees": 100,
    "budget_inr": 100000,
    "location": "Bengaluru",
    "approved_vendor_only": True,
}
POLICY_REASON_CODES = {"POLICY_REVIEW_REQUIRED", "RATE_CARD_MISMATCH", "RATE_CARD_UNVERIFIED", "POLICY_CONFLICT",
                       "INSUFFICIENT_POLICY_EVIDENCE", "POLICY_RETRIEVAL_DEGRADED"}
SAFE_ERROR_CODES = {"not_pdf", "encrypted", "corrupt", "too_large", "too_many_pages", "empty_text",
                    "processing_error"}
FORBIDDEN_CLAIMS = re.compile(r"\b(ordered|paid|purchased)\b", re.IGNORECASE)
LEAK_MARKERS = re.compile(r"Traceback|File \"|policy_store|[A-Za-z]:\\\\|/tmp/", re.IGNORECASE)

VENDOR_A = "evt-ven-001"
VENDOR_B = "evt-ven-002"
EFFECTIVE = {"effective_from": "2026-01-01", "effective_to": "2027-12-31"}


def _mongo_available() -> bool:
    try:
        client = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=2000)
        try:
            client.admin.command("ping")
            return True
        finally:
            client.close()
    except Exception:
        return False


if not _mongo_available():
    pytest.skip(
        "Real MongoDB not reachable (set WORKPLACEOPS_TEST_MONGODB_URI or start a local "
        "server on localhost:27017); skipping policy RAG acceptance test.",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------------------------- server
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, db_name: str, store_dir: Path):
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        env = os.environ.copy()
        env.update({
            "MONGODB_URI": MONGO_URI,
            "MONGODB_DATABASE": db_name,
            "WORKPLACEOPS_PERSISTENCE": "required",
            "WORKPLACEOPS_OFFLINE": "1",
            "POLICY_RAG_ENABLED": "1",
            "POLICY_RAG_SEED": "0",
            "POLICY_STORE_DIR": str(store_dir),
            "ADMIN_USERNAME": ADMIN_USERNAME,
            "ADMIN_PASSWORD": ADMIN_PASSWORD,
            "APPROVER_ACCOUNTS": APPROVER_ACCOUNTS,
        })
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(REPO_ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def wait_ready(self, timeout: float = 45.0) -> dict:
        deadline = time.monotonic() + timeout
        with httpx.Client(base_url=self.base_url, timeout=2) as c:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"uvicorn exited early with code {self.proc.returncode}")
                try:
                    r = c.get("/api/health")
                    if r.status_code == 200:
                        return r.json()
                except httpx.HTTPError:
                    pass
                time.sleep(0.3)
        raise RuntimeError("uvicorn did not become ready within timeout")

    def client(self, token: str | None = None) -> "httpx.Client":
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return httpx.Client(base_url=self.base_url, timeout=120, headers=headers)

    def token(self, username: str, password: str) -> str:
        with self.client() as c:
            r = c.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return r.json()["token"]

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


def _start(db_name: str, store_dir: Path) -> _Server:
    server = _Server(db_name, store_dir)
    try:
        health = server.wait_ready()
        assert health.get("database") == "connected", health
    except BaseException:
        server.stop()
        raise
    return server


@pytest.fixture()
def test_db():
    db_name = f"workplaceops_rag_acc_{uuid4().hex[:8]}"
    assert db_name != "workplaceops"
    mongo = pymongo.MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        yield db_name, mongo[db_name]
    finally:
        try:
            mongo.drop_database(db_name)
        finally:
            mongo.close()


# ------------------------------------------------------------------------------------ synthetic fixtures
def _synthetic_pdfs() -> list[tuple[str, bytes, dict]]:
    procurement = make_pdf([
        text_page(
            "1. Purpose and scope",
            "This synthetic Company Procurement Policy governs every purchase made for workplace events, "
            "team lunches, catering and offsites. It applies to all departments and all approved vendors. "
            "Budget owners plan event spending in advance and record the business purpose of each request.",
        ),
        text_page(
            "2. Approval threshold and quotation requirement",
            "Any purchase with a total value above INR 50,000 requires approval by an authorised approver "
            "before the purchase order is used. For purchases above the approval threshold the requester "
            "must obtain at least three written quotations from approved vendors and attach the quotation "
            "comparison to the request. A purchase order remains a draft until approval is recorded.",
        ),
        text_page(
            "3. Vendor verification",
            "Before a vendor is used for a workplace event the approver must verify the vendor licenses, "
            "insurance certificates, food safety registration and facilities. Vendor verification must be "
            "confirmed for every selected vendor, including catering vendors for team lunch events.",
        ),
    ], title="Synthetic Company Procurement Policy")
    contract = make_pdf([
        text_page(
            "1. Parties and services",
            "This synthetic master services contract is between the company and Acme Synthetic Events "
            "(Vendor A, record VEN-001). Acme Synthetic Events provides event planning coordination "
            "services for corporate gatherings under the rates of the attached rate card.",
        ),
        text_page(
            "2. Payment terms and liability",
            "Acme Synthetic Events invoices after the event. Payment terms are net 30 days from a valid "
            "invoice. Acme Synthetic Events maintains public liability insurance for every engagement and "
            "cancellation within seven days of the event incurs a fee of twenty percent.",
        ),
    ], title="Synthetic Vendor A Contract")
    rate_card = make_pdf([[
        "VENDOR A EVENT SERVICES",
        "Acme Synthetic Events",
        "Illustrative synthetic rate card for acceptance tests",
        "Record: VEN-001   Currency: INR   Price basis: fixed planning rate, before applicable taxes",
        "Package Scope Sample price",
        "Team Gathering ₹42,000 / event",
        "Annual Celebration ₹1,45,000 / event",
        "Sample terms: rates are synthetic test data valid for the stated effective period only.",
    ]], title="Synthetic Vendor A Rate Card")
    privacy = make_pdf([
        text_page(
            "1. Sensitive data handling",
            "This synthetic Privacy and Security Policy requires a privacy review whenever an event "
            "request involves personal data, attendee lists, photographs or video recordings of employees. "
            "Sensitive data may only be shared with a vendor after the privacy owner approves the transfer.",
        ),
        text_page(
            "2. Security review requirement",
            "Vendors that receive personal data must complete a security review covering storage, access "
            "control and deletion of the data after the event. Consent must be recorded for every photograph.",
        ),
    ], title="Synthetic Privacy and Security Policy")
    return [
        ("Synthetic Procurement Policy.pdf", procurement,
         {"document_type": "company_policy", "logical_key": "synthetic-procurement-policy",
          "version_label": "P-1.0", **EFFECTIVE}),
        ("Vendor A Contract.pdf", contract,
         {"document_type": "vendor_contract", "vendor_key": VENDOR_A, "logical_key": "vendor-a-contract",
          "version_label": "C-2026", **EFFECTIVE}),
        ("Vendor A Rate Card.pdf", rate_card,
         {"document_type": "rate_card", "vendor_key": VENDOR_A, "logical_key": "vendor-a-rate-card",
          "version_label": "RC-2026", "effective_from": "2026-01-01", "effective_to": "2026-12-31"}),
        ("Synthetic Privacy Security Policy.pdf", privacy,
         {"document_type": "privacy_policy", "logical_key": "synthetic-privacy-security-policy",
          "version_label": "PS-1.0", **EFFECTIVE}),
    ]


def _upload(client, items: list[tuple[str, bytes, dict]]):
    files = [("files", (name, data, "application/pdf")) for name, data, _ in items]
    return client.post("/api/policy-documents/upload", files=files,
                       data={"metadata": json.dumps([meta for _, _, meta in items])})


def _search(client, query: str, **extra):
    r = client.post("/api/policy/search", json={"query": query, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _pending_view(approver, ticket_id: str) -> dict:
    r = approver.get(f"/api/tickets/{ticket_id}/approval")
    assert r.status_code == 200, r.text
    current = r.json()["current"]
    assert current is not None and current["status"] == "pending", current
    r = approver.get(f"/api/approvals/{current['approval_id']}")
    assert r.status_code == 200, r.text
    return r.json()


def _decision(view: dict, action: str = "approve") -> dict:
    return {"action": action, "idempotency_key": f"key-{uuid4().hex}", "request_version": view["request_version"],
            "workflow_snapshot_hash": view["workflow_snapshot_hash"]}


# ---------------------------------------------------------------------------------- spec 18.6 scenario
def test_synthetic_policy_rag_end_to_end(test_db, tmp_path):
    db_name, db = test_db
    store = tmp_path / "policy_store"
    store.mkdir()
    synthetic = _synthetic_pdfs()

    server = _start(db_name, store)
    try:
        admin = server.client(server.token(ADMIN_USERNAME, ADMIN_PASSWORD))
        approver = server.client(server.token(APPROVER_USERNAME, APPROVER_PASSWORD))
        with server.client() as anon:
            r = anon.post("/api/auth/signup", json={"username": "rag-requester", "name": "Rag Requester",
                                                    "email": "rag-requester@example.com",
                                                    "password": "rag-requester-pass-1"})
        assert r.status_code == 201, r.text
        user = server.client(r.json()["token"])

        # ---- 1. one batch: 4 valid PDFs + 1 corrupt → per-file results
        batch = synthetic[:2] + [("Broken Upload.pdf", make_corrupt_pdf(), {"document_type": "other"})] + synthetic[2:]
        r = user.post("/api/policy-documents/upload",
                      files=[("files", ("x.pdf", synthetic[0][1], "application/pdf"))],
                      data={"metadata": json.dumps([synthetic[0][2]])})
        assert r.status_code == 403, r.text
        assert db.policy_documents.count_documents({}) == 0
        r = _upload(admin, batch)
        assert r.status_code == 200, r.text
        assert not LEAK_MARKERS.search(r.text), r.text
        results = r.json()["results"]
        assert len(results) == 5
        assert [res["filename"] for res in results][2] == "Broken Upload.pdf"
        bad = results[2]
        assert bad["ok"] is False
        assert bad["error_code"] in SAFE_ERROR_CODES, bad
        ok = [res for i, res in enumerate(results) if i != 2]
        for res in ok:
            assert res["ok"] is True, res
            assert res["status"] == "ready", res
            assert re.fullmatch(r"doc_[a-f0-9]{8,32}", res["document_id"]), res
            assert res.get("duplicate_of") is None
        doc_ids = {name: res["document_id"] for (name, _, _), res in zip(synthetic, ok)}
        assert len(set(doc_ids.values())) == 4

        r = admin.get("/api/policy-documents")
        assert r.status_code == 200, r.text
        assert not LEAK_MARKERS.search(r.text)
        assert "storage_name" not in r.text
        listed = {d["document_id"]: d for d in r.json()["documents"]}
        for name, did in doc_ids.items():
            d = listed[did]
            assert d["status"] == "ready", d
            assert d["filename"] == name
            assert d["chunk_count"] >= 1
            assert len(d["content_hash"]) == 64
        assert listed[doc_ids["Synthetic Procurement Policy.pdf"]]["page_count"] == 3
        assert listed[doc_ids["Vendor A Rate Card.pdf"]]["rate_entry_count"] == 2
        assert user.get("/api/policy-documents").status_code == 403

        # ---- 2. duplicate re-upload is detected, not re-ingested
        r = _upload(admin, [synthetic[1]])
        assert r.status_code == 200, r.text
        dup = r.json()["results"][0]
        assert dup["ok"] is True
        assert dup["duplicate_of"] == doc_ids["Vendor A Contract.pdf"]
        assert db.policy_documents.count_documents({}) == 4

        # ---- 3. approver verifies the synthetic rate rows (all start needs_review)
        r = approver.get("/api/rate-cards", params={"vendor_key": VENDOR_A})
        assert r.status_code == 200, r.text
        rows = {row["item_or_service_code"]: row for row in r.json()["rate_cards"]}
        assert set(rows) == {"team_gathering", "annual_celebration"}, rows.keys()
        assert rows["team_gathering"]["unit_rate"] == "42000.00"
        assert rows["annual_celebration"]["unit_rate"] == "145000.00"
        for row in rows.values():
            assert row["verification_status"] == "needs_review"
            assert row["document_id"] == doc_ids["Vendor A Rate Card.pdf"]
            assert row["source_page"] == 1
            assert row["unit"] == "event"
            assert row["tax_treatment"] == "excluded"
        assert user.get("/api/rate-cards").status_code == 403
        tg = rows["team_gathering"]["rate_id"]
        assert user.post(f"/api/rate-cards/{tg}/verify", json={"decision": "verify"}).status_code == 403
        r = approver.post(f"/api/rate-cards/{tg}/verify", json={"decision": "verify"})
        assert r.status_code == 200, r.text
        r = approver.post(f"/api/rate-cards/{tg}/verify", json={"decision": "reject"})
        assert r.status_code == 409, r.text
        row = approver.get("/api/rate-cards", params={"vendor_key": VENDOR_A,
                                                      "verification_status": "verified"}).json()["rate_cards"]
        assert [x["rate_id"] for x in row] == [tg]
        assert row[0]["verified_by"] == APPROVER_USERNAME

        # ---- 4. search: threshold/quotation clause cites the right document and page
        assert user.post("/api/policy/search", json={"query": "quotation threshold"}).status_code == 403
        res = _search(approver, "three written quotations approval threshold purchase")
        assert res["insufficient_evidence"] is False
        assert res["retrieval_mode"] in ("hybrid", "keyword_only_degraded")
        top = res["evidence"][0]
        assert top["document_id"] == doc_ids["Synthetic Procurement Policy.pdf"], top
        assert top["filename"] == "Synthetic Procurement Policy.pdf"
        assert top["page_number"] == 2, top
        assert top["version_label"] == "P-1.0"
        assert "quotation" in top["excerpt"].lower()
        assert 0 < len(top["excerpt"]) <= 400
        assert top["relevance_score"] > 0

        # vendor-specific: Vendor A docs appear for Vendor A and never for Vendor B
        vendor_a_docs = {doc_ids["Vendor A Contract.pdf"], doc_ids["Vendor A Rate Card.pdf"]}
        res = _search(approver, "Acme Synthetic Events payment terms liability insurance", vendor_key=VENDOR_A)
        assert {e["document_id"] for e in res["evidence"]} & vendor_a_docs, res
        contract_hit = next(e for e in res["evidence"] if e["document_id"] == doc_ids["Vendor A Contract.pdf"])
        assert contract_hit["page_number"] == 2
        res = _search(approver, "Acme Synthetic Events payment terms liability insurance", vendor_key=VENDOR_B)
        assert not {e["document_id"] for e in res["evidence"]} & vendor_a_docs, res
        for e in res["evidence"]:
            assert e.get("vendor_key") in (None, VENDOR_B), e

        # no-evidence query
        res = _search(approver, "zebra quantum spaceship telescope")
        assert res["insufficient_evidence"] is True

        # ---- 5. deterministic Decimal rate validation
        quote = {"vendor_key": VENDOR_A, "item_or_service_code": "team_gathering", "quantity": 1,
                 "currency": "INR", "unit": "event", "as_of": "2026-10-23"}
        assert user.post("/api/policy/validate-rate", json={**quote, "quoted_unit_rate": "42000"}).status_code == 403
        r = approver.post("/api/policy/validate-rate", json={**quote, "quoted_unit_rate": "42000.00"})
        assert r.status_code == 200, r.text
        v = r.json()
        assert v["validation_status"] == "pass", v
        assert v["expected_unit_rate"] == "42000.00"
        assert Decimal(v["difference_amount"]) == Decimal("0")
        assert v["rate_id"] == tg
        assert v["source_document_id"] == doc_ids["Vendor A Rate Card.pdf"] and v["source_page"] == 1

        r = approver.post("/api/policy/validate-rate", json={**quote, "quoted_unit_rate": "45500.50", "quantity": 2})
        assert r.status_code == 200, r.text
        v = r.json()
        assert v["validation_status"] == "fail", v
        assert v["quoted_total"] == "91001.00"
        assert v["expected_total"] == "84000.00"
        assert v["difference_amount"] == "7001.00"
        assert Decimal(v["difference_amount"]) == Decimal("45500.50") * 2 - Decimal("42000.00") * 2

        r = admin.post("/api/policy/validate-rate",
                       json={**quote, "item_or_service_code": "annual_celebration", "quoted_unit_rate": "145000"})
        assert r.status_code == 200, r.text
        v = r.json()
        assert v["validation_status"] == "needs_review", v
        assert "verif" in v["reason"].lower()

        r = approver.post("/api/policy/validate-rate", json={**quote, "vendor_key": VENDOR_B, "quoted_unit_rate": "1"})
        assert r.status_code == 200, r.text
        assert r.json()["validation_status"] == "unknown"

        # ---- 6. portal ticket run → evidence snapshot + approval bound to evaluation hash
        r = user.post("/api/my/tickets", json=HAPPY)
        assert r.status_code == 201, r.text
        ticket_id = r.json()["ticket_id"]
        r = admin.post(f"/api/admin/tickets/{ticket_id}/run", json=RUN_BODY)
        assert r.status_code == 200, r.text
        run = r.json()
        assert run["status"] == "pending_approval", run.get("status")

        r = admin.get(f"/api/requests/{ticket_id}/evidence")
        assert r.status_code == 200, r.text
        snapshot = r.json()
        evaluation = snapshot["evaluation"]
        assert snapshot["ticket_id"] == ticket_id
        assert evaluation["ticket_id"] == ticket_id
        assert evaluation["run_count"] == snapshot["run_count"] == 1
        assert evaluation["outcome"] in ("review_required", "unknown"), evaluation["outcome"]
        assert evaluation["outcome"] != "pass"
        assert len(evaluation["evaluation_hash"]) == 64
        evidence_ids = {e["evidence_id"] for e in evaluation["evidence"]}
        assert evidence_ids, "expected retrieved evidence for the ticket"
        assert evaluation["findings"]
        for f in evaluation["findings"]:
            assert set(f["evidence_ids"]) <= evidence_ids, f
        cited = [f for f in evaluation["findings"] if f["evidence_ids"]]
        assert cited, "at least one finding must cite evidence"
        # catering vendor ticket: vendor A (evt-ven-001) documents must never be evidence
        for e in evaluation["evidence"]:
            assert e["document_id"] not in vendor_a_docs, e
            assert e["page_number"] >= 1
        vendor_finding = next(f for f in evaluation["findings"] if f.get("rule_id") == "vendor_verification")
        assert vendor_finding["severity"] == "review_required"
        assert vendor_finding["evidence_ids"]
        assert evaluation["rate_validation"] is None
        assert approver.get(f"/api/requests/{ticket_id}/evidence").status_code == 200

        # requester: redacted view (summaries + citation labels, no excerpts / hashes / scores)
        r = user.get(f"/api/requests/{ticket_id}/evidence")
        assert r.status_code == 200, r.text
        red = r.json()
        assert red.get("redacted") is True
        assert "excerpt" not in r.text and "content_hash" not in r.text and "evaluation_hash" not in r.text
        assert "relevance_score" not in r.text
        assert red["findings"] and all("summary" in f for f in red["findings"])
        assert red["citations"]
        for c in red["citations"]:
            assert set(c) <= {"filename", "version_label", "page_number", "section"}
            assert c["filename"] and c["page_number"] >= 1
        for e in evaluation["evidence"]:
            assert e["excerpt"][:60] not in r.text
        with server.client() as anon:
            assert anon.get(f"/api/requests/{ticket_id}/evidence").status_code == 401

        view = _pending_view(approver, ticket_id)
        approval_id = view["approval_id"]
        codes = set(view["reason_codes"])
        assert "PO_REQUIRES_HUMAN_APPROVAL" in codes, codes
        assert codes & POLICY_REASON_CODES, codes
        assert "POLICY_REVIEW_REQUIRED" in codes, codes
        assert view["risk_level"] in ("medium", "high")
        bound = view["review_snapshot"]["policy_evaluation"]
        assert bound["evaluation_hash"] == evaluation["evaluation_hash"]
        assert bound["evaluation_id"] == evaluation["evaluation_id"]
        assert bound["rule_set_version"] == evaluation["rule_set_version"]
        assert {d["document_id"] for d in bound["documents_used"]} == {d["document_id"] for d in
                                                                       evaluation["documents_used"]}

        # ---- 7. requester and admin cannot decide; approver can
        r = user.post(f"/api/approvals/{approval_id}/decision", json=_decision(view))
        assert r.status_code == 403, r.text
        r = admin.post(f"/api/approvals/{approval_id}/decision", json=_decision(view))
        assert r.status_code == 403, r.text
        assert db.approval_requests.find_one({"approval_id": approval_id})["status"] == "pending"
        r = approver.post(f"/api/approvals/{approval_id}/decision", json=_decision(view))
        assert r.status_code == 200, r.text
        assert r.json()["approval"]["status"] == "approved"
        assert "draft" in r.json()["message"].lower()
        assert not FORBIDDEN_CLAIMS.search(r.text)

        r = admin.get(f"/api/admin/tickets/{ticket_id}/audit")
        assert r.status_code == 200, r.text
        events1 = r.json()["events"]
        r = approver.get(f"/api/approvals/{approval_id}")
        approval1 = r.json()
        before = {
            "documents": {d["document_id"]: (d["status"], d["chunk_count"], d["content_hash"], d["version"])
                          for d in admin.get("/api/policy-documents").json()["documents"]},
            "chunks": db.policy_chunks.count_documents({}),
            "rates": {x["rate_id"]: (x["verification_status"], x["unit_rate"])
                      for x in approver.get("/api/rate-cards").json()["rate_cards"]},
            "policy_events": db.policy_events.count_documents({}),
        }
        for c in (admin, approver, user):
            c.close()
    finally:
        server.stop()
    assert server.proc.poll() is not None

    # ---- 8. restart: everything is durable
    server = _start(db_name, store)
    try:
        admin = server.client(server.token(ADMIN_USERNAME, ADMIN_PASSWORD))
        approver = server.client(server.token(APPROVER_USERNAME, APPROVER_PASSWORD))
        user = server.client(server.token("rag-requester", "rag-requester-pass-1"))

        docs = {d["document_id"]: (d["status"], d["chunk_count"], d["content_hash"], d["version"])
                for d in admin.get("/api/policy-documents").json()["documents"]}
        assert docs == before["documents"]
        assert db.policy_chunks.count_documents({}) == before["chunks"]
        rates = {x["rate_id"]: (x["verification_status"], x["unit_rate"])
                 for x in approver.get("/api/rate-cards").json()["rate_cards"]}
        assert rates == before["rates"]
        assert rates[tg] == ("verified", "42000.00")
        assert db.policy_events.count_documents({}) >= before["policy_events"]

        r = approver.post("/api/policy/validate-rate", json={**quote, "quoted_unit_rate": "42000.00"})
        assert r.status_code == 200 and r.json()["validation_status"] == "pass", r.text
        res = _search(approver, "three written quotations approval threshold purchase")
        assert res["evidence"][0]["document_id"] == doc_ids["Synthetic Procurement Policy.pdf"]
        assert res["evidence"][0]["page_number"] == 2

        r = admin.get(f"/api/requests/{ticket_id}/evidence")
        assert r.status_code == 200, r.text
        assert r.json() == snapshot
        assert db.policy_evidence_snapshots.count_documents({"ticket_id": ticket_id}) == 1

        r = approver.get(f"/api/approvals/{approval_id}")
        assert r.status_code == 200, r.text
        approval2 = r.json()
        assert approval2["status"] == "approved"
        assert approval2["decisions"] == approval1["decisions"]
        assert approval2["review_snapshot"] == approval1["review_snapshot"]
        assert approval2["review_snapshot"]["policy_evaluation"]["evaluation_hash"] == evaluation["evaluation_hash"]

        r = admin.get(f"/api/admin/tickets/{ticket_id}/audit")
        assert r.status_code == 200, r.text
        events2 = r.json()["events"]
        assert events2[:len(events1)] == events1
        actions = [e["action"] for e in events2]
        assert actions.count("approval_requested") == 1
        assert actions.count("approval_approved") == 1

        r = user.get(f"/api/my/tickets/{ticket_id}")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"
        assert r.json()["po_summary"]["status"] == "draft"
        for c in (admin, approver, user):
            c.close()
    finally:
        server.stop()

    pos = list(db.purchase_orders.find({"ticket_id": ticket_id}))
    assert len(pos) == 1
    assert pos[0]["status"] == "draft"
    assert pos[0]["approval_status"] == "approved"
    assert db.tickets.find_one({"ticket_id": ticket_id})["status"] == "approved"
    stored = list(store.iterdir())
    assert len(stored) == 4  # one stored PDF per ingested document; the corrupt/duplicate files were not kept


# ------------------------------------------------------------------------------------- real bundled PDFs
def test_real_policy_pdfs_ingest_and_cite(test_db, tmp_path):
    db_name, db = test_db
    store = tmp_path / "policy_store"
    store.mkdir()
    manifest = json.loads((POLICY_DIR / "manifest.json").read_text(encoding="utf-8"))["documents"]
    items = []
    for entry in manifest:
        meta = {k: v for k, v in entry.items() if k != "filename"}
        items.append((entry["filename"], (POLICY_DIR / entry["filename"]).read_bytes(), meta))
    expected_pages = {"Company Policy.pdf": 16, "Vander Policy.pdf": 10, "Vander Manu.pdf": 6}
    assert {name for name, _, _ in items} == set(expected_pages)

    server = _start(db_name, store)
    try:
        admin = server.client(server.token(ADMIN_USERNAME, ADMIN_PASSWORD))
        approver = server.client(server.token(APPROVER_USERNAME, APPROVER_PASSWORD))
        r = _upload(admin, items)
        assert r.status_code == 200, r.text
        results = r.json()["results"]
        assert [res["filename"] for res in results] == [name for name, _, _ in items]
        for res in results:
            assert res["ok"] is True and res["status"] == "ready", res
        ids = {res["filename"]: res["document_id"] for res in results}

        listed = {d["document_id"]: d for d in admin.get("/api/policy-documents").json()["documents"]}
        for name, pages in expected_pages.items():
            d = listed[ids[name]]
            assert d["page_count"] == pages, (name, d["page_count"])
            assert d["status"] == "ready"
            assert d["chunk_count"] >= 1

        r = approver.get("/api/rate-cards", params={"document_id": ids["Vander Manu.pdf"]})
        assert r.status_code == 200, r.text
        rows = r.json()["rate_cards"]
        assert len(rows) >= 10, len(rows)
        for row in rows:
            assert re.fullmatch(r"\d+\.\d{2}", row["unit_rate"]), row
            assert Decimal(row["unit_rate"]) > 0
            assert row["currency"] == "INR"
            assert row["verification_status"] == "needs_review"
            assert 1 <= row["source_page"] <= 6
            assert row["vendor_key"].startswith("evt-ven-")
        amounts = {row["unit_rate"] for row in rows}
        assert {"18000.00", "145000.00"} <= amounts, sorted(amounts)
        ven1 = {row["item_or_service_code"]: row["unit_rate"] for row in rows if row["vendor_key"] == "evt-ven-001"}
        assert ven1.get("meeting_essentials") == "18000.00", ven1
        assert ven1.get("large_gathering_desk") == "145000.00", ven1

        res = _search(approver, "insurance licenses verification")
        assert res["insufficient_evidence"] is False
        vendor_hits = [e for e in res["evidence"] if e["filename"] == "Vander Policy.pdf"]
        assert vendor_hits, [e["filename"] for e in res["evidence"]]
        for e in vendor_hits:
            assert e["document_id"] == ids["Vander Policy.pdf"]
            assert 1 <= e["page_number"] <= 10
            assert e["excerpt"]
        res = _search(approver, "risk assessment")
        assert any(e["filename"] == "Vander Policy.pdf" and 1 <= e["page_number"] <= 10 for e in res["evidence"])
        for c in (admin, approver):
            c.close()
    finally:
        server.stop()
