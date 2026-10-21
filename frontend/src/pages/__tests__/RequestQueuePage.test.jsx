import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import RequestQueuePage from "../RequestQueuePage";
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
  { ticket_id: "REQ-1", title: "Lunch", owner_name: "Alice Kumar", owner_username: "alice", status: "submitted", created_at: "2026-10-21T09:00:00", event_date: "2026-10-30" },
  { ticket_id: "REQ-2", title: "Offsite", owner_name: "Bob Iyer", owner_username: "bob", status: "pending_approval", created_at: "2026-10-20T09:00:00" },
];
const COUNTS = { submitted: 1, pending_approval: 1 };

describe("RequestQueuePage", () => {
  it("lists requests with requester, summary, status and action", async () => {
    const { container } = renderWithApp(<RequestQueuePage />, {
      role: "admin", route: "/admin/requests", routes: [{ method: "GET", path: "/api/admin/tickets", body: { tickets: TICKETS, counts: COUNTS } }],
    });
    expect(await screen.findByText("REQ-1")).toBeInTheDocument();
    expect(screen.getAllByRole("columnheader").map((h) => h.textContent))
      .toEqual(["Request ID", "Requester", "Summary", "Submitted", "Status", "Action"]);
    expect(screen.getByText("Alice Kumar")).toBeInTheDocument();
    expect(screen.getByText("Waiting for an approver")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Submitted (1)" })).toBeInTheDocument();
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it("sends the status filter to the server and searches the loaded list client-side", async () => {
    const { fetchMock } = renderWithApp(<RequestQueuePage />, {
      role: "admin", route: "/admin/requests",
      routes: [{ method: "GET", path: "/api/admin/tickets", body: (url) => ({ tickets: url.searchParams.get("status") ? TICKETS.filter((t) => t.status === url.searchParams.get("status")) : TICKETS, counts: COUNTS }) }],
    });
    await screen.findByText("REQ-1");
    await userEvent.type(screen.getByLabelText("Search"), "bob");
    expect(screen.queryByText("REQ-1")).toBeNull();
    expect(screen.getByText("REQ-2")).toBeInTheDocument();
    expect(callsTo(fetchMock, "/api/admin/tickets", "GET")).toHaveLength(1);
    await userEvent.clear(screen.getByLabelText("Search"));
    await userEvent.selectOptions(screen.getByLabelText("Status"), "submitted");
    await waitFor(() => expect(screen.queryByText("REQ-2")).toBeNull());
    const calls = callsTo(fetchMock, "/api/admin/tickets", "GET");
    expect(calls[calls.length - 1].url.searchParams.get("status")).toBe("submitted");
  });

  it("shows a filtered empty state", async () => {
    renderWithApp(<RequestQueuePage />, { role: "admin", route: "/admin/requests", routes: [{ method: "GET", path: "/api/admin/tickets", body: { tickets: TICKETS, counts: COUNTS } }] });
    await screen.findByText("REQ-1");
    await userEvent.type(screen.getByLabelText("Search"), "zzz");
    expect(screen.getByText("No requests match")).toBeInTheDocument();
  });

  it("shows the empty state when there are no requests", async () => {
    renderWithApp(<RequestQueuePage />, { role: "admin", route: "/admin/requests", routes: [{ method: "GET", path: "/api/admin/tickets", body: { tickets: [], counts: {} } }] });
    expect(await screen.findByText("No requests yet")).toBeInTheDocument();
  });

  it("shows an error with retry", async () => {
    let n = 0;
    renderWithApp(<RequestQueuePage />, {
      role: "admin", route: "/admin/requests",
      routes: (m, url) => {
        if (url.pathname === "/api/admin/tickets") { n += 1; return n === 1 ? { status: 503, body: { detail: "Database unavailable." } } : { body: { tickets: TICKETS, counts: COUNTS } }; }
        return undefined;
      },
    });
    expect(await screen.findByText("Database unavailable.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("REQ-1")).toBeInTheDocument();
  });
});
