import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import PolicyLibraryPage from "../PolicyLibraryPage";
import { callsTo as calls, renderWithApp } from "../../test/utils";

const FORBIDDEN = /\b(Paid|Awarded|PO sent)\b/;

const DOC = {
  document_id: "doc_abcdef12", filename: "travel-policy.pdf", document_type: "company_policy", status: "needs_review",
  version: 2, version_label: "2.1", uploaded_at: "2026-10-01T10:00:00Z", updated_at: "2026-10-02T10:00:00Z",
  uploaded_by: "admin", page_count: 12, low_text_pages: [3, 7], chunk_count: 40, rate_entry_count: 0,
  extraction_status: "partial", index_status: "ready", logical_key: "travel-policy", content_hash: "f".repeat(64),
};
const READY = { ...DOC, document_id: "doc_ready0001", filename: "catering.pdf", status: "ready", low_text_pages: [],
  extraction_status: "complete" };
const RATE = { rate_id: "RATE-1", vendor_key: "evt-ven-001", vendor_name: "Tasty Foods", item_or_service_code: "lunch",
  description: "Lunch buffet", unit_rate: "950.00", currency: "INR", unit: "person", document_id: "doc_ready0001",
  source_page: 2, verification_status: "needs_review" };

const LIST = { method: "GET", path: "/api/policy-documents", body: { documents: [DOC, READY], counts: {} } };

