import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import VendorSourcingPage from "../VendorSourcingPage";
import RfqDetailPage from "../RfqDetailPage";
import { callsTo, renderWithApp } from "../../test/utils";
import { FORBIDDEN } from "./r3helpers";

const CAND = {
  candidate_id: "VCAND-ABC123", name: "Fresh Caterers", services: ["catering"], regions: ["Bengaluru"], source: "manual",
  status: "pending_review", created_by: "admin", created_at: "2026-10-20T10:00:00Z", compliance_evidence: [], status_history: [],
};
const SEARCH = { internal_vendors: [], candidates: [CAND], candidates_total: 1 };

function vendorRoutes(cand, extra = []) {
  return [
    ...extra,
    { method: "GET", path: "/api/vendor-sourcing/vendors", body: SEARCH },
    { method: "GET", path: "/api/vendor-sourcing/candidates", body: { items: [cand], total: 1, limit: 20, offset: 0, next_offset: null } },
    { method: "GET", path: `/api/vendor-sourcing/candidates/${cand.candidate_id}`, body: cand },
    { method: "GET", path: `/api/vendor-sourcing/candidates/${cand.candidate_id}/events`, body: { events: [] } },
  ];
}

async function openDetail() {
  await userEvent.click(screen.getByRole("tab", { name: "Candidates" }));
  await userEvent.click(await screen.findByText("Fresh Caterers"));
  return (await screen.findByRole("heading", { name: "Fresh Caterers", level: 2 })).closest("section");
}

