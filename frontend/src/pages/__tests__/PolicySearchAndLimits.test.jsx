import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import PolicyLibraryPage from "../PolicyLibraryPage";
import { callsTo, renderWithApp } from "../../test/utils";

const FORBIDDEN = /\b(Paid|Awarded|PO sent)\b/;
const LIST = { method: "GET", path: "/api/policy-documents", body: { documents: [], counts: {} } };
const LIMITS = { upload_max_mb: 1, upload_batch_max_mb: 1.5, max_files_per_batch: 2, max_pdf_pages: 300, allowed_content_types: ["application/pdf"] };
const EVIDENCE = {
  evidence_id: "ev_1", chunk_id: "ch_1", document_id: "doc_ready0001", filename: "catering.pdf", content_hash: "f".repeat(64),
  version: 3, version_label: "2026", page_number: 4, section: "Insurance", excerpt: "Venues must hold public liability cover.",
  relevance_score: 0.87654, document_type: "vendor_contract", vendor_key: "evt-ven-001", effective_from: "2026-01-01", effective_to: null,
};
const pdf = (name, bytes) => new File([new Uint8Array(bytes)], name, { type: "application/pdf" });

describe("PolicyLibraryPage — search", () => {
  it("approver searches with filters and sees citations, score and retrieval mode", async () => {
    const { fetchMock, container } = renderWithApp(<PolicyLibraryPage />, { role: "approver", routes: [
      LIST,
      { method: "POST", path: "/api/policy/search", body: { query_id: "q_123", retrieval_mode: "hybrid_local_hash",
        evidence: [EVIDENCE], insufficient_evidence: false, warnings: ["Some documents are still indexing."],
        embedding_provider: "local_hash", retrieval_config_version: "rc-2" } },
    ] });
    await userEvent.click(await screen.findByRole("tab", { name: "Search" }));
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Enter at least 2 characters to search.")).toBeInTheDocument();
    expect(callsTo(fetchMock, "/api/policy/search", "POST")).toHaveLength(0);

    await userEvent.type(screen.getByLabelText("Question or keywords"), "  venue insurance ");
    await userEvent.type(screen.getByLabelText("Vendor key"), "evt-ven-001");
    await userEvent.click(screen.getByLabelText("Vendor contract"));
    await userEvent.clear(screen.getByLabelText("Results"));
    await userEvent.type(screen.getByLabelText("Results"), "5");
    await userEvent.click(screen.getByLabelText(/Include superseded/));
    await userEvent.click(screen.getByRole("button", { name: "Search" }));

    const results = (await screen.findByRole("heading", { name: "Search results" })).closest("section");
    expect(within(results).getByText("catering.pdf · v3 (2026) · p. 4 · § Insurance")).toBeInTheDocument();
    expect(within(results).getByText("Venues must hold public liability cover.")).toBeInTheDocument();
    expect(within(results).getByText(/Relevance 0\.877/)).toBeInTheDocument();
    expect(within(results).getByTestId("retrieval-mode")).toHaveTextContent("Local hashed vectors + keywords (not semantic)");
    expect(within(results).getByText(/this is not semantic search/)).toBeInTheDocument();
    expect(within(results).getByText("Some documents are still indexing.")).toBeInTheDocument();
    expect(within(results).queryByText("Insufficient evidence")).toBeNull();
    expect(callsTo(fetchMock, "/api/policy/search", "POST")[0].json).toEqual({
      query: "venue insurance", top_k: 5, include_historical: true, vendor_key: "evt-ven-001", document_types: ["vendor_contract"],
    });
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it("shows insufficient-evidence and degraded-mode warnings", async () => {
    renderWithApp(<PolicyLibraryPage />, { role: "admin", routes: [
      LIST,
      { method: "POST", path: "/api/policy/search", body: { query_id: "q_2", retrieval_mode: "keyword_only_degraded",
        evidence: [], insufficient_evidence: true, warnings: [], degradation_reason: "embeddings_unavailable" } },
    ] });
    await userEvent.click(await screen.findByRole("tab", { name: "Search" }));
    await userEvent.type(screen.getByLabelText("Question or keywords"), "alcohol");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Insufficient evidence")).toBeInTheDocument();
    expect(screen.getByText(/Do not rely on this search to decide/)).toBeInTheDocument();
    expect(screen.getByTestId("retrieval-mode")).toHaveTextContent("Keyword only (degraded)");
    expect(screen.getByText(/keyword-only degraded mode/)).toBeInTheDocument();
    expect(screen.getByText("Reason: Embeddings unavailable")).toBeInTheDocument();
  });

  it("shows a server error and keeps the query", async () => {
    renderWithApp(<PolicyLibraryPage />, { role: "approver", routes: [
      LIST, { method: "POST", path: "/api/policy/search", status: 503, body: { detail: "Policy index unavailable." } },
    ] });
    await userEvent.click(await screen.findByRole("tab", { name: "Search" }));
    await userEvent.type(screen.getByLabelText("Question or keywords"), "catering");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText("Policy index unavailable.")).toBeInTheDocument();
    expect(screen.getByLabelText("Question or keywords")).toHaveValue("catering");
  });
});

describe("PolicyLibraryPage — upload limits", () => {
  it("shows server limits and validates count, size and batch total before upload", async () => {
    const { fetchMock } = renderWithApp(<PolicyLibraryPage />, { role: "admin", routes: [
      LIST, { method: "GET", path: "/api/policy-documents/limits", body: LIMITS },
    ] });
    await userEvent.click(await screen.findByRole("tab", { name: "Upload" }));
    expect(await screen.findByTestId("upload-limits"))
      .toHaveTextContent("PDF only · up to 2 files per upload · up to 1 MB per file · up to 1.5 MB per upload · up to 300 pages per PDF.");
    const input = screen.getByLabelText("PDF files");
    await userEvent.upload(input, [pdf("a.pdf", 10), pdf("b.pdf", 10), pdf("c.pdf", 10)]);
    expect(screen.getByText("Select at most 2 files.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload 3 files" })).toBeDisabled();
    await userEvent.upload(input, [pdf("big.pdf", 1024 * 1024 + 1)]);
    expect(screen.getByText("big.pdf: larger than 1 MB.")).toBeInTheDocument();
    await userEvent.upload(input, [pdf("x.pdf", 800 * 1024), pdf("y.pdf", 800 * 1024)]);
    expect(screen.getByText(/total more than 1.5 MB/)).toBeInTheDocument();
    expect(callsTo(fetchMock, "/api/policy-documents/upload", "POST")).toHaveLength(0);
  });

  it("rejects non-PDF content types using the server's allowed types", async () => {
    const user = userEvent.setup({ applyAccept: false });
    renderWithApp(<PolicyLibraryPage />, { role: "admin", routes: [LIST, { method: "GET", path: "/api/policy-documents/limits", body: LIMITS }] });
    await user.click(await screen.findByRole("tab", { name: "Upload" }));
    await screen.findByTestId("upload-limits");
    await user.upload(screen.getByLabelText("PDF files"), new File(["hi"], "notes.txt", { type: "text/plain" }));
    expect(screen.getByText("notes.txt: only PDF files are accepted.")).toBeInTheDocument();
  });

  it("falls back to the default copy when the limits endpoint is missing (404)", async () => {
    renderWithApp(<PolicyLibraryPage />, { role: "admin", routes: [
      LIST, { method: "GET", path: "/api/policy-documents/limits", status: 404, body: { detail: "Not Found" } },
    ] });
    await userEvent.click(await screen.findByRole("tab", { name: "Upload" }));
    expect(await screen.findByText(/about 20 MB per file \(the server enforces the/)).toBeInTheDocument();
    expect(screen.queryByTestId("upload-limits")).toBeNull();
  });
});
