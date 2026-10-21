import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import RfqDetailPage from "../RfqDetailPage";
import { callsTo, renderWithApp } from "../../test/utils";
import { FORBIDDEN } from "./r3helpers";

const RID = "RFQ-20261021-AAAA";
const BASE = `/api/vendor-sourcing/rfqs/${RID}`;
const UNTRUSTED = "Unverified — extracted text may be wrong. A reviewer must verify terms before comparison.";

const RFQ = {
  rfq_id: RID, request_id: "REQ-20261020-1", service: "catering", status: "open", accepting_quotes: true,
  quantity: "40", budget_limit: "50000.00", currency: "INR", delivery_date: "2026-11-05", delivery_location: "Bengaluru HQ",
  warranty_months_required: 0, compliance_requirements: ["FSSAI"], candidate_ids: ["VEN-001", "VCAND-A1"],
  response_deadline: "2026-11-04T11:30:00Z", quotes_received: 2, created_by: "admin", created_at: "2026-10-21T08:00:00Z",
};
const Q_STRUCT = {
  quote_id: "QUOTE-S1", rfq_id: RID, vendor_id: "VEN-001", quote_reference: "Q-100", submission_type: "structured",
  status: "pending_verification", revision: 1, quantity: "40", submitted_at: "2026-10-21T09:00:00Z",
  submitted: { currency: "INR", unit_price: "500.00", tax_amount: "1000.00", delivery_fee: "0", other_fees: "0",
    delivery_date: "2026-11-04", warranty_months: 0, warranty_scope: "Food quality",
    compliance: { FSSAI: { status: "declared", evidence_ref: null, expires_on: null } } },
};
const Q_PDF = {
  quote_id: "QUOTE-P1", rfq_id: RID, vendor_id: "VCAND-A1", quote_reference: "Q-PDF", submission_type: "pdf",
  status: "pending_verification", revision: 1, quantity: "40", submitted_at: "2026-10-21T09:30:00Z",
  pdf_filename: "quote.pdf", pdf_page_count: 2, pdf_size: 20480, extracted_text: "Total INR 19,000 <script>x</script>",
  submitted: { pdf_filename: "quote.pdf" },
};
const Q_VERIFIED = {
  ...Q_STRUCT, status: "verified", verified_by: "admin",
  verified: { ...Q_STRUCT.submitted, unit_price: "480.00", compliance: { FSSAI: { status: "verified", evidence_ref: "LIC-9", expires_on: null } } },
  verification: { reviewer: "admin", reviewed_at: "2026-10-21T10:00:00Z", note: "Checked against PDF",
    diff: [{ field: "unit_price", submitted: "500.00", verified: "480.00" }] },
};
const COMPARISON = {
  comparison_id: "RFQCMP-1", rfq_id: RID, currency: "INR", budget_limit: "50000.00", eligible_count: 1,
  recommended_quote_id: "QUOTE-S1", scoring_policy_version: 3, human_decision: "pending", created_by: "admin",
  created_at: "2026-10-21T11:00:00Z",
  quotes: [
    { quote_id: "QUOTE-S1", vendor_id: "VEN-001", status: "verified", eligible: true, eligibility: "eligible", rank: 1,
      unit_price: "480.00", quantity: "40", tax_amount: "1000.00", delivery_fee: "0.00", other_fees: "0.00",
      total_cost: "20200.00", within_budget: true, delivery_date: "2026-11-04", delivery_compliant: true,
      warranty_months: 0, warranty_compliant: true, compliance_gate: { FSSAI: "passed" }, reasons: [],
      score: { total: "0.9123", components: {} } },
    { quote_id: "QUOTE-P1", vendor_id: "VCAND-A1", status: "pending_verification", eligible: false, eligibility: "excluded",
      reasons: ["Quotation terms are not verified; excluded from ranking."], score: null },
  ],
};

