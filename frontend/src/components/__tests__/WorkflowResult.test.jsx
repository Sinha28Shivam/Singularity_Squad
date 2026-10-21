import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import WorkflowResult, {
  ApprovalPanel, buildSteps, NotesList, PolicyEvidence, unresolvedItems,
} from "../WorkflowResult";

const FORBIDDEN = /\b(Paid|Awarded|PO sent)\b/;

const RESULT = {
  ticket_id: "REQ-1",
  persisted: true,
  request: { request_id: "REQ-1", category: "team_lunch", service: "lunch", date: "2026-10-23", attendees: 100, budget_limit: 100000, currency: "INR", location: "Bengaluru" },
  missing_fields: [],
  clarification_questions: [],
  policy: {
    summary: "Within the per-head limit.",
    checks: [{ name: "per_head_limit", status: "pass", detail: "₹650 ≤ ₹800", evidence: [{ source: "policy.md#3", detail: "Per-head cap" }] }],
    clauses: [{ clause_id: "CAT-3", title: "Per-head cap", text: "Lunch is capped at ₹800 per head.", source_file: "catering_policy.md", score: 0.91 }],
  },
  vendors: { selected: { vendor_id: "V-1", name: "Spice Route", rate_per_person: 650, capacity: 300 }, evaluations: [{ vendor_id: "V-1", name: "Spice Route", eligible: true, reasons: ["approved"], estimated_total: 65000 }], replanned: false, explanation: "Cheapest eligible vendor." },
  procurement: {
    po: { po_id: "PO-9", vendor_name: "Spice Route", vendor_id: "V-1", status: "draft", line_items: [{ description: "Lunch", quantity: 100, unit_price: 650, line_total: 65000 }], subtotal: 65000, tax: 3250, total: 68250, currency: "INR", required_approver: "Finance manager" },
    checks: [{ name: "budget", status: "warn", detail: "Uses 68% of budget." }],
    calculation_steps: ["100 × 650 = 65000", "GST 5% = 3250"],
  },
  reconciliation: null,
  critic: { status: "warning", summary: "One budget warning to review.", findings: [{ name: "budget_headroom", status: "warn", detail: "Low headroom" }] },
  audit_trail: [{ step: 1, agent: "Intake", action: "parse_request", status: "pass", detail: "Parsed", evidence: [], llm_used: false, timestamp: "2026-10-21T10:00:00Z" }],
  final_status: "warning",
  human_approval_required: true,
  disclaimer: "Prototype using synthetic data. Draft PO only - no purchase, approval, or payment is executed.",
};

describe("WorkflowResult", () => {
  it("shows the overall outcome first, then unresolved items, then readable steps", () => {
    const { container } = render(<WorkflowResult result={RESULT} ticketId="REQ-1" />);
    expect(screen.getByTestId("overall-outcome")).toHaveTextContent("Needs review");
    const headings = [...container.querySelectorAll("h3")].map((h) => h.textContent);
    expect(headings).toEqual(["Unresolved items", "Steps"]);
    expect(screen.getByText(/Budget: Uses 68% of budget/)).toBeInTheDocument();
    const steps = container.querySelectorAll("li.step");
    expect([...steps].map((s) => s.dataset.step)).toEqual(["intake", "policy", "vendors", "procurement", "invoice", "critic"]);
    expect(within(steps[1]).getByText("Policy check")).toBeInTheDocument();
    expect(within(steps[1]).getAllByText("Passed").length).toBeGreaterThan(0);
    expect(within(steps[3]).getByText("Cost & draft PO")).toBeInTheDocument();
    expect(within(steps[3]).getAllByText("Needs review").length).toBeGreaterThan(0);
    expect(within(steps[4]).getByText("Not run")).toBeInTheDocument();
    expect(screen.getByText(/Human approval required from: Finance manager/)).toBeInTheDocument();
  });

  it("hides evidence, citations, audit and raw data behind closed disclosures", () => {
    const { container } = render(<WorkflowResult result={RESULT} />);
    const clause = screen.getByText("Lunch is capped at ₹800 per head.");
    expect(clause.closest("details")).not.toBeNull();
    expect(clause.closest("details").open).toBe(false);
    const summaries = [...container.querySelectorAll("details > summary")].map((s) => s.textContent);
    expect(summaries).toEqual(expect.arrayContaining(["Audit trail for this run (1)", "Raw result data", "Policy citations (1)"]));
    expect(screen.getByText(/"final_status": "warning"/).closest("details")).not.toBeNull();
  });

  it("distinguishes extracted data from verified data and keeps the draft PO note", () => {
    render(<WorkflowResult result={RESULT} />);
    expect(screen.getByText("Extracted data")).toBeInTheDocument();
    expect(screen.getByText(/not independently verified/)).toBeInTheDocument();
    expect(screen.getByText("A draft PO is not an order. Nothing is purchased.")).toBeInTheDocument();
  });

  it("maps final statuses to Passed / Needs review / Blocked", () => {
    const { rerender } = render(<WorkflowResult result={{ ...RESULT, final_status: "pass" }} />);
    expect(screen.getByTestId("overall-outcome")).toHaveTextContent("Passed");
    rerender(<WorkflowResult result={{ ...RESULT, final_status: "blocked" }} />);
    expect(screen.getByTestId("overall-outcome")).toHaveTextContent("Blocked");
    rerender(<WorkflowResult result={{ ...RESULT, final_status: "needs_clarification", clarification_questions: ["What time?"] }} />);
    expect(screen.getByTestId("overall-outcome")).toHaveTextContent("Needs more information");
    expect(screen.getByText("What time?")).toBeInTheDocument();
  });

  it("renders an empty state when there is no result and warns when not persisted", () => {
    const { rerender } = render(<WorkflowResult result={null} />);
    expect(screen.getByText(/No workflow result yet/)).toBeInTheDocument();
    rerender(<WorkflowResult result={{ ...RESULT, persisted: false }} ticketId="REQ-1" />);
    expect(screen.getByText("Result not fully saved")).toBeInTheDocument();
    expect(screen.getByText(/Reference: REQ-1/)).toBeInTheDocument();
  });

  it("handles a minimal / partial result without crashing", () => {
    render(<WorkflowResult result={{ final_status: "blocked", vendors: { selected: null, evaluations: [] }, procurement: { po: null, checks: [] } }} />);
    expect(screen.getByText(/No eligible vendor was found\. A person/)).toBeInTheDocument();
    expect(unresolvedItems({ vendors: { selected: null }, procurement: { po: null } })).toEqual(["No eligible vendor was found.", "No draft PO was created."]);
    expect(buildSteps({}).every((s) => s.status === "not_run")).toBe(true);
  });

  it("shows the reconciliation outcome in the invoice step", () => {
    render(<WorkflowResult result={{ ...RESULT, reconciliation: { invoice_id: "INV-5002", status: "mismatch", checks: [{ name: "rate", status: "fail", detail: "Rate differs" }], expected_total: 68250, invoiced_total: 70000, variance: 1750, recommendation: "Hold for review." } }} />);
    expect(screen.getByText(/Invoice INV-5002: Mismatch\. Hold for review\./)).toBeInTheDocument();
  });

  it("never uses forbidden words", () => {
    const { container } = render(<WorkflowResult result={RESULT} />);
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });
});

