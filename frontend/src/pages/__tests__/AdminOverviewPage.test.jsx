import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import AdminOverviewPage from "../AdminOverviewPage";
import { renderWithApp } from "../../test/utils";

const FORBIDDEN = /\b(Paid|Awarded|PO sent)\b/;
const COUNTS = { submitted: 2, reviewed: 1, processing: 0, needs_info: 1, pending_approval: 3, changes_requested: 0, approval_expired: 1, approved: 4, rejected: 1, failed: 1, persistence_error: 0 };
const TICKETS = [
  { ticket_id: "REQ-1", title: "Lunch", owner_name: "Alice Kumar", status: "submitted" },
  { ticket_id: "REQ-2", title: "Offsite", owner_name: "Bob", status: "pending_approval" },
  { ticket_id: "REQ-3", title: "Town hall", owner_username: "dev", status: "failed" },
  { ticket_id: "REQ-4", title: "Party", owner_name: "Eve", status: "approved" },
];

describe("AdminOverviewPage", () => {
  it("shows counts derived only from the server counts and a needs-attention list", async () => {
    const { container } = renderWithApp(<AdminOverviewPage />, {
      role: "admin", routes: [{ method: "GET", path: "/api/admin/tickets", body: { tickets: TICKETS, counts: COUNTS } }],
    });
    expect(await screen.findByTestId("count-needs_review")).toHaveTextContent("3");
    expect(screen.getByTestId("count-in_progress")).toHaveTextContent("4");
    expect(screen.getByTestId("count-problems")).toHaveTextContent("2");
    expect(screen.getByTestId("count-completed")).toHaveTextContent("5");
    expect(screen.getByRole("heading", { name: "Needs attention" })).toBeInTheDocument();
    expect(screen.getByText("REQ-1")).toBeInTheDocument();
    expect(screen.getByText("REQ-3")).toBeInTheDocument();
    expect(screen.queryByText("REQ-2")).toBeNull();
    expect(screen.queryByText("REQ-4")).toBeNull();
    expect(screen.getByText("Run the workflow")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it("does not invent metrics when counts are missing, and shows the empty list", async () => {
    renderWithApp(<AdminOverviewPage />, { role: "admin", route: "/admin", path: "/admin", routes: [{ method: "GET", path: "/api/admin/tickets", body: { tickets: [] } }] });
    expect(await screen.findByText(/Request counts aren't available/)).toBeInTheDocument();
    expect(screen.queryByTestId("count-needs_review")).toBeNull();
    expect(screen.getByText("Nothing needs attention")).toBeInTheDocument();
  });

  it("opens a request from the attention list", async () => {
    renderWithApp(<AdminOverviewPage />, { role: "admin", route: "/admin", path: "/admin", routes: [{ method: "GET", path: "/api/admin/tickets", body: { tickets: TICKETS, counts: COUNTS } }] });
    await userEvent.click(await screen.findByText("REQ-1"));
    expect(await screen.findByText("navigated")).toBeInTheDocument();
  });

  it("shows an error with retry", async () => {
    let n = 0;
    renderWithApp(<AdminOverviewPage />, {
      role: "admin",
      routes: (m, url) => {
        if (url.pathname === "/api/admin/tickets") { n += 1; return n === 1 ? { status: 500, body: { detail: "Traceback boom" } } : { body: { tickets: TICKETS, counts: COUNTS } }; }
        return undefined;
      },
    });
    expect(await screen.findByText("Something went wrong on the server. Please try again.")).toBeInTheDocument();
    expect(screen.queryByText(/Traceback/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("count-needs_review")).toHaveTextContent("3");
  });
});
