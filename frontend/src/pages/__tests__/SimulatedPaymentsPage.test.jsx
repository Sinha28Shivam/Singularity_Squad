import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import SimulatedPaymentsPage from "../SimulatedPaymentsPage";
import { callsTo, renderWithApp } from "../../test/utils";
import { FORBIDDEN } from "./r3helpers";

const BANNER = "Simulation only — no money is transferred.";
const TID = "REQ-20261020-1";
const PID = "psim_abcdef0123";
const ELIGIBLE = {
  mode: "SIMULATION", money_transferred: false, eligible: true, ticket_id: TID, invoice_id: "INV-5001",
  purchase_order_id: "DRAFT-PO-1", amount: "20200.00", currency: "INR", invoice_match_status: "match",
  approval_id: "APR-1", approval_status: "approved", approved_by: "carol", reasons: [], checked_at: "2026-10-21T10:00:00Z",
};
const PAYMENT = {
  mode: "SIMULATION", money_transferred: false, payment_id: PID, provider_reference: "PAY-SIM-ABC123", ticket_id: TID,
  invoice_id: "INV-5001", purchase_order_id: "DRAFT-PO-1", amount: "20200.00", currency: "INR", method: "SIMULATED_NEFT",
  status: "accepted_by_simulator", attempts: 1, approval_id: "APR-1", approval_request_version: 1, correlation_id: "corr-1",
  created_by: "admin", created_at: "2026-10-21T10:01:00Z", updated_at: "2026-10-21T10:01:00Z", audit_complete: true,
};
const EVENTS = { payment_id: PID, events: [
  { event_id: "ev1", event_type: "simulation_submitted", status: "submitted_to_simulator", actor: "admin", at: "2026-10-21T10:01:00Z", reason_codes: [] },
  { event_id: "ev2", event_type: "simulator_accepted", status: "accepted_by_simulator", actor: "simulator", at: "2026-10-21T10:01:01Z", reason_codes: [] },
] };

function routes({ elig = ELIGIBLE, payment = PAYMENT, extra = [] } = {}) {
  return [
    ...extra,
    { method: "GET", path: "/api/admin/tickets", body: { tickets: [{ ticket_id: TID, title: "Offsite lunch", status: "approved" }] } },
    { method: "GET", path: `/api/admin/tickets/${TID}`, body: { ticket_id: TID, status: "approved", workflow_result: { reconciliation: { invoice_id: "INV-5001" } } } },
    { method: "GET", path: "/api/payments/simulation/eligibility/INV-5001", body: elig },
    { method: "GET", path: `/api/payments/simulation/${PID}`, body: payment },
    { method: "GET", path: `/api/payments/simulation/${PID}/events`, body: EVENTS },
    { method: "GET", path: "/api/payments/simulation", body: { payments: [] } },
  ];
}
const render = (opts = {}) => renderWithApp(<SimulatedPaymentsPage />, { role: "admin", health: { payments_simulation_enabled: true }, ...opts });

async function pickTicket() {
  await userEvent.selectOptions(await screen.findByLabelText(/Approved request/), TID);
}

