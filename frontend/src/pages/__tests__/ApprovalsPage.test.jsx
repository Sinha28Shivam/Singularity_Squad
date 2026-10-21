import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import ApprovalsPage from "../ApprovalsPage";
import { callsTo, renderWithApp } from "../../test/utils";

const ITEM = {
  approval_id: "APR-1", ticket_id: "REQ-100", request_version: 1, status: "pending", requester_id: "alice",
  summary: "Team lunch · 100 attendees", amount: 125000.5, currency: "INR", risk_level: "high",
  reason_codes: ["AMOUNT_OVER_THRESHOLD"], created_at: "2026-10-20T10:00:00Z", expires_at: "2026-10-23T10:00:00Z",
  can_decide: true,
};
const DECIDED = { ...ITEM, approval_id: "APR-2", ticket_id: "REQ-200", status: "approved" };
const COUNTS = { pending: 1, approved: 1 };

function noForbidden(container) {
  expect(container.textContent).not.toMatch(/\bPaid\b|Awarded|PO sent/);
}

describe("ApprovalsPage", () => {
  it("lists pending approvals with server amount, plain reasons and status, and opens detail", async () => {
    const { fetchMock, container } = renderWithApp(<ApprovalsPage mode="pending" />, {
      role: "approver", route: "/approvals", path: "/approvals",
      routes: [{ method: "GET", path: "/api/approvals", body: { approvals: [ITEM], counts: COUNTS } }],
    });
    expect(await screen.findByText("REQ-100")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Pending approvals" })).toBeInTheDocument();
    const row = screen.getByText("REQ-100").closest("tr");
    expect(within(row).getByText(/1,25,000\.50/)).toBeInTheDocument();
    expect(within(row).getByText("Amount over threshold")).toBeInTheDocument();
    expect(within(row).getByText("High")).toBeInTheDocument();
    expect(within(row).getByText("Pending review")).toBeInTheDocument();
    expect(screen.getByText(/Approval is internal authorization only/)).toBeInTheDocument();
    const call = callsTo(fetchMock, "/api/approvals", "GET")[0];
    expect(call.url.searchParams.get("status")).toBe("pending");
    noForbidden(container);
    await userEvent.click(row);
    expect(await screen.findByText("navigated")).toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    renderWithApp(<ApprovalsPage mode="pending" />, {
      role: "approver", routes: [{ method: "GET", path: "/api/approvals", body: { approvals: [], counts: {} } }],
    });
    expect(await screen.findByText("Nothing waiting for approval")).toBeInTheDocument();
  });

  it("shows an error with retry", async () => {
    let n = 0;
    renderWithApp(<ApprovalsPage mode="pending" />, {
      role: "approver",
      routes: [{ method: "GET", path: "/api/approvals", get status() { return n++ === 0 ? 503 : 200; },
        body: { approvals: [ITEM], counts: COUNTS } }],
    });
    expect(await screen.findByRole("alert")).toHaveTextContent(/temporarily unavailable/i);
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("REQ-100")).toBeInTheDocument();
  });

  it("history mode shows decided requests and filters by status", async () => {
    const { fetchMock } = renderWithApp(<ApprovalsPage mode="history" />, {
      role: "approver",
      routes: [{ method: "GET", path: "/api/approvals", body: { approvals: [ITEM, DECIDED], counts: COUNTS } }],
    });
    expect(await screen.findByText("REQ-200")).toBeInTheDocument();
    expect(screen.queryByText("REQ-100")).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Status"), "rejected");
    await waitFor(() => {
      const calls = callsTo(fetchMock, "/api/approvals", "GET");
      expect(calls[calls.length - 1].url.searchParams.get("status")).toBe("rejected");
    });
  });

  it("admin read-only queue states view only", async () => {
    renderWithApp(<ApprovalsPage mode="pending" readOnly />, {
      role: "admin", routes: [{ method: "GET", path: "/api/approvals", body: { approvals: [ITEM], counts: COUNTS } }],
    });
    expect(await screen.findByText("REQ-100")).toBeInTheDocument();
    expect(screen.getByText(/View only/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });
});