describe("PolicyLibraryPage", () => {
  it("lists documents with status, type and updated date", async () => {
    const { container } = renderWithApp(<PolicyLibraryPage />, { role: "admin", routes: [LIST] });
    expect(await screen.findByText("travel-policy.pdf")).toBeInTheDocument();
    const row = screen.getByText("travel-policy.pdf").closest("tr");
    expect(within(row).getByText("Needs review")).toBeInTheDocument();
    expect(within(row).getByText("Company policy")).toBeInTheDocument();
    expect(within(row).getByText("v2 (2.1)")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Upload" })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it("shows empty and error+retry states", async () => {
    let n = 0;
    renderWithApp(<PolicyLibraryPage />, { role: "admin", routes: [{ method: "GET", path: "/api/policy-documents",
      get status() { return n++ === 0 ? 500 : 200; }, body: { documents: [], counts: {} } }] });
    expect(await screen.findByRole("alert")).toHaveTextContent(/Something went wrong/);
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No policy documents")).toBeInTheDocument();
  });

  it("document detail shows review flags; archive goes through a confirm dialog", async () => {
    const { fetchMock } = renderWithApp(<PolicyLibraryPage />, { role: "admin", routes: [
      LIST,
      { method: "GET", path: "/api/policy-documents/doc_abcdef12", body: { ...DOC, events: [{ event_type: "document_uploaded", actor: "admin", created_at: "2026-10-01T10:00:00Z" }] } },
      { method: "POST", path: "/api/policy-documents/doc_abcdef12/archive", body: { document: { ...DOC, status: "archived" } } },
    ] });
    await userEvent.click(await screen.findByText("travel-policy.pdf"));
    expect(await screen.findByText("Review flags")).toBeInTheDocument();
    expect(screen.getByText("Pages with little extracted text: 3, 7.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Extract rules" })).not.toBeInTheDocument(); // not ready
    await userEvent.click(screen.getByRole("button", { name: "Archive" }));
    const dialog = screen.getByRole("dialog");
    expect(calls(fetchMock, /archive$/, "POST")).toHaveLength(0);
    await userEvent.click(within(dialog).getByRole("button", { name: "Archive" }));
    expect(await screen.findByText(/Document archived/)).toBeInTheDocument();
    expect(calls(fetchMock, /archive$/, "POST")).toHaveLength(1);
  });

  it("extract rules on a ready document shows candidates with the unverified warning", async () => {
    renderWithApp(<PolicyLibraryPage />, { role: "admin", routes: [
      LIST,
      { method: "GET", path: "/api/policy-documents/doc_ready0001", body: { ...READY, events: [] } },
      { method: "POST", path: "/api/policy-documents/doc_ready0001/extract-rules", body: { document_id: "doc_ready0001",
        candidates: [{ rule_id: "RULE-CAT-1", rule_version: 1, name: "Catering cap", status: "candidate", condition_text: "po.total > INR 100000" }],
        duplicates: [], warnings: [], extraction_model: "m" } },
    ] });
    await userEvent.click(await screen.findByText("catering.pdf"));
    await userEvent.click(await screen.findByRole("button", { name: "Extract rules" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Extract rules" }));
    expect(await screen.findByText("Catering cap")).toBeInTheDocument();
    expect(screen.getByText(/Unverified — extracted text may be wrong/)).toBeInTheDocument();
  });

  it("upload sends multipart FormData with files and per-file metadata", async () => {
    const { fetchMock } = renderWithApp(<PolicyLibraryPage />, { role: "admin", routes: [
      LIST,
      { method: "POST", path: "/api/policy-documents/upload", body: { results: [{ filename: "new.pdf", ok: true, document_id: "doc_new00001", status: "ready" }] } },
    ] });
    await userEvent.click(await screen.findByRole("tab", { name: "Upload" }));
    expect(screen.getByText(/PDF only/)).toBeInTheDocument();
    const file = new File(["%PDF-1.4 test"], "new.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText("PDF files"), file);
    await userEvent.selectOptions(screen.getByLabelText("Document type"), "rate_card");
    await userEvent.type(screen.getByLabelText("Vendor key"), "evt-ven-001");
    await userEvent.click(screen.getByRole("button", { name: "Upload 1 file" }));
    expect(await screen.findByText("Upload results")).toBeInTheDocument();
    expect(screen.getByText(/Accepted/)).toBeInTheDocument();
    const [call] = calls(fetchMock, "/api/policy-documents/upload", "POST");
    expect(call.init.body).toBeInstanceOf(FormData);
    expect(call.init.body.getAll("files")).toHaveLength(1);
    expect(call.init.body.getAll("files")[0].name).toBe("new.pdf");
    expect(JSON.parse(call.init.body.get("metadata"))).toEqual([{ document_type: "rate_card", vendor_key: "evt-ven-001" }]);
    expect(call.init.headers["Content-Type"]).toBeUndefined();
  });

  it("upload validates metadata inline and keeps values", async () => {
    const { fetchMock } = renderWithApp(<PolicyLibraryPage />, { role: "admin", routes: [LIST] });
    await userEvent.click(await screen.findByRole("tab", { name: "Upload" }));
    await userEvent.upload(screen.getByLabelText("PDF files"), new File(["x"], "a.pdf", { type: "application/pdf" }));
    await userEvent.type(screen.getByLabelText("Vendor key"), "Bad Key");
    await userEvent.click(screen.getByRole("button", { name: "Upload 1 file" }));
    expect(await screen.findByText(/Use lowercase letters/)).toBeInTheDocument();
    expect(screen.getByLabelText("Vendor key")).toHaveValue("Bad Key");
    expect(calls(fetchMock, "/api/policy-documents/upload", "POST")).toHaveLength(0);
  });

  it("approver sees no upload tab and can verify a rate card after confirming", async () => {
    let verified = false;
    const { fetchMock } = renderWithApp(<PolicyLibraryPage />, { role: "approver", routes: [
      LIST,
      { method: "GET", path: "/api/rate-cards", body: () => ({ rate_cards: verified ? [] : [RATE], count: 1 }) },
      { method: "POST", path: "/api/rate-cards/RATE-1/verify", body: () => { verified = true; return { ...RATE, verification_status: "verified", verified_by: "carol" }; } },
    ] });
    await screen.findByText("travel-policy.pdf");
    expect(screen.queryByRole("tab", { name: "Upload" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("tab", { name: "Rate cards" }));
    expect(await screen.findByText("Lunch buffet")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Verify" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Verify" }));
    expect(await screen.findByText("Rate RATE-1 recorded as verified.")).toBeInTheDocument();
    expect(calls(fetchMock, "/api/rate-cards/RATE-1/verify", "POST")[0].json).toEqual({ decision: "verify" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
});
