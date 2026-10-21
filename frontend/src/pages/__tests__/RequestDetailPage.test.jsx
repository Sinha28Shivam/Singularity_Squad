import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import RequestDetailPage from "../RequestDetailPage";
import { renderWithApp } from "../../test/utils";

// Local copy of test/utils callsTo that also accepts URL objects (the shared helper reads `input.url`, which URL lacks).
function callsTo(fetchMock, path, method) {
  return fetchMock.mock.calls
    .map(([input, init = {}]) => ({ url: new URL(typeof input === "string" ? input : input.url ?? String(input)), init }))
    .filter(({ url, init }) => (path instanceof RegExp ? path.test(url.pathname) : url.pathname === path) &&
      (!method || (init.method || "GET").toUpperCase() === method))
    .map((c) => ({ ...c, json: typeof c.init.body === "string" ? JSON.parse(c.init.body) : undefined }));
}

const FORBIDDEN = /\b(Paid|Awarded|PO sent)\b/;
const TID = "REQ-20261021-AAAA";
const PATH = `/api/my/tickets/${TID}`;
const SUB = { title: "Team lunch Q4", event_category: "team_lunch", service: "lunch", event_date: "2030-10-30", attendees: 100, budget_inr: 100000, location: "Bengaluru", approved_vendor_only: true, notes: "Veg" };

function view(over = {}) {
  return {
    ticket_id: TID, status: "needs_info", submission: SUB, outcome: "More information needed — please update your request.",
    clarification_questions: ["What time does the event start?"], po_summary: null, notes: [], decision: null, approval: null,
    policy_evidence: null, policy_status: null, created_at: "2026-10-21T09:00:00", updated_at: "2026-10-21T10:00:00", ...over,
  };
}
const render = (v, extra = []) => renderWithApp(<RequestDetailPage />, {
  route: `/requests/${TID}`, path: "/requests/:ticketId",
  routes: [{ method: "GET", path: PATH, body: v }, ...extra],
});