function detailRoutes({ quotes = [Q_STRUCT, Q_PDF], rfq = RFQ, comparisons = [], extra = [] } = {}) {
  return [
    ...extra,
    { method: "GET", path: BASE, body: { rfq, quotes, events: [{ event_id: "e1", event_type: "rfq_created", actor: "admin", created_at: "2026-10-21T08:00:00Z" }], pending_events: [],
      notice: "RFQ is an internal sourcing record; no supplier has been contacted." } },
    { method: "GET", path: `${BASE}/comparisons`, body: { rfq_id: RID, items: comparisons } },
  ];
}
const render = (opts) => renderWithApp(<RfqDetailPage />, { role: "admin", route: `/admin/rfqs/${RID}`, path: "/admin/rfqs/:rfqId", ...opts });

describe("RfqDetailPage — summary", () => {
  it("shows summary, status, deadline, quotes with badges and the banner", async () => {
    render({ routes: detailRoutes({ quotes: [Q_STRUCT, Q_VERIFIED && { ...Q_VERIFIED, quote_id: "QUOTE-V1" }, { ...Q_STRUCT, quote_id: "QUOTE-OLD", status: "superseded" }] }) });
    expect(await screen.findByRole("heading", { level: 1, name: `RFQ ${RID}` })).toBeInTheDocument();
    expect(screen.getByText("Decision support only — no supplier is contacted and nothing is awarded.")).toBeInTheDocument();
    expect(screen.getAllByText("Response deadline", { exact: false }).length).toBeGreaterThan(0);
    const table = screen.getByRole("table", { name: "Quotes" });
    expect(within(table).getByText("Unverified")).toBeInTheDocument();
    expect(within(table).getByText("Verified")).toBeInTheDocument();
    expect(within(table).getByText("Superseded")).toBeInTheDocument();
    expect(screen.getByText("Decision support only")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });

  it("shows an error with retry", async () => {
    let n = 0;
    render({ routes: [{ method: "GET", path: BASE, status: 404, body: () => { n += 1; return { detail: "RFQ not found." }; } },
      { method: "GET", path: `${BASE}/comparisons`, body: { items: [] } }] });
    expect(await screen.findByText("RFQ not found.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(n).toBe(2));
  });

  it("closes the RFQ with a required reason", async () => {
    const { fetchMock } = render({ routes: detailRoutes({ extra: [{ method: "POST", path: `${BASE}/close`, body: { ...RFQ, status: "closed" } }] }) });
    await userEvent.click(await screen.findByRole("button", { name: "Close RFQ" }));
    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Close RFQ" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText(/Reason/), "All quotes in");
    await userEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(callsTo(fetchMock, `${BASE}/close`, "POST")[0].json).toEqual({ reason: "All quotes in" });
    expect(await screen.findByText("RFQ closed.")).toBeInTheDocument();
  });

  it("cancel failure keeps the dialog open with the server message", async () => {
    render({ routes: detailRoutes({ extra: [{ method: "POST", path: `${BASE}/cancel`, status: 409, body: { detail: "RFQ is closed; it cannot be cancelled." } }] }) });
    await userEvent.click(await screen.findByRole("button", { name: "Cancel RFQ" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/Reason/), "Event dropped");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel RFQ" }));
    expect(await within(dialog).findByText("RFQ is closed; it cannot be cancelled.")).toBeInTheDocument();
  });
});

describe("RfqDetailPage — quotes", () => {
  it("records a structured quote", async () => {
    const { fetchMock } = render({ routes: detailRoutes({ quotes: [], extra: [{ method: "POST", path: `${BASE}/quotes`, status: 201, body: { ...Q_STRUCT, quote_id: "QUOTE-NEW" } }] }) });
    const form = await screen.findByRole("form", { name: "Structured quote" });
    await userEvent.click(within(form).getByRole("button", { name: "Record quote" }));
    expect(within(form).getByText("Enter the vendor's quote reference.")).toBeInTheDocument();
    await userEvent.type(within(form).getByLabelText(/Quote reference/), "Q-200");
    await userEvent.type(within(form).getByLabelText(/Unit price/), "450.50");
    await userEvent.type(within(form).getByLabelText(/Warranty scope/), "Food quality");
    await userEvent.click(within(form).getByLabelText("Vendor declares FSSAI"));
    await userEvent.click(within(form).getByRole("button", { name: "Record quote" }));
    expect(await screen.findByText(/QUOTE-NEW recorded as unverified/)).toBeInTheDocument();
    expect(callsTo(fetchMock, `${BASE}/quotes`, "POST")[0].json).toMatchObject({
      vendor_id: "VEN-001", quote_reference: "Q-200", currency: "INR", unit_price: "450.50", tax_amount: "0",
      delivery_date: "2026-11-05", warranty_months: 0, warranty_scope: "Food quality", compliance: { FSSAI: { status: "declared" } },
    });
  });

  it("uploads a PDF as raw bytes with application/pdf and query metadata", async () => {
    const { fetchMock } = render({ routes: detailRoutes({ quotes: [], extra: [{ method: "POST", path: `${BASE}/quotes/pdf`, status: 201, body: Q_PDF }] }) });
    await userEvent.click(await screen.findByRole("tab", { name: "PDF upload" }));
    expect(screen.getByText(/PDF only · up to 5 MiB · 1–20 pages/)).toBeInTheDocument();
    const input = screen.getByLabelText(/Quotation PDF/);
    expect(input).toHaveAttribute("accept", "application/pdf");
    const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x31]);
    const file = new File([bytes], "quote.pdf", { type: "application/pdf" });
    await userEvent.upload(input, file);
    await userEvent.selectOptions(screen.getByLabelText(/^Vendor/), "VCAND-A1");
    await userEvent.type(screen.getByLabelText(/Quote reference/), "Q-PDF");
    await userEvent.click(screen.getByRole("button", { name: "Upload PDF quote" }));
    expect(await screen.findByText(/PDF quote QUOTE-P1 stored/)).toBeInTheDocument();
    const [call] = callsTo(fetchMock, `${BASE}/quotes/pdf`, "POST");
    expect(call.init.headers["Content-Type"]).toBe("application/pdf");
    expect(call.url.searchParams.get("vendor_id")).toBe("VCAND-A1");
    expect(call.url.searchParams.get("quote_reference")).toBe("Q-PDF");
    expect(call.url.searchParams.get("filename")).toBe("quote.pdf");
    expect(typeof call.init.body).not.toBe("string");
    expect(call.init.body).not.toBeInstanceOf(FormData);
    expect(new Uint8Array(call.init.body)).toEqual(bytes);
  });

  it("rejects non-PDF and >5 MiB files client side", async () => {
    const { fetchMock } = render({ routes: detailRoutes({ quotes: [] }) });
    await userEvent.click(await screen.findByRole("tab", { name: "PDF upload" }));
    const input = screen.getByLabelText(/Quotation PDF/);
    fireEvent.change(input, { target: { files: [new File(["hello"], "notes.txt", { type: "text/plain" })] } });
    expect(await screen.findByText("Only PDF files are accepted.")).toBeInTheDocument();
    const big = new File(["x"], "big.pdf", { type: "application/pdf" });
    Object.defineProperty(big, "size", { value: 5 * 1024 * 1024 + 1 });
    fireEvent.change(input, { target: { files: [big] } });
    expect(await screen.findByText("The PDF is larger than 5 MiB.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/Quote reference/), "Q-X");
    await userEvent.click(screen.getByRole("button", { name: "Upload PDF quote" }));
    expect(callsTo(fetchMock, `${BASE}/quotes/pdf`, "POST")).toHaveLength(0);
  });

  it("shows extracted PDF text as untrusted and downloads the original with auth", async () => {
    const created = vi.fn(() => "blob:pdf-1");
    globalThis.URL.createObjectURL = created;
    globalThis.URL.revokeObjectURL = vi.fn();
    const { fetchMock } = render({ routes: detailRoutes({ extra: [{ method: "GET", path: `${BASE}/quotes/QUOTE-P1/pdf`,
      raw: "%PDF-1.4", headers: { "content-type": "application/pdf" } }] }) });
    await userEvent.click(await screen.findByText("QUOTE-P1"));
    expect(screen.getByText(UNTRUSTED)).toBeInTheDocument();
    const block = screen.getByLabelText("Extracted PDF text (untrusted)");
    expect(block).toHaveClass("untrusted");
    expect(block).toHaveTextContent("<script>x</script>");
    expect(block.querySelector("script")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Get original PDF" }));
    const link = await screen.findByRole("link", { name: "Download original PDF" });
    expect(link).toHaveAttribute("href", "blob:pdf-1");
    const [call] = callsTo(fetchMock, `${BASE}/quotes/QUOTE-P1/pdf`, "GET");
    expect(call.init.headers.Authorization).toMatch(/^Bearer /);
  });

  it("verifies a quote with prefilled values and per-requirement compliance, then shows the diff", async () => {
    let verified = false;
    const { fetchMock } = render({ routes: [
      { method: "POST", path: `${BASE}/quotes/QUOTE-S1/verify`, body: () => { verified = true; return Q_VERIFIED; } },
      { method: "GET", path: BASE, body: () => ({ rfq: RFQ, quotes: [verified ? Q_VERIFIED : Q_STRUCT], events: [], pending_events: [] }) },
      { method: "GET", path: `${BASE}/comparisons`, body: { items: [] } },
    ] });
    await userEvent.click(await screen.findByText("QUOTE-S1"));
    const form = screen.getByRole("form", { name: "Verify quote terms" });
    expect(within(form).getByLabelText(/Unit price/)).toHaveValue("500.00");
    expect(within(form).getByLabelText("FSSAI status")).toHaveValue("declared");
    await userEvent.clear(within(form).getByLabelText(/Unit price/));
    await userEvent.type(within(form).getByLabelText(/Unit price/), "480.00");
    await userEvent.selectOptions(within(form).getByLabelText("FSSAI status"), "verified");
    await userEvent.type(within(form).getByLabelText(/Reviewer note/), "Checked against PDF");
    await userEvent.click(within(form).getByRole("button", { name: "Verify terms" }));
    expect(within(form).getByText("Verified compliance needs an evidence reference.")).toBeInTheDocument();
    await userEvent.type(within(form).getByLabelText("FSSAI evidence reference"), "LIC-9");
    await userEvent.click(within(form).getByLabelText(/Warranty scope accepted/));
    await userEvent.click(within(form).getByRole("button", { name: "Verify terms" }));
    const diff = await screen.findByRole("table", { name: "Submitted vs verified changes" });
    const row = within(diff).getByText("Unit price").closest("tr");
    expect(row).toHaveTextContent("500.00");
    expect(row).toHaveTextContent("480.00");
    expect(await screen.findByText(/Quote QUOTE-S1 verified by a reviewer/)).toBeInTheDocument();
    expect(callsTo(fetchMock, `${BASE}/quotes/QUOTE-S1/verify`, "POST")[0].json).toMatchObject({
      unit_price: "480.00", currency: "INR", warranty_scope_accepted: true, reviewer_note: "Checked against PDF",
      compliance: { FSSAI: { status: "verified", evidence_ref: "LIC-9" } },
    });
  });

  it("revises a quote with a required reason", async () => {
    const { fetchMock } = render({ routes: detailRoutes({ extra: [{ method: "POST", path: `${BASE}/quotes/QUOTE-S1/revise`, status: 201,
      body: { ...Q_STRUCT, quote_id: "QUOTE-S2", revision: 2, revision_of: "QUOTE-S1" } }] }) });
    await userEvent.click(await screen.findByText("QUOTE-S1"));
    await userEvent.click(screen.getByRole("button", { name: "Revise quote" }));
    const form = screen.getByRole("form", { name: "Revise quote" });
    expect(within(form).getByLabelText(/Quote reference/)).toHaveValue("Q-100");
    await userEvent.click(within(form).getByRole("button", { name: "Record revision" }));
    expect(within(form).getByText(/reason of at least 5/)).toBeInTheDocument();
    await userEvent.type(within(form).getByLabelText(/Revision reason/), "Vendor lowered price");
    await userEvent.click(within(form).getByRole("button", { name: "Record revision" }));
    expect(await screen.findByText(/Revision QUOTE-S2 recorded/)).toBeInTheDocument();
    const body = callsTo(fetchMock, `${BASE}/quotes/QUOTE-S1/revise`, "POST")[0].json;
    expect(body.revision_reason).toBe("Vendor lowered price");
    expect(body.vendor_id).toBeUndefined();
  });
});

describe("RfqDetailPage — comparison", () => {
  it("runs a comparison and renders server totals, gate, eligibility, rank and score", async () => {
    let ran = false;
    render({ routes: [
      { method: "POST", path: `${BASE}/compare`, body: () => { ran = true; return COMPARISON; } },
      { method: "GET", path: BASE, body: { rfq: RFQ, quotes: [Q_VERIFIED, Q_PDF], events: [], pending_events: [] } },
      { method: "GET", path: `${BASE}/comparisons`, body: () => ({ items: ran ? [COMPARISON] : [] }) },
    ] });
    expect(await screen.findByText("No comparison yet")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Run comparison" }));
    const table = await screen.findByRole("table", { name: "Quote comparison" });
    expect(within(table).getByText("₹20,200.00")).toBeInTheDocument();
    expect(within(table).getByText(/Tax ₹1,000.00/)).toBeInTheDocument();
    expect(within(table).getByText("FSSAI: Verified")).toBeInTheDocument();
    expect(within(table).getByText("Eligible")).toBeInTheDocument();
    expect(within(table).getByText("Quotation terms are not verified; excluded from ranking.")).toBeInTheDocument();
    expect(within(table).getByText("0.9123")).toBeInTheDocument();
    expect(within(table).getByText("Policy v3")).toBeInTheDocument();
    expect(screen.getByText("Version 3")).toBeInTheDocument();
    expect(screen.getByText("Decision support only")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });

  it("records a human accept with reason for an eligible quote", async () => {
    const { fetchMock } = render({ routes: detailRoutes({ quotes: [Q_VERIFIED], comparisons: [COMPARISON],
      extra: [{ method: "POST", path: `${BASE}/comparisons/RFQCMP-1/decision`, body: { ...COMPARISON, human_decision: "accepted" } }] }) });
    const select = await screen.findByLabelText(/Quote to accept/);
    expect(within(select).getAllByRole("option").map((o) => o.value)).toEqual(["QUOTE-S1"]);
    await userEvent.click(screen.getByRole("button", { name: "Record decision" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/No supplier is contacted/)).toBeInTheDocument();
    await userEvent.type(within(dialog).getByLabelText(/Reason/), "Lowest verified cost");
    await userEvent.click(within(dialog).getByRole("button", { name: "Accept quote" }));
    expect(await screen.findByText(/Human selection recorded: quote QUOTE-S1 accepted/)).toBeInTheDocument();
    expect(callsTo(fetchMock, `${BASE}/comparisons/RFQCMP-1/decision`, "POST")[0].json)
      .toEqual({ decision: "accept", reason: "Lowest verified cost", quote_id: "QUOTE-S1" });
  });

  it("records a reject without a quote id", async () => {
    const { fetchMock } = render({ routes: detailRoutes({ quotes: [Q_VERIFIED], comparisons: [COMPARISON],
      extra: [{ method: "POST", path: `${BASE}/comparisons/RFQCMP-1/decision`, body: { ...COMPARISON, human_decision: "rejected" } }] }) });
    await userEvent.click(await screen.findByLabelText("Reject this comparison"));
    await userEvent.click(screen.getByRole("button", { name: "Record decision" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/Reason/), "Need more quotes");
    await userEvent.click(within(dialog).getByRole("button", { name: "Reject comparison" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(callsTo(fetchMock, `${BASE}/comparisons/RFQCMP-1/decision`, "POST")[0].json).toEqual({ decision: "reject", reason: "Need more quotes" });
  });

  it("shows a recorded decision instead of controls", async () => {
    render({ routes: detailRoutes({ quotes: [Q_VERIFIED], comparisons: [{ ...COMPARISON, human_decision: "accepted",
      decision: { decision: "accept", quote_id: "QUOTE-S1", reason: "Lowest cost", decided_by: "admin", decided_at: "2026-10-21T12:00:00Z" } }] }) });
    expect(await screen.findByText("Human selection recorded")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record decision" })).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });
});
