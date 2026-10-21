import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import ApprovalDetailPage from "../ApprovalDetailPage";
import { callsTo, renderWithApp } from "../../test/utils";

const HASH = "a".repeat(64);
const VIEW = {
  approval_id: "APR-1", ticket_id: "REQ-100", request_version: 2, ticket_version: 2, status: "pending",
  requester_id: "alice", required_role: "approver", reason_codes: ["AMOUNT_OVER_THRESHOLD"],
  reasons: ["Total exceeds the auto-approval limit."], risk_level: "high", amount: 125000, currency: "INR",
  policy_version: "approval-policy-v1", workflow_snapshot_hash: HASH,
  review_snapshot: {
    ticket_id: "REQ-100", run_count: 2, final_status: "pending_approval", policy_set_version: 7,
    request: { date: "2026-10-23", attendees: 100, service: "catering", location: "Pune", budget_limit: 150000 },
    po: { po_id: "PO-9", vendor_id: "V-1", vendor_name: "Tasty Foods", subtotal: 100000, tax: 25000, total: 125000,
      currency: "INR", required_approver: "approver",
      line_items: [{ description: "Lunch buffet", quantity: 100, unit_price: 1000, line_total: 100000 }] },
  },
  created_at: "2026-10-20T10:00:00Z", expires_at: "2026-10-23T10:00:00Z", decisions: [],
  can_decide: true, cannot_decide_reason: null, stale_policy: false,
  safety_notice: "Approval records internal authorization only. No order or payment is executed.",
};
const PATH = "/api/approvals/APR-1";
const DECISION = `${PATH}/decision`;

function render(view = VIEW, extra = [], opts = {}) {
  return renderWithApp(<ApprovalDetailPage readOnly={opts.readOnly} />, {
    role: opts.role || "approver", route: "/approvals/APR-1", path: "/approvals/:approvalId",
    routes: [{ method: "GET", path: PATH, body: view }, ...extra],
  });
}

