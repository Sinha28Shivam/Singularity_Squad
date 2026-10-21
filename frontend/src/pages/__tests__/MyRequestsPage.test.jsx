import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import MyRequestsPage from "../MyRequestsPage";
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
const TICKETS = [
  { ticket_id: "REQ-20261021-AAAA", title: "Team lunch Q4", event_category: "team_lunch", event_date: "2026-10-30", status: "needs_info", created_at: "2026-10-21T09:00:00" },
  { ticket_id: "REQ-20261021-BBBB", title: "Offsite", event_category: "offsite", event_date: "2026-11-10", status: "approved", created_at: "2026-10-20T09:00:00" },
];

describe("MyRequestsPage", () => {
  it("lists requests with ID, summary, date, status badge and plain next action", async () => {
    const { fetchMock, container } = renderWithApp(<MyRequestsPage />, {
      routes: [{ method: "GET", path: "/api/my/tickets", body: { tickets: TICKETS } }],
    });
    expect(await screen.findByText("REQ-20261021-AAAA")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "My requests" })).toBeInTheDocument();
    expect(screen.getByText("Team lunch Q4")).toBeInTheDocument();
    expect(screen.getByText("Needs more information", { selector: ".badge" })).toBeInTheDocument();
    expect(screen.getByText("Answer the questions and resubmit.")).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent))
      .toEqual(["Request ID", "Summary", "Submitted", "Status", "Next action"]);
    expect(screen.getByRole("link", { name: /New request/ })).toHaveAttribute("href", "/requests/new");
    expect(callsTo(fetchMock, "/api/my/tickets", "GET")[0].url.searchParams.get("limit")).toBe("50");
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it("filters by search and status on the loaded list", async () => {
    renderWithApp(<MyRequestsPage />, { route: "/requests", path: "/requests", routes: [{ method: "GET", path: "/api/my/tickets", body: { tickets: TICKETS } }] });
    await screen.findByText("REQ-20261021-AAAA");
    await userEvent.type(screen.getByLabelText("Search"), "offsite");
    expect(screen.queryByText("REQ-20261021-AAAA")).toBeNull();
    expect(screen.getByText("REQ-20261021-BBBB")).toBeInTheDocument();
    await userEvent.clear(screen.getByLabelText("Search"));
    await userEvent.selectOptions(screen.getByLabelText("Status"), "needs_info");
    expect(screen.getByText("REQ-20261021-AAAA")).toBeInTheDocument();
    expect(screen.queryByText("REQ-20261021-BBBB")).toBeNull();
  });

  it("navigates to the detail page when a row is clicked", async () => {
    renderWithApp(<MyRequestsPage />, { route: "/requests", path: "/requests", routes: [{ method: "GET", path: "/api/my/tickets", body: { tickets: TICKETS } }] });
    await userEvent.click(await screen.findByText("Offsite"));
    expect(await screen.findByText("navigated")).toBeInTheDocument();
  });

  it("shows the empty state with a Create a request action", async () => {
    renderWithApp(<MyRequestsPage />, { route: "/requests", path: "/requests", routes: [{ method: "GET", path: "/api/my/tickets", body: { tickets: [] } }] });
    expect(await screen.findByText("No requests yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create a request" })).toHaveAttribute("href", "/requests/new");
  });

  it("shows a safe error and retries", async () => {
    let n = 0;
    renderWithApp(<MyRequestsPage />, {
      routes: (m, url) => {
        if (m === "GET" && url.pathname === "/api/my/tickets") {
          n += 1;
          return n === 1 ? { status: 503, body: { detail: "Database unavailable." } } : { body: { tickets: TICKETS } };
        }
        return undefined;
      },
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Database unavailable.");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(screen.getByText("REQ-20261021-AAAA")).toBeInTheDocument());
  });
});
