import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import AnalyticsPage from "../AnalyticsPage";
import { callsTo, renderWithApp } from "../../test/utils";
import { pageTitle } from "../../routes";
import { FORBIDDEN } from "./r3helpers";

const SUMMARY = {
  from: null, to: null,
  tickets_by_status: { approved: 2, reviewed: 1 },
  total_requests: 3, approved_count: 2, draft_po_total: "96325.00", currency: "INR",
  spend_by_vendor: [
    { vendor_id: "VEN-002", vendor_name: "GreenLeaf Kitchens", total: "76125.00", count: 1 },
    { vendor_id: "VEN-001", vendor_name: "Spice Route Caterers", total: "20200.00", count: 1 },
  ],
  spend_by_category: [{ category: "team_lunch", total: "96325.00", count: 2 }],
  spend_by_month: [{ month: "2026-10", total: "96325.00", count: 2 }],
  budget_vs_actual: [{ category: "team_lunch", budget_total: "90000.00", po_total: "96325.00" }],
  invoice_exception_rate: 0.5, invoice_exceptions: 1, invoices_reconciled: 2,
  top_policy_blocks: [{ code: "policy:lead_time", count: 1 }],
  simulated_payments: { count: 1, total: "76125.00" },
  cycle_time_hours: { submit_to_approve_median: 5.5, approve_to_payment_sim_median: null },
};
const EMPTY = { ...SUMMARY, total_requests: 0, approved_count: 0, tickets_by_status: {}, spend_by_vendor: [],
  spend_by_category: [], spend_by_month: [], budget_vs_actual: [], top_policy_blocks: [] };

function routes({ summary = SUMMARY, csv } = {}) {
  return [
    { method: "GET", path: "/api/analytics/summary", body: summary },
    csv || { method: "GET", path: "/api/analytics/export.csv", raw: "ticket_id,category\r\nREQ-1,team_lunch\r\n",
      headers: { "content-type": "text/csv; charset=utf-8" } },
  ];
}

afterEach(() => { vi.restoreAllMocks(); });

describe("AnalyticsPage", () => {
  it("renders KPIs, bar charts, budget table and policy blocks from the server summary", async () => {
    renderWithApp(<AnalyticsPage />, { role: "admin", route: "/admin/analytics", routes: routes() });
    expect(await screen.findByRole("heading", { name: "Spend analytics" })).toBeInTheDocument();
    const kpi = (id) => screen.getByTestId(`kpi-${id}`);
    await waitFor(() => expect(within(kpi("requests")).getByText("3")).toBeInTheDocument());
    expect(within(kpi("approved")).getByText("2")).toBeInTheDocument();
    expect(within(kpi("spend")).getByText(/96,325\.00/)).toBeInTheDocument();
    expect(within(kpi("exceptions")).getByText("50.0%")).toBeInTheDocument();
    expect(within(kpi("exceptions")).getByText("1 of 2 invoices")).toBeInTheDocument();
    expect(within(kpi("payments")).getByText("1")).toBeInTheDocument();

    const vendor = screen.getByTestId("chart-vendor");
    const bars = within(vendor).getAllByRole("listitem");
    expect(bars).toHaveLength(2);
    expect(within(bars[0]).getByText("GreenLeaf Kitchens")).toBeInTheDocument();
    expect(bars[0].querySelector(".bar-fill").style.width).toBe("100%");
    expect(parseFloat(bars[1].querySelector(".bar-fill").style.width)).toBeCloseTo(26.54, 1);
    expect(within(screen.getByTestId("chart-category")).getByText("Team lunch")).toBeInTheDocument();
    expect(within(screen.getByTestId("chart-month")).getByText("2026-10")).toBeInTheDocument();

    const budget = screen.getByRole("table", { name: /budget vs actual/i });
    expect(within(budget).getByText(/90,000\.00/)).toBeInTheDocument();
    const over = budget.querySelector(".analytics-over");
    expect(over).not.toBeNull();
    expect(over.textContent).toMatch(/^-.*6,325\.00$/);
    const blocks = screen.getByRole("table", { name: /blocking reasons/i });
    expect(within(blocks).getByText("policy:lead_time")).toBeInTheDocument();
    expect(screen.getByText(/submit → approve: 5\.5 h/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });

  it("sends the date range as from/to and validates order", async () => {
    const user = userEvent.setup();
    const { fetchMock } = renderWithApp(<AnalyticsPage />, { role: "approver", route: "/analytics", routes: routes() });
    await screen.findByTestId("kpi-requests");
    await user.type(screen.getByLabelText("From"), "2026-10-31");
    await user.type(screen.getByLabelText("To"), "2026-10-01");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/start date must be on or before/i);
    expect(callsTo(fetchMock, "/api/analytics/summary")).toHaveLength(1);

    await user.clear(screen.getByLabelText("From"));
    await user.type(screen.getByLabelText("From"), "2026-09-01");
    await user.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(callsTo(fetchMock, "/api/analytics/summary")).toHaveLength(2));
    const url = callsTo(fetchMock, "/api/analytics/summary")[1].url;
    expect(url.searchParams.get("from")).toBe("2026-09-01");
    expect(url.searchParams.get("to")).toBe("2026-10-01");
  });

  it("exports CSV with the bearer token and the active range", async () => {
    const user = userEvent.setup();
    const createObjectURL = vi.fn(() => "blob:analytics");
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    const { fetchMock } = renderWithApp(<AnalyticsPage />, { role: "admin", routes: routes() });
    await screen.findByTestId("kpi-requests");
    await user.click(screen.getByRole("button", { name: /export csv/i }));
    expect(await screen.findByText("CSV export downloaded.")).toBeInTheDocument();
    const [call] = callsTo(fetchMock, "/api/analytics/export.csv", "GET");
    expect(call.init.headers.Authorization).toBe("Bearer test-token");
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const blob = createObjectURL.mock.calls[0][0];
    expect(await blob.text()).toContain("REQ-1,team_lunch");
    expect(click).toHaveBeenCalled();
  });

  it("shows a safe error when export is forbidden", async () => {
    const user = userEvent.setup();
    renderWithApp(<AnalyticsPage />, { role: "approver", routes: routes({ csv: {
      method: "GET", path: "/api/analytics/export.csv", status: 403, body: { detail: "Approver or admin access required." } } }) });
    await screen.findByTestId("kpi-requests");
    await user.click(screen.getByRole("button", { name: /export csv/i }));
    expect(await screen.findByText("Approver or admin access required.")).toBeInTheDocument();
  });

  it("shows empty and error states", async () => {
    const { unmount } = renderWithApp(<AnalyticsPage />, { role: "admin", routes: routes({ summary: EMPTY }) });
    expect(await screen.findByText("No requests in this range")).toBeInTheDocument();
    unmount();
    renderWithApp(<AnalyticsPage />, { role: "admin", routes: [
      { method: "GET", path: "/api/analytics/summary", status: 500, body: { detail: "boom internals" } }] });
    expect(await screen.findByText("Analytics aren't available")).toBeInTheDocument();
    expect(screen.queryByText(/boom internals/)).not.toBeInTheDocument();
  });

  it("has page titles for admin and approver routes", () => {
    expect(pageTitle("/admin/analytics")).toBe("Spend analytics");
    expect(pageTitle("/analytics")).toBe("Spend analytics");
  });
});
