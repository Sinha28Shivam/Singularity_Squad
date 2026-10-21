import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import App from "../App";
import { ApiError, detailFrom, friendlyMessage, request, setToken } from "../api/client";
import { navForRole } from "../routes";
import StatusBadge from "../components/StatusBadge";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider } from "../lib/auth";
import { mockFetch, USERS } from "./utils";

function renderApp(route = "/") {
  return render(<MemoryRouter initialEntries={[route]}><AuthProvider><App /></AuthProvider></MemoryRouter>);
}

describe("api client", () => {
  it("sends bearer token and maps errors to safe messages", async () => {
    setToken("tok-1");
    const f = mockFetch([{ method: "GET", path: "/api/x", status: 409, body: { detail: "Ticket changed." } }]);
    const err = await request("GET", "/api/x").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(409);
    expect(err.message).toBe("Ticket changed.");
    expect(f.mock.calls[0][1].headers.Authorization).toBe("Bearer tok-1");
  });
  it("never surfaces HTML or tracebacks", () => {
    expect(detailFrom("<html>Internal Server Error</html>")).toBe("");
    expect(friendlyMessage(500, "Traceback (most recent call last)")).toBe("Something went wrong on the server. Please try again.");
    expect(detailFrom({ detail: [{ loc: ["body", "attendees"], msg: "must be positive" }] })).toBe("attendees: must be positive");
  });
  it("network failure gives a retryable message", async () => {
    globalThis.fetch = () => Promise.reject(new TypeError("Failed to fetch"));
    const err = await request("GET", "/api/x").catch((e) => e);
    expect(err.status).toBe(0);
    expect(err.message).toMatch(/can't reach the server/i);
  });
});

describe("navigation by role", () => {
  it("hides payments unless enabled and gives approvers no admin pages", () => {
    const labels = (r, o) => navForRole(r, o).flatMap((s) => s.items.map((i) => i.label));
    expect(labels("admin")).not.toContain("Payments (Simulation)");
    expect(labels("admin", { paymentsEnabled: true })).toContain("Payments (Simulation)");
    expect(labels("approver")).toEqual(["Pending approvals", "Approval history", "Policy review"]);
    expect(labels("user")).toEqual(["My requests", "New request"]);
  });
});

describe("status badge", () => {
  it("always renders a text label", () => {
    render(<StatusBadge status="pending_approval" />);
    expect(screen.getByText("Waiting for approval")).toBeInTheDocument();
  });
});

describe("login flow", () => {
  it("signs in and lands on the role home page", async () => {
    mockFetch([
      { method: "POST", path: "/api/auth/login", body: { token: "t", expires_at: "2026-10-21T20:00:00Z", user: USERS.user } },
      { method: "GET", path: "/api/health", body: { status: "ok" } },
      { method: "GET", path: "/api/my/tickets", body: { tickets: [] } },
    ]);
    renderApp("/");
    await userEvent.type(await screen.findByLabelText(/username/i), "alice");
    await userEvent.type(screen.getByLabelText(/password/i), "user-pass-123");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(screen.getAllByText("My requests").length).toBeGreaterThan(0));
  });
  it("shows the server's safe error on bad credentials", async () => {
    mockFetch([{ method: "POST", path: "/api/auth/login", status: 401, body: { detail: "Invalid username or password." } }]);
    renderApp("/login");
    await userEvent.type(await screen.findByLabelText(/username/i), "x");
    await userEvent.type(screen.getByLabelText(/password/i), "y");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid username or password.");
  });
});