describe("ApprovalPanel", () => {
  const TAV = {
    ticket_id: "REQ-1",
    current: { approval_id: "APR-1", request_version: 2, status: "pending", amount: 68250, currency: "INR", risk_level: "medium", required_role: "approver", reasons: ["Above ₹50,000"], created_at: "2026-10-21T10:00:00Z", expires_at: "2026-10-24T10:00:00Z", decisions: [], reconciliation_required: true },
    history: [
      { approval_id: "APR-0", request_version: 1, status: "changes_requested", decisions: [{ action: "request_changes", actor_id: "carol", actor_role: "approver", comment: "Fewer people", decided_at: "2026-10-20T10:00:00Z" }] },
    ],
  };
  it("shows who must decide and never offers approval controls to admins", () => {
    render(<ApprovalPanel approval={TAV} audience="admin" />);
    expect(screen.getByText("Pending review")).toBeInTheDocument();
    expect(screen.getByText("An eligible approver")).toBeInTheDocument();
    expect(screen.getByText(/Admins can't approve requests/)).toBeInTheDocument();
    expect(screen.getByText("Follow-up updates incomplete")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
    expect(screen.getByText("Approval is internal authorization only. It does not send a PO or move money.")).toBeInTheDocument();
    expect(screen.getByText(/Changes requested by carol \(approver\)/)).toBeInTheDocument();
  });
  it("hides admin-only flags for requesters and handles a missing approval", () => {
    const { rerender } = render(<ApprovalPanel approval={TAV} audience="requester" />);
    expect(screen.queryByText("Follow-up updates incomplete")).toBeNull();
    expect(screen.queryByText(/Admins can't approve/)).toBeNull();
    rerender(<ApprovalPanel approval={null} />);
    expect(screen.getByText(/No approval request/)).toBeInTheDocument();
  });
  it("accepts an embedded single approval object", () => {
    render(<ApprovalPanel approval={{ approval_id: "APR-9", status: "approved", request_version: 1 }} />);
    expect(screen.getAllByText("Approved").length).toBeGreaterThan(0);
  });
});

describe("PolicyEvidence and NotesList", () => {
  const EVAL = {
    outcome: "review_required",
    findings: [{ status: "fail", severity: "review_required", category: "alcohol", summary: "Alcohol needs VP sign-off", reason: "Policy 4.2", evidence_ids: ["e1"] }],
    evidence: [{ evidence_id: "e1", filename: "events.pdf", version: 2, page_number: 4, excerpt: "Alcohol requires VP approval." }],
    summary: "AI says review.",
  };
  it("full view shows findings with citations in a disclosure", () => {
    render(<PolicyEvidence evaluation={EVAL} />);
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    expect(screen.getByText("Alcohol needs VP sign-off")).toBeInTheDocument();
    expect(screen.getByText("Alcohol requires VP approval.").closest("details")).not.toBeNull();
  });
  it("redacted view never shows document text", () => {
    render(<PolicyEvidence evaluation={{ ...EVAL, redacted: true, citations: [{ filename: "events.pdf", version: 2 }] }} redacted />);
    expect(screen.queryByText("Alcohol requires VP approval.")).toBeNull();
    expect(screen.getByText(/Summary only/)).toBeInTheDocument();
  });
  it("renders notes with role labels and an empty state", () => {
    const { rerender } = render(<NotesList notes={[{ author: "admin", role: "admin", text: "On it", created_at: "2026-10-21T10:00:00Z" }]} />);
    expect(screen.getByText("Operations")).toBeInTheDocument();
    expect(screen.getByText("On it")).toBeInTheDocument();
    rerender(<NotesList notes={[]} />);
    expect(screen.getByText("No notes yet.")).toBeInTheDocument();
  });
});
