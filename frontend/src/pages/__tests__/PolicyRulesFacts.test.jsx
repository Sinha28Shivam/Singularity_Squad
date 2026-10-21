import { configure, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import PolicyRulesPage from "../PolicyRulesPage";
import { callsTo, renderWithApp } from "../../test/utils";

// The create-rule flow types into many fields; allow for a busy parallel test run.
configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 20000 });

const DOCS = { method: "GET", path: "/api/policy-documents", body: { documents: [{ document_id: "doc_ready0001", filename: "catering.pdf",
  version: 1, content_hash: "f".repeat(64), status: "ready" }], counts: {} } };
const BASE = [
  { method: "GET", path: "/api/policy-rules", body: { rules: [], counts: {}, active_policy_set_version: 1 } },
  { method: "GET", path: "/api/policy-set/active", body: { active: true, policy_set_version: 1, rule_refs: [], rules: [] } },
  DOCS,
];
const FACTS = {
  facts: [
    { name: "request.attendees", type: "integer", description: "Number of attendees (normalized request, else submission)." },
    { name: "po.total", type: "money", description: "Draft PO total.", currency: "INR" },
    { name: "rate.applicable", type: "boolean", description: "A verified rate card applies." },
  ],
  operators: ["eq", "gt", "between", "exists"],
};

async function openForm() {
  await userEvent.click(await screen.findByRole("button", { name: "New rule" }));
  return screen.getByRole("form", { name: "New rule candidate" });
}

describe("PolicyRulesPage — facts and operators from the server", () => {
  it("offers server facts/operators, shows the fact description and sets the value type", async () => {
    const { fetchMock } = renderWithApp(<PolicyRulesPage />, { role: "admin", routes: [
      ...BASE,
      { method: "GET", path: "/api/policy-rules/facts", body: FACTS },
      { method: "POST", path: "/api/policy-rules", status: 201, body: { rule_id: "RULE-NEW", rule_version: 1, name: "Big group", status: "candidate" } },
    ] });
    const form = await openForm();
    await within(form).findByRole("option", { name: "is between" });
    const ops = within(within(form).getByLabelText("Condition")).getAllByRole("option").map((o) => o.value);
    expect(ops).toEqual(["eq", "gt", "between", "exists"]);
    const datalist = form.querySelector("#rule-facts");
    expect([...datalist.querySelectorAll("option")].map((o) => o.value)).toEqual(["request.attendees", "po.total", "rate.applicable"]);

    const fact = within(form).getByLabelText("Fact");
    await userEvent.clear(fact);
    await userEvent.type(fact, "request.attendees");
    expect(within(form).getByText("Number of attendees (normalized request, else submission). (Integer)")).toBeInTheDocument();
    expect(within(form).getByLabelText("Value type")).toHaveValue("integer");

    await userEvent.selectOptions(within(form).getByLabelText("Condition"), "between");
    await userEvent.type(within(form).getByLabelText("Rule name"), "Big group");
    await userEvent.type(within(form).getByLabelText("Value"), "100");
    await userEvent.type(within(form).getByLabelText("Reason code"), "BIG_GROUP");
    await userEvent.type(within(form).getByLabelText("Outcome message"), "Big groups need review");
    await within(form).findByRole("option", { name: "catering.pdf (v1)" });
    await userEvent.selectOptions(within(form).getByLabelText("Source document"), "doc_ready0001");
    await userEvent.click(within(form).getByRole("button", { name: "Create candidate" }));
    expect(await within(form).findByText(/Enter exactly two values/)).toBeInTheDocument();
    await userEvent.type(within(form).getByLabelText("Value"), ", 300");
    await userEvent.click(within(form).getByRole("button", { name: "Create candidate" }));
    expect(await screen.findByText("Candidate created")).toBeInTheDocument();
    expect(callsTo(fetchMock, "/api/policy-rules", "POST")[0].json.condition)
      .toEqual({ field: "request.attendees", op: "between", value_type: "integer", value: [100, 300] });
  });

  it("warns about unknown facts when the server list is available", async () => {
    renderWithApp(<PolicyRulesPage />, { role: "admin", routes: [...BASE, { method: "GET", path: "/api/policy-rules/facts", body: FACTS }] });
    const form = await openForm();
    await within(form).findByRole("option", { name: "is between" });
    const fact = within(form).getByLabelText("Fact");
    await userEvent.clear(fact);
    await userEvent.type(fact, "po.discount");
    expect(within(form).getByText(/Not a known fact/)).toBeInTheDocument();
  });

  it("falls back to the built-in fact and operator lists when the endpoint 404s", async () => {
    renderWithApp(<PolicyRulesPage />, { role: "admin", routes: [
      ...BASE, { method: "GET", path: "/api/policy-rules/facts", status: 404, body: { detail: "Not Found" } },
    ] });
    const form = await openForm();
    const ops = within(within(form).getByLabelText("Condition")).getAllByRole("option").map((o) => o.value);
    expect(ops).toContain("date_after");
    expect(ops).not.toContain("between");
    const datalist = form.querySelector("#rule-facts");
    expect([...datalist.querySelectorAll("option")].map((o) => o.value)).toContain("po.total");
  });
});
