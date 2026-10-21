/**
 * Test helpers.
 *
 * mockFetch(routes): replaces global fetch. `routes` is an array of
 *   { method: "GET", path: "/api/my/tickets" | RegExp, status=200, body, headers }
 * or a function (method, url:URL, init) => {status, body} | undefined.
 * Unmatched calls fail loudly (status 599) so tests never hit the network.
 * Returns the vi.fn so tests can assert calls: calls[i] = [url, init].
 *
 * renderWithApp(ui, { route, user }) renders inside MemoryRouter + a fake AuthContext provider.
 */
import { render } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";
import { AuthProvider } from "../lib/auth";
import { setToken } from "../api/client";
import { _resetHealthCache } from "../lib/health";

export const USERS = {
  user: { username: "alice", name: "Alice Kumar", email: "alice@example.com", role: "user" },
  admin: { username: "admin", name: "Ops Admin", email: "admin@example.com", role: "admin" },
  approver: { username: "carol", name: "Carol Rao", email: "carol@example.com", role: "approver" },
};

export function mockFetch(routes = []) {
  const fn = vi.fn(async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.href ?? input.url ?? String(input));
    const method = (init.method || "GET").toUpperCase();
    let hit;
    if (typeof routes === "function") hit = routes(method, url, init);
    else {
      hit = routes.find((r) => (r.method || "GET").toUpperCase() === method &&
        (r.path instanceof RegExp ? r.path.test(url.pathname) : r.path === url.pathname));
      if (hit && typeof hit.body === "function") hit = { ...hit, body: hit.body(url, init) };
    }
    if (!hit) {
      return new Response(JSON.stringify({ detail: `unmocked ${method} ${url.pathname}` }),
        { status: 599, headers: { "content-type": "application/json" } });
    }
    const status = hit.status ?? 200;
    const headers = { "content-type": "application/json", ...(hit.headers || {}) };
    const body = hit.raw !== undefined ? hit.raw : hit.body === undefined ? null : JSON.stringify(hit.body);
    return new Response(status === 204 ? null : body, { status, headers });
  });
  globalThis.fetch = fn;
  return fn;
}

/** Calls made to a path (string or RegExp), optionally filtered by method. Returns [{url, init, json}]. */
export function callsTo(fetchMock, path, method) {
  return fetchMock.mock.calls
    .map(([input, init = {}]) => ({ url: new URL(typeof input === "string" ? input : input.href ?? input.url), init }))
    .filter(({ url, init }) => (path instanceof RegExp ? path.test(url.pathname) : url.pathname === path) &&
      (!method || (init.method || "GET").toUpperCase() === method))
    .map((c) => ({ ...c, json: typeof c.init.body === "string" ? safeJson(c.init.body) : undefined }));
}
function safeJson(s) { try { return JSON.parse(s); } catch { return undefined; } }

/**
 * Render a page as a signed-in user. Provide the routes the page needs in `routes`; /api/auth/me and
 * /api/health are mocked automatically.
 */
export function renderWithApp(ui, { route = "/", path = "*", role = "user", user, routes = [], health = {} } = {}) {
  _resetHealthCache();
  const u = user || USERS[role];
  setToken("test-token");
  const extra = [
    { method: "GET", path: "/api/auth/me", body: u },
    { method: "GET", path: "/api/health", body: { status: "ok", payments_simulation_enabled: false, ...health } },
  ];
  const fetchMock = mockFetch(typeof routes === "function"
    ? (m, url, init) => routes(m, url, init) ?? extra.find((r) => r.method === m && r.path === url.pathname)
    : [...routes, ...extra]);
  const result = render(
    <MemoryRouter initialEntries={[route]}>
      <AuthProvider>
        <Routes><Route path={path} element={ui} /><Route path="*" element={<div>navigated</div>} /></Routes>
      </AuthProvider>
    </MemoryRouter>,
  );
  return { ...result, fetchMock, user: u };
}
