/**
 * Request (ticket) API + shared request vocabulary.
 * Mirrors ui/pages_portal/user_portal.py and admin_console.py. The backend is authoritative for every
 * status transition; the constants here only shape what the UI offers.
 */
import { api, ApiError } from "./client";

const enc = (id) => encodeURIComponent(String(id));

// ---------- requester ----------
/**
 * GET /api/my/tickets?q=&status=&limit=&offset= -> {tickets, total, next_offset}.
 * Accepts a number (legacy: limit) or {q, status, limit, offset}. Older servers ignore q/status/offset and
 * omit `total`; callers detect that and fall back to filtering the loaded list.
 */
export const listMyTickets = (opts = {}) => {
  const { q, status, limit = 50, offset } = typeof opts === "number" ? { limit: opts } : opts;
  return api.get("/api/my/tickets", { q: q || undefined, status: status || undefined, limit, offset: offset || undefined });
};
/** Max length of the server-side ticket search query (backend Query max_length). */
export const TICKET_QUERY_MAX = 120;
/** True when a ticket list response came from a server that supports q/status/offset paging. */
export const supportsServerSearch = (data) => Boolean(data) && typeof data.total === "number";
export const getMyTicket = (id) => api.get(`/api/my/tickets/${enc(id)}`);
export const createMyTicket = (submission) => api.post("/api/my/tickets", submission);
export const updateMyTicket = (id, submission) => api.put(`/api/my/tickets/${enc(id)}`, submission);
export const addMyNote = (id, text) => api.post(`/api/my/tickets/${enc(id)}/notes`, { text });

// ---------- admin ----------
/** GET /api/admin/tickets?q=&status=&limit=&offset= -> {tickets, counts (global by status), total, next_offset}. */
export const listAdminTickets = ({ q, status, limit = 100, offset } = {}) =>
  api.get("/api/admin/tickets", { q: q || undefined, status, limit, offset: offset || undefined });
export const getAdminTicket = (id) => api.get(`/api/admin/tickets/${enc(id)}`);
export const runAdminTicket = (id, { invoiceId, today } = {}) =>
  api.post(`/api/admin/tickets/${enc(id)}/run`, { invoice_id: invoiceId || null, today: today || null });
export const rejectAdminTicket = (id, note) =>
  api.post(`/api/admin/tickets/${enc(id)}/decision`, { decision: "reject", note: (note || "").trim() || null });
export const addAdminNote = (id, text) => api.post(`/api/admin/tickets/${enc(id)}/notes`, { text });
export const getAdminAudit = (id, limit = 500) => api.get(`/api/admin/tickets/${enc(id)}/audit`, { limit });
export const matchInvoice = (id, invoiceId) =>
  api.post(`/api/admin/tickets/${enc(id)}/invoice-match`, { invoice_id: invoiceId });
export const reconcileApproval = (approvalId) => api.post(`/api/admin/approvals/${enc(approvalId)}/reconcile`);
/** POST /api/reconcile {po, invoice_id} -> ReconciliationReport. Ad-hoc preview: nothing is stored on the ticket. */
export const previewInvoiceCheck = (po, invoiceId) => api.post("/api/reconcile", { po, invoice_id: invoiceId }, { timeoutMs: 60000 });

// ---------- shared ----------
export const listInvoices = async () => {
  const data = await api.get("/api/invoices");
  return Array.isArray(data) ? data.filter((i) => i && i.invoice_id) : [];
};