describe("SimulatedPaymentsPage", () => {
  it("shows the persistent banner and an eligible amount from the server", async () => {
    render({ routes: routes() });
    expect(screen.getByRole("heading", { level: 1, name: "Payments (Simulation)" })).toBeInTheDocument();
    expect(screen.getByText(BANNER)).toBeInTheDocument();
    await pickTicket();
    expect(await screen.findByText("₹20,200.00")).toBeInTheDocument();
    expect(screen.getByText("Eligible")).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });

  it("confirms with the exact amount, creates the simulation and shows the receipt", async () => {
    const { fetchMock } = render({ routes: routes({ extra: [{ method: "POST", path: "/api/payments/simulation", status: 201,
      body: { payment: PAYMENT, replayed: false, mode: "SIMULATION", money_transferred: false, message: "Simulation accepted. No money was transferred." } }] }) });
    await pickTicket();
    await userEvent.click(await screen.findByRole("button", { name: "Create simulated payment instruction" }));
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveTextContent("You are about to create a simulated payment instruction for ₹20,200.00 (INR)");
    expect(dialog).toHaveTextContent("No bank will be contacted and no money will be transferred.");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create simulation" }));
    const receipt = (await screen.findByRole("heading", { name: "Simulation receipt" })).closest("section");
    expect(within(receipt).getAllByText("Accepted by simulator").length).toBeGreaterThan(0);
    expect(within(receipt).getByText("PAY-SIM-ABC123")).toBeInTheDocument();
    expect(await within(receipt).findByText("Simulation submitted")).toBeInTheDocument();
    expect(within(receipt).getByText("Simulator accepted")).toBeInTheDocument();
    expect(within(receipt).queryByText(/Existing simulated payment returned/)).not.toBeInTheDocument();

    const body = callsTo(fetchMock, "/api/payments/simulation", "POST")[0].json;
    expect(body).toMatchObject({ ticket_id: TID, invoice_id: "INV-5001", purchase_order_id: "DRAFT-PO-1", amount: "20200.00",
      currency: "INR", simulation_only: true, confirmed_by_user: true });
    expect(body.idempotency_key).toMatch(/^[A-Za-z0-9_.:-]{8,128}$/);
    expect(document.body.textContent).not.toMatch(FORBIDDEN);
  });

  it("reuses the idempotency key per ticket+invoice and shows the replay notice", async () => {
    let n = 0;
    const { fetchMock } = render({ routes: routes({ extra: [{ method: "POST", path: "/api/payments/simulation",
      body: () => { n += 1; return { payment: PAYMENT, replayed: n > 1, message: "x" }; } }] }) });
    await pickTicket();
    for (let i = 0; i < 2; i += 1) {
      await userEvent.click(await screen.findByRole("button", { name: "Create simulated payment instruction" }));
      await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Create simulation" }));
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    }
    expect(await screen.findByText("Existing simulated payment returned")).toBeInTheDocument();
    const keys = callsTo(fetchMock, "/api/payments/simulation", "POST").map((c) => c.json.idempotency_key);
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
  });

  it("renders blocked reasons by code and offers no create button", async () => {
    render({ routes: routes({ elig: { ...ELIGIBLE, eligible: false, amount: null, reasons: [
      { code: "APPROVAL_STALE", message: "The approval no longer matches the current request." },
      { code: "INVOICE_PO_MISMATCH", message: "Invoice total differs from the draft PO." },
    ] } }) });
    await pickTicket();
    expect(await screen.findByText("Not eligible")).toBeInTheDocument();
    expect(screen.getByText("APPROVAL_STALE")).toBeInTheDocument();
    expect(screen.getByText(/approval no longer matches/)).toBeInTheDocument();
    expect(screen.getByText("INVOICE_PO_MISMATCH")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create simulated payment instruction" })).not.toBeInTheDocument();
  });

  it("renders an {error, message, reasons} body when create is refused and keeps the dialog open", async () => {
    render({ routes: routes({ extra: [{ method: "POST", path: "/api/payments/simulation", status: 409,
      body: { error: "NOT_ELIGIBLE", message: "Payment simulation is not allowed.", reasons: [{ code: "ALREADY_SIMULATED", message: "A simulation exists." }] } }] }) });
    await pickTicket();
    await userEvent.click(await screen.findByRole("button", { name: "Create simulated payment instruction" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create simulation" }));
    expect(await within(dialog).findByText("Payment simulation is not allowed.")).toBeInTheDocument();
    expect(within(dialog).getByText("ALREADY_SIMULATED")).toBeInTheDocument();
  });

  it("retries a failed simulation after confirmation", async () => {
    let retried = false;
    const failed = { ...PAYMENT, status: "failed_simulated", failure_code: "SIMULATED_PROVIDER_UNAVAILABLE", provider_reference: null };
    const { fetchMock } = render({ routes: [
      { method: "POST", path: `/api/payments/simulation/${PID}/retry`, body: () => { retried = true; return { payment: PAYMENT, replayed: false, message: "ok" }; } },
      { method: "GET", path: `/api/payments/simulation/${PID}`, body: () => (retried ? PAYMENT : failed) },
      ...routes({ elig: { ...ELIGIBLE, eligible: false, reasons: [{ code: "ALREADY_SIMULATED", message: "Exists." }], existing_payment_id: PID } }),
    ] });
    await pickTicket();
    await userEvent.click(await screen.findByRole("button", { name: "View existing simulated payment" }));
    expect((await screen.findAllByText("Simulation failed (retryable)")).length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Retry simulation" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Retry simulation" }));
    expect(await screen.findByText(/Retry submitted to the simulator/)).toBeInTheDocument();
    expect(callsTo(fetchMock, `/api/payments/simulation/${PID}/retry`, "POST")[0].json).toEqual({ confirmed_by_user: true });
    await waitFor(() => expect(screen.getAllByText("Accepted by simulator").length).toBeGreaterThan(0));
  });

  it("handles 503 SIMULATION_DISABLED gracefully", async () => {
    render({ routes: routes({ extra: [{ method: "GET", path: "/api/payments/simulation/eligibility/INV-5001", status: 503,
      body: { error: "SIMULATION_DISABLED", message: "Payment simulation is disabled.", reasons: [] } }] }) });
    await pickTicket();
    expect(await screen.findByText("Simulated payments are turned off")).toBeInTheDocument();
    expect(screen.getByText(/Payment simulation is disabled/)).toBeInTheDocument();
    expect(screen.getByText(BANNER)).toBeInTheDocument();
  });

  it("shows a disabled notice when the health flag is off", async () => {
    render({ health: { payments_simulation_enabled: false }, routes: routes() });
    expect(await screen.findByText("Simulated payments are turned off")).toBeInTheDocument();
    expect(screen.getByText(BANNER)).toBeInTheDocument();
  });

  it("shows empty and error+retry states for approved requests", async () => {
    let n = 0;
    render({ routes: [{ method: "GET", path: "/api/admin/tickets", status: 503, body: () => { n += 1; return { detail: "Database unavailable." }; } },
      { method: "GET", path: "/api/payments/simulation", body: { payments: [] } }] });
    expect(await screen.findByText("Database unavailable.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(n).toBe(2));
  });

  it("shows an empty state when no request is approved", async () => {
    render({ routes: [{ method: "GET", path: "/api/admin/tickets", body: { tickets: [] } },
      { method: "GET", path: "/api/payments/simulation", body: { payments: [] } }] });
    expect(await screen.findByText("No approved requests yet")).toBeInTheDocument();
  });
});