describe("ApprovalDetailPage", () => {
  it("shows request, draft PO, reasons, policy set and safety copy", async () => {
    const { container } = render();
    expect(await screen.findByRole("heading", { level: 1, name: "Approval for REQ-100" })).toBeInTheDocument();
    expect(screen.getByText("Total exceeds the auto-approval limit.")).toBeInTheDocument();
    expect(screen.getByText("Tasty Foods (V-1)")).toBeInTheDocument();
    expect(screen.getByText("Lunch buffet")).toBeInTheDocument();
    expect(screen.getByText("Version 7")).toBeInTheDocument();
    expect(screen.getByText(/A draft PO is not an order. Nothing is purchased./)).toBeInTheDocument();
    expect(screen.getAllByText(/Approval is internal authorization only. It does not send a PO or move money./).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/\bPaid\b|Awarded|PO sent/);
  });

  it("approve: confirm dialog posts DecisionIn with a stable idempotency key and shows the result only after success", async () => {
    let attempt = 0;
    const decided = { ...VIEW, status: "approved", can_decide: false, decided_at: "2026-10-21T10:00:00Z",
      decisions: [{ decision_id: "D1", action: "approve", actor_id: "carol", actor_role: "approver",
        decided_at: "2026-10-21T10:00:00Z", request_version: 2 }] };
    const { fetchMock } = render(VIEW, [{ method: "POST", path: DECISION, get status() { return attempt++ === 0 ? 500 : 200; },
      body: { approval: decided, replayed: false, reconciliation_required: false, message: "Approval recorded for internal authorization only." } }]);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.type(screen.getByLabelText("Comment (optional)"), "Looks fine");
    const submit = () => [...screen.getByRole("dialog").querySelectorAll("button")].find((b) => b.textContent === "Approve");
    expect(dialog).toBeInTheDocument();
    await userEvent.click(submit());
    // first attempt fails (500): dialog stays open, no result shown
    expect(await screen.findByText(/Something went wrong on the server/)).toBeInTheDocument();
    expect(screen.queryByText(/Recorded: Approved/)).not.toBeInTheDocument();
    await userEvent.click(submit());
    expect(await screen.findByText("Recorded: Approved")).toBeInTheDocument();
    const calls = callsTo(fetchMock, DECISION, "POST");
    expect(calls).toHaveLength(2);
    expect(calls[0].json).toEqual({ action: "approve", comment: "Looks fine", idempotency_key: calls[0].json.idempotency_key,
      request_version: 2, workflow_snapshot_hash: HASH });
    expect(calls[0].json.idempotency_key).toMatch(/^apr-/);
    expect(calls[1].json.idempotency_key).toBe(calls[0].json.idempotency_key);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Request changes" })).not.toBeInTheDocument();
  });

  it("reject requires a reason before submitting", async () => {
    const { fetchMock } = render(VIEW, [{ method: "POST", path: DECISION,
      body: { approval: { ...VIEW, status: "rejected", can_decide: false }, message: "Rejection recorded." } }]);
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));
    const dialog = screen.getByRole("dialog");
    const submit = [...dialog.querySelectorAll("button")].find((b) => b.textContent === "Reject");
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByLabelText("Reason for rejecting"), "Over budget");
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    expect(await screen.findByText("Recorded: Rejected")).toBeInTheDocument();
    expect(callsTo(fetchMock, DECISION, "POST")[0].json).toMatchObject({ action: "reject", comment: "Over budget" });
  });

  it("renders a 409 conflict message and reloads", async () => {
    const { fetchMock } = render(VIEW, [{ method: "POST", path: DECISION, status: 409,
      body: { detail: "The request has changed; reload and review the latest version." } }]);
    await userEvent.click(await screen.findByRole("button", { name: "Request changes" }));
    await userEvent.type(screen.getByLabelText("What needs to change"), "Fewer people");
    const dialog = screen.getByRole("dialog");
    await userEvent.click([...dialog.querySelectorAll("button")].find((b) => b.textContent === "Request changes"));
    expect(await screen.findByText("This request changed or was already decided")).toBeInTheDocument();
    expect(screen.getByText(/The request has changed; reload and review the latest version./)).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(callsTo(fetchMock, PATH, "GET").length).toBeGreaterThanOrEqual(2));
  });

  it("can_decide false shows the server reason and no actions; stale policy notice", async () => {
    render({ ...VIEW, can_decide: false, cannot_decide_reason: "You cannot decide your own request.", stale_policy: true });
    expect(await screen.findByText("You cannot decide your own request.")).toBeInTheDocument();
    expect(screen.getByText(/active policy set changed/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("admin read-only hides decision controls", async () => {
    render(VIEW, [], { role: "admin", readOnly: true });
    expect(await screen.findByText("View only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
  });

  it("not found renders a safe error", async () => {
    renderWithApp(<ApprovalDetailPage />, {
      role: "approver", route: "/approvals/APR-9", path: "/approvals/:approvalId",
      routes: [{ method: "GET", path: "/api/approvals/APR-9", status: 404, body: { detail: "Approval request not found." } }],
    });
    expect(await screen.findByText("Approval request not found.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("loads policy evidence when the disclosure is opened", async () => {
    render(VIEW, [{ method: "GET", path: "/api/requests/REQ-100/evidence", body: {
      evaluation: { outcome: "review_required", findings: [{ finding_id: "F1", status: "fail", severity: "review_required", summary: "Over catering cap" }],
        evidence: [{ evidence_id: "E1", filename: "policy.pdf", version: 1, page_number: 4, excerpt: "Catering is capped." }] } } }]);
    await userEvent.click(await screen.findByText("Policy evidence and citations"));
    expect(await screen.findByText(/Over catering cap/)).toBeInTheDocument();
    expect(screen.getByText("policy.pdf · v1 · p. 4")).toBeInTheDocument();
  });
});
