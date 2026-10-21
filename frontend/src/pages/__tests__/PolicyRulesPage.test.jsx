import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import PolicyRulesPage from "../PolicyRulesPage";
import { callsTo, renderWithApp } from "../../test/utils";

const FORBIDDEN = /\b(Paid|Awarded|PO sent)\b/;

const ACTIVE = { rule_id: "RULE-PO-LIMIT", rule_version: 1, name: "PO limit", status: "approved", severity: "review",
  source: "legacy_config", condition_text: "po.total > INR 100000", outcome: { decision: "review_required", reason_code: "PO_OVER_LIMIT", message: "Needs review" },
  created_by: "system:bootstrap", created_at: "2026-10-01T10:00:00Z", approved_at: "2026-10-01T10:00:00Z" };
const CAND = { rule_id: "RULE-CAT-CAP", rule_version: 2, name: "Catering cap", status: "candidate", severity: "block",
  source: "extracted", condition_text: "request.attendees > 200", outcome: { decision: "blocked", reason_code: "TOO_MANY", message: "Too many attendees" },
  created_by: "system:extractor", created_at: "2026-10-10T10:00:00Z",
  evidence_refs: [{ document_id: "doc_ready0001", document_version: 1, content_hash: "f".repeat(64), page_number: 4, excerpt: "Max 200 attendees." }] };
const REJ = { ...CAND, rule_id: "RULE-OLD", rule_version: 1, name: "Old rule", status: "rejected" };
const PSET = { active: true, policy_set_version: 7, rule_refs: ["RULE-PO-LIMIT@1"], created_at: "2026-10-01T10:00:00Z",
  created_by: "carol", reason: "approve RULE-PO-LIMIT@1", rules: [] };
const COUNTS = { approved: 1, candidate: 1, rejected: 1 };

function routes(extra = []) {
  return [
    ...extra,
    { method: "GET", path: "/api/policy-rules", body: (url) => {
      const rid = url.searchParams.get("rule_id");
      const status = url.searchParams.get("status");
      let rules = [ACTIVE, CAND, REJ];
      if (rid) rules = rules.filter((r) => r.rule_id === rid);
      if (status) rules = rules.filter((r) => r.status === status);
      return { rules, counts: COUNTS, active_policy_set_version: 7 };
    } },
    { method: "GET", path: "/api/policy-set/active", body: PSET },
    { method: "GET", path: "/api/policy-rules/RULE-CAT-CAP/versions/2", body: { ...CAND, active: false, events: [] } },
  ];
}

