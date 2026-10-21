import { configure, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import MyRequestsPage from "../MyRequestsPage";
import RequestQueuePage from "../RequestQueuePage";
import { renderWithApp } from "../../test/utils";

function callsTo(fetchMock, path, method) {
  return fetchMock.mock.calls
    .map(([input, init = {}]) => ({ url: new URL(typeof input === "string" ? input : input.url ?? String(input)), init }))
    .filter(({ url, init }) => url.pathname === path && (!method || (init.method || "GET").toUpperCase() === method));
}

// Search is debounced and pages render 50–120 rows; give async queries headroom when the full suite runs in parallel.
configure({ asyncUtilTimeout: 4000 });
vi.setConfig({ testTimeout: 20000 });

const FORBIDDEN = /\b(Paid|Awarded|PO sent)\b/;
const mk = (i, over = {}) => ({ ticket_id: `REQ-2026-${String(i).padStart(4, "0")}`, title: `Event ${i}`, status: "submitted",
  owner_name: "Alice Kumar", owner_username: "alice", created_at: "2026-10-21T09:00:00", ...over });

/** Fake server: filters by q (id/title) and status; pages with limit/offset; returns total + next_offset. */
function serverList(all, { counts } = {}) {
  return (url) => {
    const q = (url.searchParams.get("q") || "").toLowerCase();
    const status = url.searchParams.get("status");
    const limit = Number(url.searchParams.get("limit") || 50);
    const offset = Number(url.searchParams.get("offset") || 0);
    const hits = all.filter((t) => (!status || t.status === status) && (!q || `${t.ticket_id} ${t.title}`.toLowerCase().includes(q)));
    const page = hits.slice(offset, offset + limit);
    const next = offset + page.length < hits.length ? offset + page.length : null;
    return { tickets: page, total: hits.length, next_offset: next, ...(counts ? { counts } : {}) };
  };
}

describe("MyRequestsPage — server-side search and paging", () => {
  const ALL = Array.from({ length: 60 }, (_, i) => mk(i + 1, i === 59 ? { title: "Offsite retreat", status: "approved" } : {}));

  it("shows 'Showing N of total' and loads more via next_offset", async () => {
    const { fetchMock, container } = renderWithApp(<MyRequestsPage />, {
      routes: [{ method: "GET", path: "/api/my/tickets", body: serverList(ALL) }],
    });
    expect(await screen.findByText("Showing 50 of 60 requests.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Showing 60 of 60 requests.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    const calls = callsTo(fetchMock, "/api/my/tickets", "GET");
    expect(calls[1].url.searchParams.get("offset")).toBe("50");
    expect(calls[1].url.searchParams.get("limit")).toBe("50");
    expect(container.textContent).not.toMatch(FORBIDDEN);
  });

  it("sends a debounced q and the status filter to the server", async () => {
    const { fetchMock } = renderWithApp(<MyRequestsPage />, {
      routes: [{ method: "GET", path: "/api/my/tickets", body: serverList(ALL) }],
    });
    await screen.findByText("Showing 50 of 60 requests.");
    await userEvent.type(screen.getByLabelText("Search"), "offsite");
    expect(await screen.findByText("Offsite retreat")).toBeInTheDocument();
    expect(await screen.findByText("Showing 1 of 1 requests.")).toBeInTheDocument();
    const qCalls = callsTo(fetchMock, "/api/my/tickets", "GET").filter((c) => c.url.searchParams.get("q"));
    // Debounced: one request for the whole word, not one per keystroke.
    expect(qCalls.map((c) => c.url.searchParams.get("q"))).toEqual(["offsite"]);
    await userEvent.clear(screen.getByLabelText("Search"));
    await userEvent.selectOptions(screen.getByLabelText("Status"), "approved");
    await waitFor(() => {
      const last = callsTo(fetchMock, "/api/my/tickets", "GET").at(-1).url;
      expect(last.searchParams.get("status")).toBe("approved");
      expect(last.searchParams.get("q")).toBeNull();
    });
    expect(await screen.findByText("Showing 1 of 1 requests.")).toBeInTheDocument();
  });

  it("shows a 'no match' state for a server search with no hits", async () => {
    renderWithApp(<MyRequestsPage />, { routes: [{ method: "GET", path: "/api/my/tickets", body: serverList(ALL) }] });
    await screen.findByText("Showing 50 of 60 requests.");
    await userEvent.type(screen.getByLabelText("Search"), "zzz");
    expect(await screen.findByText("No requests match")).toBeInTheDocument();
  });

  it("shows a load-more error without losing loaded rows", async () => {
    renderWithApp(<MyRequestsPage />, {
      routes: (m, url) => {
        if (url.pathname !== "/api/my/tickets") return undefined;
        if (url.searchParams.get("offset")) return { status: 503, body: { detail: "Database unavailable." } };
        return { body: serverList(ALL)(url) };
      },
    });
    await screen.findByText("Showing 50 of 60 requests.");
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Database unavailable.")).toBeInTheDocument();
    expect(screen.getByText("REQ-2026-0001")).toBeInTheDocument();
  });
});

describe("RequestQueuePage — server-side search and paging", () => {
  const ALL = Array.from({ length: 120 }, (_, i) => mk(i + 1, i === 119 ? { title: "Board dinner", status: "pending_approval", owner_name: "Bob Iyer", owner_username: "bob" } : {}));
  const COUNTS = { submitted: 119, pending_approval: 1 };

  it("searches on the server, keeps global counts and pages with Load more", async () => {
    const { fetchMock } = renderWithApp(<RequestQueuePage />, {
      role: "admin", route: "/admin/requests",
      routes: [{ method: "GET", path: "/api/admin/tickets", body: serverList(ALL, { counts: COUNTS }) }],
    });
    expect(await screen.findByText("Showing 100 of 120 requests.")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Submitted (119)" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("Showing 120 of 120 requests.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Search"), "board");
    expect(await screen.findByText("Showing 1 of 1 requests.")).toBeInTheDocument();
    expect(screen.getByText("Board dinner")).toBeInTheDocument();
    expect(callsTo(fetchMock, "/api/admin/tickets", "GET").at(-1).url.searchParams.get("q")).toBe("board");
  });
});
