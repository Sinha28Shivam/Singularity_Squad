import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import AdminRequestDetailPage from "../AdminRequestDetailPage";
import { renderWithApp } from "../../test/utils";

function callsTo(fetchMock, path, method) {
  return fetchMock.mock.calls
    .map(([input, init = {}]) => ({ url: new URL(typeof input === "string" ? input : input.url ?? String(input)), init }))
    .filter(({ url, init }) => url.pathname === path && (!method || (init.method || "GET").toUpperCase() === method))
    .map((c) => ({ ...c, json: typeof c.init.body === "string" ? JSON.parse(c.init.body) : undefined }));
}

const FORBIDDEN = /\b(Paid|Awarded|PO sent)\b/;
const TID = "REQ-20261021-AAAA";
const BASE = `/api/admin/tickets/${TID}`;
const INVOICES = [{ invoice_id: "INV-5001", vendor_id: "V-1" }, { invoice_id: "INV-5003", vendor_id: "V-1" }];
const PO = { po_id: "PO-9", request_id: TID, vendor_name: "Spice Route", vendor_id: "V-1", status: "draft", event_date: "2026-10-30",
  line_items: [], subtotal: 65000, tax: 3250, total: 68250, currency: "INR", required_approver: "Finance manager" };
const RESULT = {
  persisted: true, request: { category: "team_lunch", service: "lunch", attendees: 100 }, missing_fields: [],
  policy: { summary: "OK", checks: [], clauses: [] }, vendors: { selected: { vendor_id: "V-1", name: "Spice Route" }, evaluations: [] },
  procurement: { po: PO, checks: [], calculation_steps: [] }, reconciliation: null,
  critic: { status: "pass", summary: "All good.", findings: [] }, audit_trail: [], final_status: "pass", human_approval_required: true,
};
const VIEW = {
  ticket_id: TID, status: "pending_approval", owner_username: "alice", owner_name: "Alice Kumar",
  submission: { title: "Team lunch Q4" }, workflow_result: RESULT, notes: [], run_count: 1, approval: null,
  policy_evaluation: null, policy_status: null, invoice_match: null, created_at: "2026-10-21T09:00:00",
};

function setup(reconcile) {
  return renderWithApp(<AdminRequestDetailPage />, {
    role: "admin", route: `/admin/requests/${TID}`, path: "/admin/requests/:ticketId",
    routes: (m, url) => {
      const key = `${m} ${url.pathname}`;
      if (key === `GET ${BASE}`) return { body: VIEW };
      if (key === "GET /api/invoices") return { body: INVOICES };
      if (key === "POST /api/reconcile") return reconcile();
      return undefined;
    },
  });
}

async function openPanel() {
  await screen.findByTestId("ticket-id");
  const details = screen.getByText("Check another invoice").closest("details");
  details.open = true;
  fireEvent(details, new Event("toggle"));
  return details;
}

describe("AdminRequestDetailPage — ad-hoc invoice re-check", () => {
  it("posts {po, invoice_id} and shows a labelled, unsaved mismatch result with checks", async () => {
    const { fetchMock, container } = setup(() => ({ body: {
      invoice_id: "INV-5003", status: "mismatch", expected_total: 68250, invoiced_total: 70000, variance: 1750,
      recommendation: "Hold for review.", checks: [{ name: "total_matches", status: "fail", detail: "Invoice total differs by ₹1,750." }],
    } }));
    const panel = await openPanel();
    expect(within(panel).getByText("Preview only — not saved")).toBeInTheDocument();
    await within(within(panel).getByLabelText("Invoice to re-check")).findByRole("option", { name: /INV-5003/ });
    const btn = within(panel).getByRole("button", { name: "Check invoice" });
    expect(btn).toBeDisabled();
    await userEvent.selectOptions(within(panel).getByLabelText("Invoice to re-check"), "INV-5003");
    await userEvent.click(btn);
    const out = await screen.findByTestId("invoice-preview");
    expect(within(out).getByText(/Preview only — not saved/)).toBeInTheDocument();
    expect(within(out).getByText("Mismatch")).toBeInTheDocument();
    expect(within(out).getByText("Invoice total differs by ₹1,750.")).toBeInTheDocument();
    expect(within(out).getByText("Hold for review.")).toBeInTheDocument();
    const [call] = callsTo(fetchMock, "/api/reconcile", "POST");
    expect(call.json).toEqual({ po: PO, invoice_id: "INV-5003" });
    // Preview must not write to the ticket.
    expect(callsTo(fetchMock, `${BASE}/invoice-match`, "POST")).toHaveLength(0);
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it("shows duplicate results and server errors", async () => {
    let n = 0;
    setup(() => {
      n += 1;
      return n === 1 ? { status: 404, body: { detail: "Invoice not found." } }
        : { body: { invoice_id: "INV-5001", status: "duplicate", expected_total: 68250, invoiced_total: 68250, variance: 0, recommendation: "Already billed.", checks: [] } };
    });
    const panel = await openPanel();
    await within(within(panel).getByLabelText("Invoice to re-check")).findByRole("option", { name: /INV-5001/ });
    await userEvent.selectOptions(within(panel).getByLabelText("Invoice to re-check"), "INV-5001");
    await userEvent.click(within(panel).getByRole("button", { name: "Check invoice" }));
    expect(await within(panel).findByText("Invoice not found.")).toBeInTheDocument();
    await userEvent.click(within(panel).getByRole("button", { name: "Check invoice" }));
    expect(await within(await screen.findByTestId("invoice-preview")).findByText("Duplicate")).toBeInTheDocument();
  });

  it("is not offered when there is no draft PO", async () => {
    renderWithApp(<AdminRequestDetailPage />, {
      role: "admin", route: `/admin/requests/${TID}`, path: "/admin/requests/:ticketId",
      routes: (m, url) => {
        if (url.pathname === BASE) return { body: { ...VIEW, workflow_result: { ...RESULT, procurement: null } } };
        if (url.pathname === "/api/invoices") return { body: INVOICES };
        return undefined;
      },
    });
    await screen.findByTestId("ticket-id");
    expect(screen.queryByText("Check another invoice")).toBeNull();
  });
});