describe("PolicyRulesPage", () => {
  it("admin: shows active set banner, distinct text statuses and stale copy", async () => {
    const { container } = renderWithApp(<PolicyRulesPage />, { role: "admin", routes: routes() });
    expect(await screen.findByText("Active policy set: version 7")).toBeInTheDocument();
    expect(await screen.findByText("PO limit")).toBeInTheDocument();
    expect(within(screen.getByText("PO limit").closest("tr")).getByText("Active")).toBeInTheDocument();
    expect(within(screen.getByText("Catering cap").closest("tr")).getByText("Pending review")).toBeInTheDocument();
    expect(within(screen.getByText("Old rule").closest("tr")).getByText("Rejected")).toBeInTheDocument();
    expect(screen.getAllByText(/may become stale/).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "New rule" })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it("admin (not reviewer mode) sees no approve controls in rule detail", async () => {
    renderWithApp(<PolicyRulesPage />, { role: "admin", routes: routes() });
    await userEvent.click(await screen.findByText("Catering cap"));
    expect(await screen.findByRole("heading", { name: "Catering cap" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("reviewer: defaults to pending filter and approves with expected_policy_set_version after confirming", async () => {
    let approved = false;
    const { fetchMock } = renderWithApp(<PolicyRulesPage reviewer />, { role: "approver", routes: routes([
      { method: "POST", path: "/api/policy-rules/RULE-CAT-CAP/approve", body: () => { approved = true;
        return { rule: { ...CAND, status: "approved" }, policy_set_version: 8, replayed: false }; } },
    ]) });
    expect(await screen.findByRole("heading", { level: 1, name: "Policy review" })).toBeInTheDocument();
    expect(await screen.findByText("Catering cap")).toBeInTheDocument();
    expect(screen.queryByText("PO limit")).not.toBeInTheDocument();
    await userEvent.click(screen.getByText("Catering cap"));
    await userEvent.click(await screen.findByText("Source evidence (1)"));
    expect(screen.getByText("Max 200 attendees.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/may become stale/)).toBeInTheDocument();
    expect(approved).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve rule" }));
    expect(await screen.findByText("Rule approved and active")).toBeInTheDocument();
    expect(screen.getByText(/Active policy set is now version 8/)).toBeInTheDocument();
    expect(callsTo(fetchMock, "/api/policy-rules/RULE-CAT-CAP/approve", "POST")[0].json)
      .toEqual({ rule_version: 2, expected_policy_set_version: 7 });
  });

  it("reviewer: creator-cannot-approve 403 is shown in the dialog", async () => {
    renderWithApp(<PolicyRulesPage reviewer />, { role: "approver", routes: routes([
      { method: "POST", path: "/api/policy-rules/RULE-CAT-CAP/approve", status: 403,
        body: { detail: "The creator of a manual rule candidate cannot approve it; another reviewer must approve." } },
    ]) });
    await userEvent.click(await screen.findByText("Catering cap"));
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    const dialog = screen.getByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve rule" }));
    expect(await within(dialog).findByText(/creator of a manual rule candidate cannot approve it/)).toBeInTheDocument();
  });

  it("reviewer: 409 with conflicts is rendered", async () => {
    renderWithApp(<PolicyRulesPage reviewer />, { role: "approver", routes: routes([
      { method: "POST", path: "/api/policy-rules/RULE-CAT-CAP/approve", status: 409,
        body: { detail: "The rule has unresolved conflicts and cannot be approved.", conflicts: ["RULE-PO-LIMIT@1"] } },
    ]) });
    await userEvent.click(await screen.findByText("Catering cap"));
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Approve rule" }));
    expect(await screen.findByText("The rule has unresolved conflicts and cannot be approved.")).toBeInTheDocument();
    expect(screen.getByText("Conflict: RULE-PO-LIMIT@1")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("reviewer: reject requires a reason and posts it", async () => {
    const { fetchMock } = renderWithApp(<PolicyRulesPage reviewer />, { role: "approver", routes: routes([
      { method: "POST", path: "/api/policy-rules/RULE-CAT-CAP/reject", body: { ...CAND, status: "rejected" } },
    ]) });
    await userEvent.click(await screen.findByText("Catering cap"));
    await userEvent.click(await screen.findByRole("button", { name: "Reject" }));
    const dialog = screen.getByRole("dialog");
    const submit = within(dialog).getByRole("button", { name: "Reject rule" });
    expect(submit).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Reason for rejecting"), "Wrong threshold");
    await userEvent.click(submit);
    expect(await screen.findByText("Rule rejected")).toBeInTheDocument();
    expect(callsTo(fetchMock, "/api/policy-rules/RULE-CAT-CAP/reject", "POST")[0].json)
      .toEqual({ rule_version: 2, comment: "Wrong threshold" });
  });

  it("reviewer: validate shows server problems", async () => {
    renderWithApp(<PolicyRulesPage reviewer />, { role: "approver", routes: routes([
      { method: "POST", path: "/api/policy-rules/RULE-CAT-CAP/validate", body: { rule: { ...CAND, status: "needs_review" },
        errors: ["evidence[0]: content hash does not match document doc_ready0001"], conflicts: [] } },
    ]) });
    await userEvent.click(await screen.findByText("Catering cap"));
    await userEvent.click(await screen.findByRole("button", { name: "Validate" }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Validate" }));
    expect(await screen.findByText("Validation found problems")).toBeInTheDocument();
    expect(screen.getByText(/content hash does not match/)).toBeInTheDocument();
  });

  it("empty and error+retry states", async () => {
    let n = 0;
    renderWithApp(<PolicyRulesPage reviewer />, { role: "approver", routes: [
      { method: "GET", path: "/api/policy-rules", get status() { return n++ === 0 ? 500 : 200; }, body: { rules: [], counts: {} } },
      { method: "GET", path: "/api/policy-set/active", body: { active: false, policy_set_version: 0, rule_refs: [] } },
    ] });
    expect(await screen.findByText("No active policy set")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("No rules waiting for review")).toBeInTheDocument();
  });

  it("admin creates a rule candidate with evidence from a ready document", async () => {
    const { fetchMock } = renderWithApp(<PolicyRulesPage />, { role: "admin", routes: routes([
      { method: "GET", path: "/api/policy-documents", body: { documents: [{ document_id: "doc_ready0001", filename: "catering.pdf",
        version: 1, content_hash: "f".repeat(64), status: "ready" }], counts: {} } },
      { method: "POST", path: "/api/policy-rules", status: 201, body: { ...CAND, rule_id: "RULE-NEW-ONE", rule_version: 1, name: "Big events", created_by: "admin" } },
    ]) });
    await userEvent.click(await screen.findByRole("button", { name: "New rule" }));
    await userEvent.click(screen.getByRole("button", { name: "Create candidate" }));
    expect(await screen.findByText("Enter a name of at least 3 characters.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Rule name"), "Big events");
    await userEvent.type(screen.getByLabelText("Value"), "100000");
    await userEvent.type(screen.getByLabelText("Reason code"), "PO_BIG");
    await userEvent.type(screen.getByLabelText("Outcome message"), "Large spend needs review");
    await screen.findByRole("option", { name: "catering.pdf (v1)" });
    await userEvent.selectOptions(screen.getByLabelText("Source document"), "doc_ready0001");
    await userEvent.type(screen.getByLabelText("Page"), "4");
    await userEvent.click(screen.getByRole("button", { name: "Create candidate" }));
    expect(await screen.findByText("Candidate created")).toBeInTheDocument();
    const body = callsTo(fetchMock, "/api/policy-rules", "POST")[0].json;
    expect(body).toMatchObject({ name: "Big events", severity: "review",
      condition: { field: "po.total", op: "gt", value: "100000", value_type: "money", currency: "INR" },
      outcome: { decision: "review_required", reason_code: "PO_BIG", message: "Large spend needs review" },
      evidence_refs: [{ document_id: "doc_ready0001", document_version: 1, content_hash: "f".repeat(64), page_number: 4 }] });
  });
});