describe("RequestDetailPage", () => {
  it("shows ID, status, outcome and clarification questions", async () => {
    const { container } = render(view());
    expect(await screen.findByTestId("ticket-id")).toHaveTextContent(TID);
    expect(screen.getByRole("heading", { level: 1, name: "Team lunch Q4" })).toBeInTheDocument();
    expect(screen.getByTestId("outcome")).toHaveTextContent("More information needed");
    expect(screen.getByText("We need a bit more information")).toBeInTheDocument();
    expect(screen.getByText("What time does the event start?")).toBeInTheDocument();
    expect(screen.getByText("Bengaluru")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it.each(["submitted", "needs_info", "changes_requested", "approval_expired"])("allows edit & resubmit when status is %s", async (status) => {
    render(view({ status }));
    expect(await screen.findByRole("button", { name: "Edit and resubmit" })).toBeInTheDocument();
  });

  it.each(["processing", "reviewed", "pending_approval", "approved", "rejected", "failed"])("hides edit & resubmit when status is %s", async (status) => {
    render(view({ status, clarification_questions: [] }));
    await screen.findByTestId("ticket-id");
    expect(screen.queryByRole("button", { name: "Edit and resubmit" })).toBeNull();
  });

  it("resubmits with PUT, prefilled with the stored submission", async () => {
    const { fetchMock } = render(view(), [
      { method: "PUT", path: PATH, body: view({ status: "submitted", submission: { ...SUB, attendees: 80 }, clarification_questions: [], outcome: "Waiting for an admin to review your request." }) },
    ]);
    await userEvent.click(await screen.findByRole("button", { name: "Edit and resubmit" }));
    const att = screen.getByLabelText(/^Attendees/);
    expect(att).toHaveValue(100);
    expect(screen.getByLabelText(/^Summary/)).toHaveValue("Team lunch Q4");
    await userEvent.clear(att);
    await userEvent.type(att, "80");
    await userEvent.click(screen.getByRole("button", { name: "Resubmit request" }));
    expect(await screen.findByText(`Request ${TID} was resubmitted.`)).toBeInTheDocument();
    const [put] = callsTo(fetchMock, PATH, "PUT");
    expect(put.json).toMatchObject({ title: "Team lunch Q4", attendees: 80, budget_inr: 100000, event_date: "2030-10-30" });
    expect(screen.getByTestId("outcome")).toHaveTextContent("Waiting for an admin");
  });

  it("keeps edits and shows the server's message when resubmit is refused", async () => {
    render(view(), [{ method: "PUT", path: PATH, status: 409, body: { detail: "Ticket cannot be edited while status is 'processing'." } }]);
    await userEvent.click(await screen.findByRole("button", { name: "Edit and resubmit" }));
    await userEvent.clear(screen.getByLabelText(/^Location/));
    await userEvent.type(screen.getByLabelText(/^Location/), "Pune");
    await userEvent.click(screen.getByRole("button", { name: "Resubmit request" }));
    expect(await screen.findByText("Ticket cannot be edited while status is 'processing'.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Location/)).toHaveValue("Pune");
  });

  it("shows the draft PO summary with the not-an-order note and a requester-safe approval panel", async () => {
    const { container } = render(view({
      status: "pending_approval", clarification_questions: [],
      po_summary: { po_id: "PO-9", vendor_name: "Spice Route", total: 68250, currency: "INR", status: "draft", approval_status: "pending" },
      approval: { current: { approval_id: "APR-1", status: "pending", request_version: 1, amount: 68250, currency: "INR", required_role: "approver", reasons: ["Above ₹50,000"], decisions: [] }, history: [], safety_notice: "x" },
    }));
    expect(await screen.findByText("Draft purchase order")).toBeInTheDocument();
    expect(screen.getByText("PO-9")).toBeInTheDocument();
    expect(screen.getByText("A draft PO is not an order. Nothing is purchased.")).toBeInTheDocument();
    const approval = screen.getByRole("heading", { name: "Approval" }).closest("section");
    expect(within(approval).getAllByText("Pending review").length).toBeGreaterThan(0);
    expect(within(approval).getByText("Above ₹50,000")).toBeInTheDocument();
    expect(within(approval).getByText("Approval is internal authorization only. It does not send a PO or move money.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it("fetches the approval only when the backend omitted it", async () => {
    const v = view({ status: "changes_requested" });
    delete v.approval;
    const { fetchMock } = render(v, [{ method: "GET", path: `/api/tickets/${TID}/approval`, status: 404, body: { detail: "nope" } }]);
    await screen.findByTestId("ticket-id");
    await waitFor(() => expect(callsTo(fetchMock, `/api/tickets/${TID}/approval`, "GET")).toHaveLength(1));
    expect(screen.getByText(/No approval request/)).toBeInTheDocument();
  });

  it("adds a note and validates empty notes", async () => {
    const { fetchMock } = render(view(), [
      { method: "POST", path: `${PATH}/notes`, body: view({ notes: [{ author: "alice", role: "user", text: "Starts at 1pm", created_at: "2026-10-21T11:00:00" }] }) },
    ]);
    await screen.findByTestId("ticket-id");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    expect(screen.getByText("Write a note before adding it.")).toBeInTheDocument();
    expect(callsTo(fetchMock, `${PATH}/notes`, "POST")).toHaveLength(0);
    await userEvent.type(screen.getByLabelText("Add a note for the operations team"), "Starts at 1pm");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    expect(await screen.findByText("Note added.")).toBeInTheDocument();
    expect(screen.getAllByText("Starts at 1pm").length).toBeGreaterThan(0);
    expect(callsTo(fetchMock, `${PATH}/notes`, "POST")[0].json).toEqual({ text: "Starts at 1pm" });
  });

  it("shows a safe error and retries", async () => {
    let n = 0;
    renderWithApp(<RequestDetailPage />, {
      route: `/requests/${TID}`, path: "/requests/:ticketId",
      routes: (m, url) => {
        if (m === "GET" && url.pathname === PATH) { n += 1; return n === 1 ? { status: 404, body: { detail: "Ticket not found." } } : { body: view() }; }
        return undefined;
      },
    });
    expect(await screen.findByText("Ticket not found.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("ticket-id")).toHaveTextContent(TID);
  });
});