describe("VendorSourcingPage — allowed_decisions", () => {
  it("offers only the decisions the server lists", async () => {
    renderWithApp(<VendorSourcingPage />, { role: "admin", routes: vendorRoutes({ ...CAND, allowed_decisions: ["reject"] }) });
    const detail = await openDetail();
    expect(within(detail).getByRole("button", { name: "Reject" })).toBeInTheDocument();
    expect(within(detail).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(within(detail).queryByRole("button", { name: "Suspend" })).toBeNull();
  });

  it("shows no controls when the server allows none", async () => {
    renderWithApp(<VendorSourcingPage />, { role: "admin", routes: vendorRoutes({ ...CAND, allowed_decisions: [] }) });
    const detail = await openDetail();
    expect(within(detail).getByText(/No further decisions are available/)).toBeInTheDocument();
  });

  it("uses the decision response's allowed_decisions after a decision", async () => {
    const approved = { ...CAND, status: "approved", allowed_decisions: ["suspend"] };
    renderWithApp(<VendorSourcingPage />, { role: "admin", routes: vendorRoutes({ ...CAND, allowed_decisions: ["approve", "reject", "suspend"] }, [
      { method: "POST", path: "/api/vendor-sourcing/candidates/VCAND-ABC123/decision", body: approved },
    ]) });
    const detail = await openDetail();
    await userEvent.click(within(detail).getByRole("button", { name: "Approve" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/Reason/), "Evidence checked");
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(within(detail).queryByRole("button", { name: "Approve" })).toBeNull());
    expect(within(detail).getByRole("button", { name: "Suspend" })).toBeInTheDocument();
  });

  it("falls back to the local transition table when the field is absent", async () => {
    renderWithApp(<VendorSourcingPage />, { role: "admin", routes: vendorRoutes({ ...CAND, status: "suspended" }) });
    const detail = await openDetail();
    expect(within(detail).getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(within(detail).queryByRole("button", { name: "Reject" })).toBeNull();
  });
});

describe("VendorSourcingPage — activity metrics", () => {
  function openActivity() {
    const details = screen.getByText("Activity since the API started").closest("details");
    details.open = true;
    fireEvent(details, new Event("toggle"));
    return details;
  }

  it("loads counters on demand and labels them as process-local", async () => {
    const { fetchMock } = renderWithApp(<VendorSourcingPage />, { role: "admin", routes: [
      { method: "GET", path: "/api/vendor-sourcing/vendors", body: SEARCH },
      { method: "GET", path: "/api/vendor-sourcing/metrics", body: { scope: "process", note: "Counters reset when the API restarts.",
        counters: { rfqs_created: { total: 3, by_label: {} }, decisions_by_type: { total: 2, by_label: { approve: 1, reject: 1 } } } } },
    ] });
    await screen.findByText("Fresh Caterers");
    expect(callsTo(fetchMock, "/api/vendor-sourcing/metrics", "GET")).toHaveLength(0);
    const details = openActivity();
    const grid = await within(details).findByTestId("sourcing-activity");
    expect(within(grid).getByText("RFQs created")).toBeInTheDocument();
    expect(within(grid).getByText("3")).toBeInTheDocument();
    expect(within(grid).getByText("Candidate decisions")).toBeInTheDocument();
    expect(within(details).getByText(/Counts since the API started/)).toBeInTheDocument();
    expect(within(details).getByText(/Approve: 1 · Reject: 1/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });

  it("shows empty and error states", async () => {
    let n = 0;
    renderWithApp(<VendorSourcingPage />, { role: "admin", routes: [
      { method: "GET", path: "/api/vendor-sourcing/vendors", body: SEARCH },
      { method: "GET", path: "/api/vendor-sourcing/metrics", get status() { return n++ === 0 ? 503 : 200; },
        body: () => (n <= 1 ? { detail: "Metrics unavailable." } : { counters: {} }) },
    ] });
    await screen.findByText("Fresh Caterers");
    const details = openActivity();
    expect(await within(details).findByText("Activity is unavailable")).toBeInTheDocument();
    await userEvent.click(within(details).getByRole("button", { name: "Try again" }));
    expect(await within(details).findByText("No activity recorded since the API started.")).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------------------------------ RFQ score breakdown
const RID = "RFQ-20261021-AAAA";
const BASE = `/api/vendor-sourcing/rfqs/${RID}`;
const RFQ = { rfq_id: RID, request_id: "REQ-1", service: "catering", status: "open", accepting_quotes: true, quantity: "40",
  budget_limit: "50000.00", currency: "INR", delivery_date: "2026-11-05", candidate_ids: ["VEN-001"], created_at: "2026-10-21T08:00:00Z" };
const ROW = { quote_id: "QUOTE-S1", vendor_id: "VEN-001", status: "verified", eligible: true, eligibility: "eligible", rank: 1,
  total_cost: "20200.00", reasons: [], score: { total: "0.9123" } };
const LIST_CMP = { comparison_id: "RFQCMP-1", rfq_id: RID, currency: "INR", budget_limit: "50000.00", eligible_count: 1,
  recommended_quote_id: "QUOTE-S1", scoring_policy_version: 3, human_decision: "pending", quotes: [ROW] };
const FULL_SCORE = { total: "0.9123", components: { cost: "1.0000", delivery: "0.8000", warranty: "0.5000" },
  weights: { cost: "0.6", delivery: "0.3", warranty: "0.1" }, raw: { total_cost: "20200.00", days_before_required: 1, warranty_months: 6 },
  explanatory_only: true };

function rfqRoutes(single) {
  return [
    { method: "GET", path: BASE, body: { rfq: RFQ, quotes: [], events: [], pending_events: [] } },
    { method: "GET", path: `${BASE}/comparisons`, body: { items: [LIST_CMP] } },
    { method: "GET", path: `${BASE}/comparisons/RFQCMP-1`, ...single },
  ];
}
const renderRfq = (single) => renderWithApp(<RfqDetailPage />, { role: "admin", route: `/admin/rfqs/${RID}`, path: "/admin/rfqs/:rfqId", routes: rfqRoutes(single) });

describe("RfqDetailPage — score breakdown", () => {
  it("fetches the comparison by id and shows components × weights and raw values", async () => {
    const { fetchMock } = renderRfq({ body: { ...LIST_CMP, quotes: [{ ...ROW, score: FULL_SCORE }] } });
    const table = await screen.findByRole("table", { name: "Quote comparison" });
    const breakdown = (await within(table).findByText("How this score was calculated")).closest("details");
    expect(within(breakdown).getByText("Explanatory only — does not change ranking.")).toBeInTheDocument();
    const rows = within(breakdown).getAllByRole("row").slice(1).map((r) => [...r.querySelectorAll("td")].map((td) => td.textContent));
    expect(rows).toEqual([["Cost", "1", "0.6", "0.6"], ["Delivery", "0.8", "0.3", "0.24"], ["Warranty", "0.5", "0.1", "0.05"]]);
    expect(within(breakdown).getByText("Days before required date")).toBeInTheDocument();
    expect(within(breakdown).getByText("₹20,200.00")).toBeInTheDocument();
    expect(within(breakdown).getByText("6")).toBeInTheDocument();
    expect(within(breakdown).getByText("0.9123")).toBeInTheDocument();
    expect(within(table).getAllByText("0.9123")).toHaveLength(2);
    expect(callsTo(fetchMock, `${BASE}/comparisons/RFQCMP-1`, "GET")).toHaveLength(1);
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });

  it("falls back to the list row (no breakdown) when the single-comparison endpoint 404s", async () => {
    renderRfq({ status: 404, body: { detail: "Not Found" } });
    const table = await screen.findByRole("table", { name: "Quote comparison" });
    expect(within(table).getByText("0.9123")).toBeInTheDocument();
    await waitFor(() => expect(within(table).queryByText("How this score was calculated")).toBeNull());
  });
});
