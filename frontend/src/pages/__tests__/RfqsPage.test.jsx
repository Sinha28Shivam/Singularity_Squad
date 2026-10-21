import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import RfqsPage from "../RfqsPage";
import { callsTo as calls, renderWithApp } from "../../test/utils";
import { FORBIDDEN } from "./r3helpers";
import { localToIsoWithOffset } from "../../api/vendorSourcing";

const RFQ = {
  rfq_id: "RFQ-20261021-AAAA", request_id: "REQ-20261020-1", service: "catering", status: "open",
  response_deadline: "2026-10-30T11:30:00Z", quotes_received: 2, budget_limit: "50000.00", currency: "INR",
};
const TICKETS = { tickets: [
  { ticket_id: "REQ-20261020-1", title: "Offsite lunch", status: "pending_approval", final_status: "pass" },
  { ticket_id: "REQ-20261020-2", title: "Blocked one", status: "reviewed", final_status: "blocked" },
  { ticket_id: "REQ-20261020-3", title: "Missing fields", status: "reviewed", final_status: "warning" },
] };
const TICKET_OK = { ticket_id: "REQ-20261020-1", status: "pending_approval", workflow_result: {
  final_status: "pass", missing_fields: [],
  request: { service: "catering", attendees: 40, budget_limit: 50000, currency: "INR", date: "2026-11-05", location: "Bengaluru HQ" },
} };
const TICKET_MISSING = { ticket_id: "REQ-20261020-3", status: "reviewed", workflow_result: {
  final_status: "warning", missing_fields: ["budget_limit"], request: { service: "catering", attendees: 10, date: "2026-11-05" },
} };
const VENDORS = {
  internal_vendors: [{ vendor_id: "VEN-001", name: "Catalog Caterers", approved: true }, { vendor_id: "VEN-009", name: "Not approved", approved: false }],
  candidates: [{ candidate_id: "VCAND-A1", name: "Approved Cand", status: "approved" }, { candidate_id: "VCAND-P1", name: "Pending Cand", status: "pending_review" }],
};

function routes(extra = []) {
  return [
    ...extra,
    { method: "GET", path: "/api/vendor-sourcing/rfqs", body: { items: [RFQ], total: 1, limit: 50, offset: 0, next_offset: null } },
    { method: "GET", path: "/api/admin/tickets", body: TICKETS },
    { method: "GET", path: "/api/admin/tickets/REQ-20261020-1", body: TICKET_OK },
    { method: "GET", path: "/api/admin/tickets/REQ-20261020-3", body: TICKET_MISSING },
    { method: "GET", path: "/api/vendor-sourcing/vendors", body: VENDORS },
  ];
}

