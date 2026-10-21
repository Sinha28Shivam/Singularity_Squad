import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import NewRequestPage from "../NewRequestPage";
import { renderWithApp } from "../../test/utils";

// Local copy of test/utils callsTo that also accepts URL objects (the shared helper reads `input.url`, which URL lacks).
function callsTo(fetchMock, path, method) {
  return fetchMock.mock.calls
    .map(([input, init = {}]) => ({ url: new URL(typeof input === "string" ? input : input.url ?? String(input)), init }))
    .filter(({ url, init }) => (path instanceof RegExp ? path.test(url.pathname) : url.pathname === path) &&
      (!method || (init.method || "GET").toUpperCase() === method))
    .map((c) => ({ ...c, json: typeof c.init.body === "string" ? JSON.parse(c.init.body) : undefined }));
}
import { todayIso, validateForm, EMPTY_FORM } from "../../api/requests";

const FORBIDDEN = /\b(Paid|Awarded|PO sent)\b/;

function futureDate(days = 30) {
  const d = new Date(); d.setDate(d.getDate() + days); return todayIso(d);
}

async function fillValid() {
  await userEvent.type(screen.getByLabelText(/^Summary/), "Quarterly team lunch");
  await userEvent.selectOptions(screen.getByLabelText(/^Event type/), "celebration");
  await userEvent.selectOptions(screen.getByLabelText(/^Catering service/), "dinner");
  await userEvent.type(screen.getByLabelText(/^Event date/), futureDate());
  await userEvent.type(screen.getByLabelText(/^Location/), "Bengaluru office");
  await userEvent.type(screen.getByLabelText(/^Attendees/), "120");
  await userEvent.type(screen.getByLabelText(/^Budget/), "150000");
  await userEvent.type(screen.getByLabelText(/^Notes/), "Vegetarian options please");
}

describe("NewRequestPage", () => {
  it("groups fields by meaning, marks required ones and says it doesn't place an order", () => {
    const { container } = renderWithApp(<NewRequestPage />);
    expect([...container.querySelectorAll("legend")].map((l) => l.textContent))
      .toEqual(["What is needed?", "When and where?", "Budget and quantity", "Additional context"]);
    for (const name of ["Summary", "Event type", "Catering service", "Event date", "Attendees", "Budget (INR)"]) {
      expect(screen.getByText(name, { selector: "label" })).toHaveClass("required");
    }
    expect(screen.getByText("Location", { selector: "label" })).not.toHaveClass("required");
    expect(screen.getByText("Submitting a request doesn't place an order.")).toBeInTheDocument();
    expect(screen.getByLabelText("Use approved vendors only")).toBeChecked();
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it("validates on the client and does not call the API when invalid", async () => {
    const { fetchMock } = renderWithApp(<NewRequestPage />);
    await userEvent.type(screen.getByLabelText(/^Summary/), "ab");
    await userEvent.type(screen.getByLabelText(/^Attendees/), "0");
    await userEvent.click(screen.getByRole("button", { name: "Submit request" }));
    expect(screen.getByText("Enter a short summary of at least 3 characters.")).toBeInTheDocument();
    expect(screen.getByText("Choose the event date.")).toBeInTheDocument();
    expect(screen.getByText("Attendees must be a whole number from 1 to 5,000.")).toBeInTheDocument();
    expect(screen.getByText("Enter the budget.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Summary/)).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText(/^Summary/)).toHaveValue("ab");
    expect(callsTo(fetchMock, "/api/my/tickets", "POST")).toHaveLength(0);
  });

  it("validateForm rejects past dates and out-of-range numbers", () => {
    const base = { ...EMPTY_FORM, title: "Lunch", event_date: "2026-10-30", attendees: "10", budget_inr: "1000" };
    expect(validateForm(base, { today: "2026-10-21" })).toEqual({});
    expect(validateForm({ ...base, event_date: "2026-10-01" }, { today: "2026-10-21" }).event_date).toMatch(/past/);
    expect(validateForm({ ...base, attendees: "5001" }).attendees).toBeTruthy();
    expect(validateForm({ ...base, attendees: "2.5" }).attendees).toBeTruthy();
    expect(validateForm({ ...base, budget_inr: "-5" }).budget_inr).toMatch(/greater than zero/);
    expect(validateForm({ ...base, budget_inr: "20000000" }).budget_inr).toBeTruthy();
  });

  it("submits a TicketSubmission and confirms the created ID with a link", async () => {
    const { fetchMock } = renderWithApp(<NewRequestPage />, {
      routes: [{ method: "POST", path: "/api/my/tickets", status: 201, body: { ticket_id: "REQ-20261021-NEW1", status: "submitted", submission: {}, outcome: "Waiting" } }],
    });
    await fillValid();
    await userEvent.click(screen.getByRole("button", { name: "Submit request" }));
    expect(await screen.findByText("Request submitted")).toBeInTheDocument();
    expect(screen.getByTestId("created-id")).toHaveTextContent("REQ-20261021-NEW1");
    expect(screen.getByRole("link", { name: "View request REQ-20261021-NEW1" })).toHaveAttribute("href", "/requests/REQ-20261021-NEW1");
    expect(screen.getByText("Submitting a request doesn't place an order.")).toBeInTheDocument();
    const [call] = callsTo(fetchMock, "/api/my/tickets", "POST");
    expect(call.json).toEqual({
      title: "Quarterly team lunch", event_category: "celebration", service: "dinner", event_date: futureDate(),
      attendees: 120, budget_inr: 150000, location: "Bengaluru office", approved_vendor_only: true, notes: "Vegetarian options please",
    });
  });

  it("keeps entered values and shows the server message after an error", async () => {
    renderWithApp(<NewRequestPage />, {
      routes: [{ method: "POST", path: "/api/my/tickets", status: 422, body: { detail: [{ loc: ["body", "budget_inr"], msg: "too high" }] } }],
    });
    await fillValid();
    await userEvent.click(screen.getByRole("button", { name: "Submit request" }));
    expect(await screen.findByText("Please check the form: budget_inr: too high")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Summary/)).toHaveValue("Quarterly team lunch");
    expect(screen.getByLabelText(/^Attendees/)).toHaveValue(120);
    expect(screen.getByLabelText(/^Notes/)).toHaveValue("Vegetarian options please");
    expect(screen.getByRole("button", { name: "Submit request" })).toBeEnabled();
  });

  it("disables submit while pending", async () => {
    let release;
    renderWithApp(<NewRequestPage />);
    globalThis.fetch = () => new Promise((resolve) => {
      release = () => resolve(new Response(JSON.stringify({ ticket_id: "REQ-X", status: "submitted" }), { status: 201, headers: { "content-type": "application/json" } }));
    });
    await fillValid();
    await userEvent.click(screen.getByRole("button", { name: "Submit request" }));
    expect(screen.getByRole("button", { name: "Submitting…" })).toBeDisabled();
    release();
    await waitFor(() => expect(screen.getByText("Request submitted")).toBeInTheDocument());
  });
});
