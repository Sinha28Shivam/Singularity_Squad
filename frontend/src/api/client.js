/**
 * Shared API client for the FastAPI backend.
 *
 * - Bearer token auth (POST /api/auth/login returns {token, expires_at, user}). The token lives in
 *   sessionStorage only (cleared when the tab closes); it is never logged.
 * - All errors become ApiError with a safe, user-facing message. Raw server internals are never shown.
 * - The backend is authoritative for every permission and business rule; the client only displays results.
 */
export const API_BASE = (import.meta.env.VITE_API_BASE_URL || "http://localhost:8000").replace(/\/+$/, "");
const TOKEN_KEY = "workplaceops.token";
const DEFAULT_TIMEOUT_MS = 30000;

let unauthorizedHandler = null;
export function onUnauthorized(fn) { unauthorizedHandler = fn; }

export function getToken() {
  try { return sessionStorage.getItem(TOKEN_KEY); } catch { return null; }
}
export function setToken(token) {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch { /* storage unavailable */ }
}

export class ApiError extends Error {
  constructor(status, message, detail, body) {
    super(message);
    this.name = "ApiError";
    this.status = status;        // HTTP status, 0 for network/timeout
    this.detail = detail;        // server `detail` (string) when safe
    this.body = body;            // parsed JSON body (e.g. {error, reasons} for payments)
  }
}

/** Extract a short, safe message from a FastAPI error body. */
export function detailFrom(body) {
  if (!body) return "";
  if (typeof body === "string") return body.length > 300 || body.trim().startsWith("<") ? "" : body;
  const d = body.detail ?? body.message;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    // FastAPI validation errors: [{loc, msg}] -> "field: message"
    return d.slice(0, 5).map((e) => {
      const loc = Array.isArray(e?.loc) ? e.loc.filter((p) => p !== "body").join(".") : "";
      return loc ? `${loc}: ${e?.msg ?? "invalid"}` : e?.msg ?? "invalid value";
    }).join("; ");
  }
  if (d && typeof d === "object" && typeof d.message === "string") return d.message;
  return "";
}

/** Map a status + detail to a plain-language message (spec §8: safe user-facing messages). */
export function friendlyMessage(status, detail) {
  if (status === 0) return detail || "Can't reach the server. Check that the API is running and try again.";
  if (status === 401) return detail || "Your session has ended. Please sign in again.";
  if (status === 403) return detail || "You don't have access to this.";
  if (status === 404) return detail || "We couldn't find that.";
  if (status === 409) return detail || "This can't be done right now — the item changed or is in the wrong state.";
  if (status === 413) return detail || "That file is too large.";
  if (status === 415) return detail || "That file type isn't supported.";
  if (status === 422) return detail ? `Please check the form: ${detail}` : "Please check the form and try again.";
  if (status === 503) return detail || "The service is temporarily unavailable. Please try again shortly.";
  if (status >= 500) return "Something went wrong on the server. Please try again.";
  return detail || `Request failed (HTTP ${status}).`;
}

async function parseBody(res) {
  const type = res.headers.get("content-type") || "";
  if (res.status === 204) return null;
  if (type.includes("application/json")) {
    try { return await res.json(); } catch { return null; }
  }
  if (type.includes("application/pdf")) return res.blob();
  try { return await res.text(); } catch { return null; }
}

/**
 * request(method, path, {query, json, body, headers, timeoutMs, auth=true, raw=false})
 * Returns parsed JSON (or Blob for PDFs). Throws ApiError.
 */
export async function request(method, path, opts = {}) {
  const { query, json, body, headers = {}, timeoutMs = DEFAULT_TIMEOUT_MS, auth = true } = opts;
  const url = new URL(API_BASE + path);
  if (query) {
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
    }
  }
  const h = { Accept: "application/json", ...headers };
  const token = getToken();
  if (auth && token) h.Authorization = `Bearer ${token}`;
  let payload = body;
  if (json !== undefined) { h["Content-Type"] = "application/json"; payload = JSON.stringify(json); }

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let res;
  try {
    res = await fetch(url, { method, headers: h, body: payload, signal: ctrl.signal });
  } catch (e) {
    const msg = e?.name === "AbortError" ? "The request took too long. Please try again." : undefined;
    throw new ApiError(0, friendlyMessage(0, msg), msg);
  } finally {
    clearTimeout(timer);
  }
  const data = await parseBody(res);
  if (!res.ok) {
    const detail = detailFrom(data);
    if (res.status === 401 && auth && token) {
      setToken(null);
      if (unauthorizedHandler) unauthorizedHandler();
    }
    throw new ApiError(res.status, friendlyMessage(res.status, detail), detail, data);
  }
  return data;
}

export const api = {
  get: (path, query, opts) => request("GET", path, { ...opts, query }),
  post: (path, json, opts) => request("POST", path, { ...opts, json: json ?? {} }),
  put: (path, json, opts) => request("PUT", path, { ...opts, json: json ?? {} }),
  /** Raw binary upload (e.g. vendor quote PDF: Content-Type application/pdf, NOT multipart). */
  postRaw: (path, data, contentType, query, opts) =>
    request("POST", path, { ...opts, query, body: data, headers: { "Content-Type": contentType } }),
  /** Multipart upload (e.g. policy documents). */
  postForm: (path, formData, query, opts) => request("POST", path, { ...opts, query, body: formData }),
};

/** Generate a client idempotency key (approval decisions, payments). */
export function idempotencyKey(prefix = "ui") {
  const rnd = (globalThis.crypto?.randomUUID?.() || Math.random().toString(36).slice(2) + Date.now().toString(36));
  return `${prefix}-${rnd}`.replace(/[^A-Za-z0-9_.:-]/g, "").slice(0, 100);
}
