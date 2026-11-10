import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import InboxPage from "../InboxPage";
import { renderWithApp } from "../../test/utils";

const ITEMS = [
  { kind: "resolve_invoice", priority: 1, ticket_id: "REQ-1", title: "Lunch", label: "Resolve the invoice mismatch",
    detail: "The invoice did not match.", link: "/admin/requests/REQ-1", since: "2026-10-20T10:00:00Z", stalled: true },
  { kind: "simulate_payment", priority: 2, ticket_id: "REQ-2", title: null, label: "Ready to simulate payment",
    detail: "Invoice matched.", link: "/admin/payments", since: null, stalled: false },
];

describe("InboxPage", () => {
  it("lists server-derived actions with priority, stalled flag and links", async () => {
    renderWithApp(<InboxPage />, { role: "admin", routes: [
      { method: "GET", path: "/api/inbox", body: { role: "admin", items: ITEMS, total: 2, stalled: 1, stall_hours: 24, counts: {} } },
    ] });
    const rows = await screen.findAllByTestId("inbox-item");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("Resolve the invoice mismatch");
    expect(within(rows[0]).getByText("Stalled")).toBeInTheDocument();
    expect(within(rows[0]).getByText("High")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("link", { name: "Open" })).toHaveAttribute("href", "/admin/requests/REQ-1");
    expect(within(rows[1]).queryByText("Stalled")).toBeNull();
    expect(screen.getByText(/1 waiting longer than 24 hours/)).toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    renderWithApp(<InboxPage />, { role: "approver", routes: [
      { method: "GET", path: "/api/inbox", body: { role: "approver", items: [], total: 0, stalled: 0, stall_hours: 24 } },
    ] });
    expect(await screen.findByText("You're all caught up")).toBeInTheDocument();
  });
});