/** GET /api/tickets/{id}/approval. A 404 means "no approval request" and resolves to null. */
export async function getTicketApproval(id) {
  try {
    return await api.get(`/api/tickets/${enc(id)}/approval`);
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

// ---------- vocabulary (enums from openapi TicketSubmission) ----------
export const EVENT_CATEGORIES = [
  ["team_lunch", "Team lunch"],
  ["offsite", "Team offsite"],
  ["celebration", "Celebration / party"],
  ["training_workshop", "Training / workshop"],
  ["town_hall", "Town hall / all-hands"],
  ["client_visit", "Client visit"],
  ["other", "Other event"],
];
export const SERVICES = [["lunch", "Lunch"], ["breakfast", "Breakfast"], ["dinner", "Dinner"], ["snacks", "Snacks"]];
export const categoryLabel = (k) => (EVENT_CATEGORIES.find(([v]) => v === k) || [null, k || "—"])[1];
export const serviceLabel = (k) => (SERVICES.find(([v]) => v === k) || [null, k || "—"])[1];

export const TICKET_STATUSES = [
  "submitted", "processing", "reviewed", "needs_info", "pending_approval", "changes_requested",
  "approval_expired", "approved", "rejected", "failed", "persistence_error",
];
/** app/portal_schemas.py USER_EDITABLE_STATUSES */
export const USER_EDITABLE_STATUSES = new Set(["submitted", "needs_info", "changes_requested", "approval_expired"]);
/** Statuses the server refuses to run (admin_console.py NOT_RUNNABLE). The server still decides (409). */
export const NOT_RUNNABLE_STATUSES = new Set(["approved", "rejected", "processing", "changes_requested"]);
export const APPROVAL_TICKET_STATUSES = new Set(["pending_approval", "changes_requested", "approval_expired", "approved", "rejected"]);
export const ATTENTION_STATUSES = new Set(["submitted", "reviewed", "approval_expired", "failed", "persistence_error"]);

const USER_NEXT = {
  submitted: "Nothing yet — an admin will review it. You can still edit it.",
  processing: "Nothing — your request is being checked.",
  reviewed: "Nothing — an admin is reviewing it.",
  needs_info: "Answer the questions and resubmit.",
  pending_approval: "Nothing — waiting for an approver.",
  changes_requested: "Update the request and resubmit.",
  approval_expired: "Update and resubmit, or wait for an admin to re-run it.",
  approved: "No action needed.",
  rejected: "No action needed — see the reason.",
  failed: "Nothing — an admin will retry.",
  persistence_error: "Nothing — an admin will retry.",
};
const ADMIN_NEXT = {
  submitted: "Run the workflow",
  processing: "Wait for the run to finish",
  reviewed: "Review the result; close if no approval is needed",
  needs_info: "Waiting for the requester",
  pending_approval: "Waiting for an approver",
  changes_requested: "Waiting for the requester to resubmit",
  approval_expired: "Re-run the workflow",
  approved: "Match the invoice if one arrives",
  rejected: "Closed",
  failed: "Re-run the workflow",
  persistence_error: "Re-run the workflow",
};
export const userNextAction = (s) => USER_NEXT[s] || "Check the request for details.";
export const adminNextAction = (s) => ADMIN_NEXT[s] || "Open the request";

// ---------- form helpers ----------
export const EMPTY_FORM = {
  title: "", event_category: "team_lunch", service: "lunch", event_date: "", attendees: "", budget_inr: "",
  location: "", approved_vendor_only: true, notes: "",
};

export function formFromSubmission(sub) {
  if (!sub) return { ...EMPTY_FORM };
  return {
    title: sub.title ?? "",
    event_category: sub.event_category || "team_lunch",
    service: sub.service || "lunch",
    event_date: sub.event_date ? String(sub.event_date).slice(0, 10) : "",
    attendees: sub.attendees ?? "",
    budget_inr: sub.budget_inr ?? "",
    location: sub.location ?? "",
    approved_vendor_only: sub.approved_vendor_only !== false,
    notes: sub.notes ?? "",
  };
}

export function todayIso(d = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** Client-side checks mirroring TicketSubmission; the backend remains authoritative. Returns {field: message}. */
export function validateForm(f, { today = todayIso() } = {}) {
  const e = {};
  const title = String(f.title || "").trim();
  if (title.length < 3) e.title = "Enter a short summary of at least 3 characters.";
  else if (title.length > 120) e.title = "Keep the summary under 120 characters.";
  if (!EVENT_CATEGORIES.some(([v]) => v === f.event_category)) e.event_category = "Choose an event type.";
  if (!SERVICES.some(([v]) => v === f.service)) e.service = "Choose a catering service.";
  if (!f.event_date) e.event_date = "Choose the event date.";
  else if (!/^\d{4}-\d{2}-\d{2}$/.test(f.event_date) || Number.isNaN(new Date(f.event_date).getTime())) e.event_date = "Enter a valid date.";
  else if (f.event_date < today) e.event_date = "The event date can't be in the past.";
  const att = Number(f.attendees);
  if (f.attendees === "" || f.attendees === null) e.attendees = "Enter the number of attendees.";
  else if (!Number.isInteger(att) || att < 1 || att > 5000) e.attendees = "Attendees must be a whole number from 1 to 5,000.";
  const bud = Number(f.budget_inr);
  if (f.budget_inr === "" || f.budget_inr === null) e.budget_inr = "Enter the budget.";
  else if (!Number.isFinite(bud) || bud <= 0) e.budget_inr = "Budget must be greater than zero.";
  else if (bud > 10000000) e.budget_inr = "Budget can't exceed ₹1,00,00,000.";
  if (String(f.location || "").length > 120) e.location = "Keep the location under 120 characters.";
  if (String(f.notes || "").length > 2000) e.notes = "Keep notes under 2,000 characters.";
  return e;
}

export function toSubmission(f) {
  return {
    title: String(f.title).trim(),
    event_category: f.event_category,
    service: f.service,
    event_date: f.event_date,
    attendees: Number(f.attendees),
    budget_inr: Number(f.budget_inr),
    location: String(f.location || "").trim() || null,
    approved_vendor_only: Boolean(f.approved_vendor_only),
    notes: String(f.notes || "").trim() || null,
  };
}
