import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import AdminRequestDetailPage from "../AdminRequestDetailPage";
import { renderWithApp } from "../../test/utils";

// Local copy of test/utils callsTo that also accepts URL objects (the shared helper reads `input.url`, which URL lacks).
function callsTo(fetchMock, path, method) {
  return fetchMock.mock.calls
    .map(([input, init = {}]) => ({ url: new URL(typeof input === "string" ? input : input.url ?? String(input)), init }))
    .filter(({ url, init }) => (path instanceof RegExp ? path.test(url.pathname) : url.pathname === path) &&
      (!method || (init.method || "GET").toUpperCase() === method))
    .map((c) => ({ ...c, json: typeof c.init.body === "string" ? JSON.parse(c.init.body) : undefined }));
}
import { todayIso } from "../../api/requests";

const FORBIDDEN = /\b(Paid|Awarded|PO sent)\b/;
const TID = "REQ-20261021-AAAA";
const BASE = `/api/admin/tickets/${TID}`;
const INVOICES = [{ invoice_id: "INV-5001", vendor_id: "V-1" }, { invoice_id: "INV-5002", vendor_id: "V-1" }];
const PO = { po_id: "PO-9", vendor_name: "Spice Route", vendor_id: "V-1", status: "draft", line_items: [], subtotal: 65000, tax: 3250, total: 68250, currency: "INR", required_approver: "Finance manager" };
const RESULT = {
  persisted: true, request: { category: "team_lunch", service: "lunch", attendees: 100 }, missing_fields: [],
  policy: { summary: "OK", checks: [{ name: "per_head", status: "pass", detail: "fine" }], clauses: [] },
  vendors: { selected: { vendor_id: "V-1", name: "Spice Route" }, evaluations: [], replanned: false, explanation: "Best." },
  procurement: { po: PO, checks: [], calculation_steps: [] }, reconciliation: null,
  critic: { status: "pass", summary: "All good.", findings: [] }, audit_trail: [], final_status: "pass", human_approval_required: true,
};

function view(over = {}) {
  return {
    ticket_id: TID, status: "submitted", owner_username: "alice", owner_name: "Alice Kumar", owner_email: "alice@example.com",
    submission: { title: "Team lunch Q4", event_category: "team_lunch", service: "lunch", event_date: "2026-10-30", attendees: 100, budget_inr: 100000 },
    raw_text: "Team lunch for 100", workflow_result: null, notes: [], decision: null, run_count: 0, last_run_by: null, error: null,
    approval: null, policy_evaluation: null, policy_status: null, invoice_match: null, created_at: "2026-10-21T09:00:00", ...over,
  };
}

/** Stateful backend: `state.view` is returned by GET; handlers can mutate it. */
function setup(initial, handlers = {}) {
  const state = { view: initial };
  const result = renderWithApp(<AdminRequestDetailPage />, {
    role: "admin", route: `/admin/requests/${TID}`, path: "/admin/requests/:ticketId",
    routes: (m, url, init) => {
      const key = `${m} ${url.pathname}`;
      if (handlers[key]) return handlers[key](state, init, url);
      if (key === `GET ${BASE}`) return { body: state.view };
      if (key === "GET /api/invoices") return { body: INVOICES };
      return undefined;
    },
  });
  return { ...result, state };
}