describe("RfqsPage", () => {
  it("lists RFQs with a status filter and the decision-support banner", async () => {
    const { fetchMock } = renderWithApp(<RfqsPage />, { role: "admin", routes: routes() });
    expect(screen.getByRole("heading", { level: 1, name: "RFQs" })).toBeInTheDocument();
    expect(screen.getByText("Decision support only — no supplier is contacted and nothing is awarded.")).toBeInTheDocument();
    expect(await screen.findByText("RFQ-20261021-AAAA")).toBeInTheDocument();
    expect(document.querySelector('.badge[data-status="open"]')).toHaveTextContent("Open");
    await userEvent.selectOptions(screen.getByLabelText("Status"), "closed");
    await waitFor(() => expect(calls(fetchMock, "/api/vendor-sourcing/rfqs", "GET").at(-1).url.searchParams.get("status")).toBe("closed"));
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });

  it("shows an empty state", async () => {
    renderWithApp(<RfqsPage />, { role: "admin", routes: [{ method: "GET", path: "/api/vendor-sourcing/rfqs",
      body: { items: [], total: 0 } }] });
    expect(await screen.findByText("No RFQs yet")).toBeInTheDocument();
  });

  it("error state retries", async () => {
    let n = 0;
    renderWithApp(<RfqsPage />, { role: "admin", routes: [{ method: "GET", path: "/api/vendor-sourcing/rfqs", status: 503,
      body: () => { n += 1; return { detail: "RFQ service temporarily unavailable." }; } }] });
    expect(await screen.findByText("RFQ service temporarily unavailable.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(n).toBe(2));
  });

  it("creates an RFQ prefilled from a validated ticket with approved recipients and an offset deadline", async () => {
    const { fetchMock } = renderWithApp(<RfqsPage />, {
      route: "/admin/rfqs", path: "/admin/rfqs",
      role: "admin", routes: routes([{ method: "POST", path: "/api/vendor-sourcing/rfqs", status: 201, body: { ...RFQ, rfq_id: "RFQ-NEW-1" } }]),
    });
    await userEvent.click(screen.getByRole("button", { name: "New RFQ" }));
    const select = await screen.findByLabelText(/Validated request/);
    const options = within(select).getAllByRole("option").map((o) => o.value);
    expect(options).toEqual(["", "REQ-20261020-1", "REQ-20261020-3"]);
    await userEvent.selectOptions(select, "REQ-20261020-1");
    expect(await screen.findByText("Bengaluru HQ")).toBeInTheDocument();
    expect(screen.getByText("40")).toBeInTheDocument();

    // Only approved recipients are offered.
    expect(await screen.findByLabelText(/VEN-001/)).toBeInTheDocument();
    expect(screen.getByLabelText(/VCAND-A1/)).toBeInTheDocument();
    expect(screen.queryByText(/VEN-009/)).not.toBeInTheDocument();
    expect(screen.queryByText(/VCAND-P1/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Create RFQ record" }));
    expect(screen.getByText("Select at least one approved recipient.")).toBeInTheDocument();
    await userEvent.click(screen.getByLabelText(/VEN-001/));
    await userEvent.type(screen.getByLabelText("Compliance requirements"), "FSSAI, GST");
    const deadline = screen.getByLabelText(/Response deadline/);
    expect(deadline).toHaveValue("2026-11-04T17:00");
    await userEvent.click(screen.getByRole("button", { name: "Create RFQ record" }));
    await waitFor(() => expect(screen.getByText("navigated")).toBeInTheDocument());
    const body = calls(fetchMock, "/api/vendor-sourcing/rfqs", "POST")[0].json;
    expect(body).toMatchObject({
      request_id: "REQ-20261020-1", service: "catering", quantity: "40", budget_limit: "50000", currency: "INR",
      delivery_date: "2026-11-05", delivery_location: "Bengaluru HQ", warranty_months_required: 0,
      compliance_requirements: ["FSSAI", "GST"], candidate_ids: ["VEN-001"],
    });
    expect(body.response_deadline).toMatch(/^2026-11-04T17:00:00[+-]\d\d:\d\d$/);
    expect(body.response_deadline).toBe(localToIsoWithOffset("2026-11-04T17:00"));
  });

  it("refuses a ticket with missing fields", async () => {
    renderWithApp(<RfqsPage />, { role: "admin", routes: routes() });
    await userEvent.click(screen.getByRole("button", { name: "New RFQ" }));
    await userEvent.selectOptions(await screen.findByLabelText(/Validated request/), "REQ-20261020-3");
    expect(await screen.findByText("This request can't be used for an RFQ")).toBeInTheDocument();
    expect(screen.getByText(/missing: budget_limit/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create RFQ record" })).toBeDisabled();
  });

  it("shows a 422 from the server and keeps the form", async () => {
    renderWithApp(<RfqsPage />, {
      role: "admin", routes: routes([{ method: "POST", path: "/api/vendor-sourcing/rfqs", status: 422,
        body: { detail: "RFQ budget must match the validated source ticket." } }]),
    });
    await userEvent.click(screen.getByRole("button", { name: "New RFQ" }));
    await userEvent.selectOptions(await screen.findByLabelText(/Validated request/), "REQ-20261020-1");
    await userEvent.click(await screen.findByLabelText(/VCAND-A1/));
    await userEvent.click(screen.getByRole("button", { name: "Create RFQ record" }));
    expect(await screen.findByText(/RFQ budget must match/)).toBeInTheDocument();
    expect(screen.getByLabelText(/VCAND-A1/)).toBeChecked();
  });
});

describe("localToIsoWithOffset", () => {
  it("always includes a timezone offset", () => {
    expect(localToIsoWithOffset("2026-10-22T17:00")).toMatch(/^2026-10-22T17:00:00[+-]\d\d:\d\d$/);
    expect(localToIsoWithOffset("")).toBe("");
  });
});