describe("AdminRequestDetailPage", () => {
  it("runs the workflow with an optional invoice and today's demo date, then shows readable steps", async () => {
    const { fetchMock, container } = setup(view(), {
      [`POST ${BASE}/run`]: (s) => { s.view = view({ status: "pending_approval", run_count: 1, workflow_result: RESULT }); return { body: s.view }; },
    });
    expect(await screen.findByTestId("ticket-id")).toHaveTextContent(TID);
    expect(screen.getByText(/No workflow result yet/)).toBeInTheDocument();
    expect(screen.getByLabelText("Demo date (today)")).toHaveValue(todayIso());
    await screen.findByRole("option", { name: /INV-5002/ });
    await userEvent.selectOptions(screen.getByLabelText(/Invoice to check/), "INV-5002");
    await userEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    expect(await screen.findByText("Workflow finished — status: Waiting for approval.")).toBeInTheDocument();
    expect(callsTo(fetchMock, `${BASE}/run`, "POST")[0].json).toEqual({ invoice_id: "INV-5002", today: todayIso() });
    expect(screen.getByTestId("overall-outcome")).toHaveTextContent("Passed");
    expect(screen.getByRole("button", { name: "Re-run workflow" })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it("shows the server's 409 message when the ticket is not runnable", async () => {
    setup(view({ status: "failed", run_count: 1 }), {
      [`POST ${BASE}/run`]: () => ({ status: 409, body: { detail: "Ticket is already being processed or is no longer runnable." } }),
    });
    await userEvent.click(await screen.findByRole("button", { name: "Re-run workflow" }));
    expect(await screen.findByText("Ticket is already being processed or is no longer runnable.")).toBeInTheDocument();
  });

  it.each(["approved", "rejected", "processing", "changes_requested"])("disables running when status is %s", async (status) => {
    setup(view({ status, run_count: 1, workflow_result: RESULT }));
    expect(await screen.findByRole("button", { name: "Re-run workflow" })).toBeDisabled();
  });

  it("asks for confirmation before re-running a pending approval", async () => {
    const { fetchMock } = setup(view({ status: "pending_approval", run_count: 1, workflow_result: RESULT, approval: { current: { approval_id: "APR-1", status: "pending", request_version: 1, decisions: [] }, history: [] } }), {
      [`POST ${BASE}/run`]: (s) => { s.view = { ...s.view, run_count: 2 }; return { body: s.view }; },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Re-run workflow" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Re-run and replace the pending approval?")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(callsTo(fetchMock, `${BASE}/run`, "POST")).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Re-run workflow" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Re-run workflow" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(callsTo(fetchMock, `${BASE}/run`, "POST")).toHaveLength(1);
  });

  it("admins can never approve: shows who must decide and no approve control", async () => {
    setup(view({
      status: "pending_approval", run_count: 1, workflow_result: RESULT,
      approval: { current: { approval_id: "APR-1", status: "pending", request_version: 1, required_role: "approver", amount: 68250, decisions: [] }, history: [] },
    }));
    await screen.findByTestId("ticket-id");
    expect(screen.getAllByText(/Admins can't approve requests/).length).toBeGreaterThan(0);
    expect(screen.getByText("An eligible approver")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^approve/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Close request" })).toBeNull();
    expect(screen.getByText("Approval is internal authorization only. It does not send a PO or move money.")).toBeInTheDocument();
  });

  it("closes a reviewed request only after confirming with a reason", async () => {
    const { fetchMock } = setup(view({ status: "reviewed", run_count: 1, workflow_result: { ...RESULT, final_status: "blocked" } }), {
      [`POST ${BASE}/decision`]: (s, init) => {
        if (!s.failedOnce) { s.failedOnce = true; return { status: 409, body: { detail: "Ticket status changed; please reload and try again." } }; }
        s.view = view({ status: "rejected", run_count: 1, decision: { decision: "reject", by: "admin", at: "2026-10-21T12:00:00", note: JSON.parse(init.body).note } });
        return { body: s.view };
      },
    });
    await userEvent.click(await screen.findByRole("button", { name: "Close request" }));
    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Close request" });
    expect(confirm).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText(/Reason for the requester/), "Blocked by policy");
    await userEvent.click(confirm);
    expect(await within(dialog).findByText("Ticket status changed; please reload and try again.")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Close request" }));
    expect(await screen.findByText(`Request ${TID} was closed.`)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    const calls = callsTo(fetchMock, `${BASE}/decision`, "POST");
    expect(calls).toHaveLength(2);
    expect(calls[1].json).toEqual({ decision: "reject", note: "Blocked by policy" });
    expect(calls.every((c) => c.json.decision !== "approve")).toBe(true);
  });

  it("matches a post-approval invoice for an approved ticket with no reconciliation", async () => {
    const { fetchMock } = setup(view({ status: "approved", run_count: 1, workflow_result: RESULT }), {
      [`POST ${BASE}/invoice-match`]: (s) => {
        s.view = { ...s.view, invoice_match: { invoice_id: "INV-5001", status: "match", checks: [], expected_total: 68250, invoiced_total: 68250, variance: 0, matched_by: "admin", matched_at: "2026-10-21T12:00:00", recommendation: "Matches." } };
        return { body: s.view };
      },
    });
    expect(await screen.findByRole("heading", { name: "Invoice after approval" })).toBeInTheDocument();
    await within(screen.getByLabelText("Invoice")).findByRole("option", { name: /INV-5001/ });
    await userEvent.selectOptions(screen.getByLabelText("Invoice"), "INV-5001");
    await userEvent.click(screen.getByRole("button", { name: "Match invoice" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Match invoice" }));
    expect(await screen.findByText("Invoice INV-5001 matched against the approved draft PO.")).toBeInTheDocument();
    expect(screen.getByText("Matched")).toBeInTheDocument();
    expect(callsTo(fetchMock, `${BASE}/invoice-match`, "POST")[0].json).toEqual({ invoice_id: "INV-5001" });
  });

  it("disputes a mismatched invoice, then matches the corrected one", async () => {
    const MISMATCH = { invoice_id: "INV-5002", status: "mismatch", post_approval: true, expected_total: 68250, invoiced_total: 70350,
      variance: 2100, matched_by: "admin", matched_at: "2026-10-21T12:00:00", recommendation: "Hold payment.",
      checks: [{ name: "line_item:Lunch", status: "fail", detail: "unit price 720 vs PO 700" }] };
    const { fetchMock } = setup(view({ status: "approved", run_count: 1, workflow_result: RESULT, invoice_match: MISMATCH, invoice_status: "mismatch" }), {
      [`POST ${BASE}/invoice-exception`]: (s, init) => {
        const { action, note } = JSON.parse(init.body);
        s.view = { ...s.view, invoice_status: "disputed", invoice_exception: { invoice_id: "INV-5002", state: "disputed", reason: note, updated_by: "admin" },
          invoice_history: [{ action, by: "admin", at: "2026-10-21T12:05:00", note, invoice_id: "INV-5002" }] };
        return { body: s.view };
      },
      [`POST ${BASE}/invoice-match`]: (s) => {
        s.view = { ...s.view, invoice_status: "matched", invoice_exception: null,
          invoice_match: { ...MISMATCH, invoice_id: "INV-5001", status: "match", invoiced_total: 68250, variance: 0, checks: [] },
          invoice_history: [...s.view.invoice_history, { invoice_id: "INV-5002", status: "mismatch", superseded_by: "INV-5001", superseded_by_user: "admin", superseded_at: "2026-10-21T12:10:00" }] };
        return { body: s.view };
      },
    });
    expect(await screen.findByText("Invoice mismatch")).toBeInTheDocument();
    expect(screen.getByText(/Dispute the mismatch with the vendor/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Dispute with vendor" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("Nothing is sent to the vendor");
    await userEvent.type(within(dialog).getByLabelText(/Reason/), "Rate 720 vs 700");
    await userEvent.click(within(dialog).getByRole("button", { name: "Dispute with vendor" }));
    expect(await screen.findByText("Dispute with vendor: recorded. No payment was made.")).toBeInTheDocument();
    expect(document.querySelector('[data-status="disputed"]')).toHaveTextContent("Invoice disputed");
    expect(screen.getByTestId("invoice-exception")).toHaveTextContent("Reason: Rate 720 vs 700");
    expect(callsTo(fetchMock, `${BASE}/invoice-exception`, "POST")[0].json).toEqual({ action: "dispute", note: "Rate 720 vs 700" });
    // Disputed: only "record vendor response" and "reject" remain, plus re-matching.
    expect(screen.queryByRole("button", { name: "Dispute with vendor" })).toBeNull();
    expect(screen.getByRole("button", { name: "Record vendor response" })).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Match corrected invoice" }));
    const select = screen.getByLabelText("Invoice");
    expect(within(select).queryByRole("option", { name: /INV-5002/ })).toBeNull();   // current invoice excluded
    await userEvent.selectOptions(select, "INV-5001");
    await userEvent.click(screen.getByRole("button", { name: "Match invoice" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Match invoice" }));
    expect(await screen.findByText("Ready to simulate")).toBeInTheDocument();
    expect(screen.getByText("Invoice history (2)")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });

  it("shows server explanations for each decision with evidence", async () => {
    setup(view({ status: "reviewed", run_count: 1, workflow_result: RESULT, explanations: [
      { topic: "invoice", title: "Invoice check", outcome: "fail", summary: "Invoice INV-5002: mismatch.",
        factors: [{ label: "Delivery check", status: "fail", detail: "billed 100 vs delivered 95", evidence: ["policy:FIN-5.2"] }],
        limitations: ["3-way match."] },
      { topic: "policy", title: "Policy check", outcome: "not_run", summary: "The policy check didn't run.", factors: [], limitations: [] },
    ] }));
    const panel = await screen.findByTestId("explanations");
    expect(within(panel).getByText("Why? Invoice check")).toBeInTheDocument();
    expect(panel).toHaveTextContent("billed 100 vs delivered 95");
    expect(panel).toHaveTextContent("policy:FIN-5.2");
    expect(within(panel).getByText("Not run")).toBeInTheDocument();
  });

  it("does not offer invoice matching when the run already reconciled an invoice", async () => {
    setup(view({ status: "approved", run_count: 1, workflow_result: { ...RESULT, reconciliation: { invoice_id: "INV-5001", status: "match", checks: [] } } }));
    await screen.findByTestId("ticket-id");
    expect(screen.queryByRole("heading", { name: "Invoice after approval" })).toBeNull();
  });

  it("loads the saved audit history inside a disclosure", async () => {
    const { fetchMock } = setup(view({ run_count: 1, status: "reviewed", workflow_result: RESULT }), {
      [`GET ${BASE}/audit`]: () => ({ body: { ticket_id: TID, events: [{ sequence: 2, agent: "Critic", action: "review", status: "pass", detail: "Second" }, { sequence: 1, agent: "Intake", action: "parse", status: "pass", detail: "First" }] } }),
    });
    await screen.findByTestId("ticket-id");
    const details = screen.getByText("Saved audit history").closest("details");
    details.open = true;
    fireEvent(details, new Event("toggle"));
    expect(await screen.findByText("First")).toBeInTheDocument();
    const rows = within(details).getAllByRole("row").slice(1).map((r) => r.textContent);
    expect(rows[0]).toMatch(/First/);
    expect(callsTo(fetchMock, `${BASE}/audit`, "GET")).toHaveLength(1);
  });

  it("adds an admin note", async () => {
    const { fetchMock } = setup(view(), {
      [`POST ${BASE}/notes`]: (s) => { s.view = { ...s.view, notes: [{ author: "admin", role: "admin", text: "Checking", created_at: "2026-10-21T12:00:00" }] }; return { body: s.view }; },
    });
    await userEvent.type(await screen.findByLabelText("Add a note (visible to the requester)"), "Checking");
    await userEvent.click(screen.getByRole("button", { name: "Add note" }));
    expect(await screen.findByText("Note added.")).toBeInTheDocument();
    expect(callsTo(fetchMock, `${BASE}/notes`, "POST")[0].json).toEqual({ text: "Checking" });
  });

  it("shows an error with retry", async () => {
    let n = 0;
    renderWithApp(<AdminRequestDetailPage />, {
      role: "admin", route: `/admin/requests/${TID}`, path: "/admin/requests/:ticketId",
      routes: (m, url) => {
        if (url.pathname === BASE) { n += 1; return n === 1 ? { status: 404, body: { detail: "Ticket not found." } } : { body: view() }; }
        if (url.pathname === "/api/invoices") return { body: INVOICES };
        return undefined;
      },
    });
    expect(await screen.findByText("Ticket not found.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("ticket-id")).toHaveTextContent(TID);
  });
});
