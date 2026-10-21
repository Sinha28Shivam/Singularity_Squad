/**
 * Vendor sourcing API (admin only). Decision support only: nothing here contacts a supplier or awards work.
 * Mirrors app/routers/vendor_sourcing.py + vendor_candidates.py.
 */
import { api } from "./client";

const BASE = "/api/vendor-sourcing";
const enc = encodeURIComponent;
const rfqPath = (rfqId) => `${BASE}/rfqs/${enc(rfqId)}`;
const quotePath = (rfqId, quoteId) => `${rfqPath(rfqId)}/quotes/${enc(quoteId)}`;

export const CANDIDATE_STATUSES = ["pending_review", "approved", "rejected", "suspended"];
export const RFQ_STATUSES = ["open", "closed", "cancelled"];
export const COMPLIANCE_STATUSES = ["verified", "declared", "missing"];
/** Decisions the backend allows per candidate status (repository.ALLOWED_TRANSITIONS). */
export const CANDIDATE_TRANSITIONS = {
  pending_review: ["approve", "reject", "suspend"],
  approved: ["suspend"],
  rejected: [],
  suspended: ["approve"],
};
/**
 * Decisions to offer for a candidate: the server's `allowed_decisions` when the response includes it
 * (single source of truth), otherwise the local copy of the transition table. The server re-validates.
 */
export function allowedDecisions(candidate) {
  if (candidate && Array.isArray(candidate.allowed_decisions)) {
    return candidate.allowed_decisions.filter((d) => typeof d === "string");
  }
  return CANDIDATE_TRANSITIONS[candidate?.status] || [];
}
export const MAX_PDF_BYTES = 5 * 1024 * 1024;
export const MAX_PDF_PAGES = 20;
export const MIN_REASON = 5;

// ---------------------------------------------------------------- directory / candidates
export const searchVendors = ({ q, service, status, limit = 100, offset = 0 } = {}) =>
  api.get(`${BASE}/vendors`, { q, service, status, limit, offset });

export const listCandidates = ({ q, service, status, limit = 20, offset = 0 } = {}) =>
  api.get(`${BASE}/candidates`, { q, service, status, limit, offset });

export const getCandidate = (id) => api.get(`${BASE}/candidates/${enc(id)}`);

export async function candidateEvents(id) {
  const data = await api.get(`${BASE}/candidates/${enc(id)}/events`);
  return Array.isArray(data?.events) ? data.events : [];
}

export const createCandidate = (body) => api.post(`${BASE}/candidates`, body);

export const decideCandidate = (id, decision, reason) =>
  api.post(`${BASE}/candidates/${enc(id)}/decision`, { decision, reason });

// ---------------------------------------------------------------- RFQs
export const listRfqs = ({ status, request_id, limit = 50, offset = 0 } = {}) =>
  api.get(`${BASE}/rfqs`, { status, request_id, limit, offset });

export const createRfq = (body) => api.post(`${BASE}/rfqs`, body);

/** Returns {rfq, quotes, events, pending_events, notice}. */
export const getRfq = (rfqId) => api.get(rfqPath(rfqId));

export const closeRfq = (rfqId, reason) => api.post(`${rfqPath(rfqId)}/close`, { reason });
export const cancelRfq = (rfqId, reason) => api.post(`${rfqPath(rfqId)}/cancel`, { reason });

// ---------------------------------------------------------------- quotes
export const submitQuote = (rfqId, body) => api.post(`${rfqPath(rfqId)}/quotes`, body);
export const reviseQuote = (rfqId, quoteId, body) => api.post(`${quotePath(rfqId, quoteId)}/revise`, body);
export const verifyQuote = (rfqId, quoteId, body) => api.post(`${quotePath(rfqId, quoteId)}/verify`, body);

/** Client-side PDF checks (the server re-checks signature, encryption and page count). Returns an error or "". */
export function pdfFileProblem(file) {
  if (!file) return "Choose a PDF file.";
  const isPdf = file.type === "application/pdf" || (!file.type && /\.pdf$/i.test(file.name || ""));
  if (!isPdf) return "Only PDF files are accepted.";
  if (!file.size) return "The file is empty.";
  if (file.size > MAX_PDF_BYTES) return "The PDF is larger than 5 MiB.";
  return "";
}

/** Raw application/pdf body (NOT multipart); vendor/reference/filename travel in the query string. */
export const uploadPdfQuote = (rfqId, bytes, { vendor_id, quote_reference, filename = "quotation.pdf" }) =>
  api.postRaw(`${rfqPath(rfqId)}/quotes/pdf`, bytes, "application/pdf",
    { vendor_id, quote_reference, filename }, { timeoutMs: 120000 });

/** Authenticated download of the stored original PDF. Returns a Blob. */
export const downloadQuotePdf = (rfqId, quoteId) =>
  api.get(`${quotePath(rfqId, quoteId)}/pdf`, undefined, { headers: { Accept: "application/pdf" } });

// ---------------------------------------------------------------- comparison
export const compareQuotes = (rfqId) => api.post(`${rfqPath(rfqId)}/compare`, {}, { timeoutMs: 60000 });

/** GET .../comparisons/{id} -> one stored comparison incl. per-row score {total, components, weights, raw}. */
export const getComparison = (rfqId, comparisonId) => api.get(`${rfqPath(rfqId)}/comparisons/${enc(comparisonId)}`);

/** GET /api/vendor-sourcing/metrics -> {counters: {name: {total, by_label}}, scope, note}. In-process; resets on restart. */
export const getSourcingMetrics = () => api.get(`${BASE}/metrics`);

export async function listComparisons(rfqId) {
  const data = await api.get(`${rfqPath(rfqId)}/comparisons`);
  return Array.isArray(data?.items) ? data.items : [];
}

export function decideComparison(rfqId, comparisonId, decision, quoteId, reason) {
  const body = { decision, reason };
  if (decision === "accept") body.quote_id = quoteId;
  return api.post(`${rfqPath(rfqId)}/comparisons/${enc(comparisonId)}/decision`, body);
}

// ---------------------------------------------------------------- source tickets (validated, for RFQ prefill)
export const VALID_TICKET_RESULTS = ["pass", "warning"];

export async function listValidatedTickets() {
  const data = await api.get("/api/admin/tickets", { limit: 100 });
  const rows = Array.isArray(data?.tickets) ? data.tickets : [];
  return rows.filter((t) => t?.ticket_id && VALID_TICKET_RESULTS.includes(t.final_status));
}

/**
 * Load the ticket's validated request snapshot. Returns {request} or {problem}.
 * Mirrors the backend RFQ source-ticket checks so the form is prefilled only from valid data.
 */
export async function loadTicketSnapshot(ticketId) {
  const view = await api.get(`/api/admin/tickets/${enc(ticketId)}`);
  const wr = view?.workflow_result;
  if (!wr || !VALID_TICKET_RESULTS.includes(wr.final_status)) {
    return { problem: "This request has no completed pass/warning workflow result." };
  }
  const req = wr.request;
  if (!req || typeof req !== "object") return { problem: "This request has no validated request snapshot." };
  const missing = ["service", "attendees", "budget_limit", "date"].filter((f) => req[f] === null || req[f] === undefined || req[f] === "");
  const serverMissing = Array.isArray(wr.missing_fields) ? wr.missing_fields : [];
  if (missing.length || serverMissing.length) {
    return { problem: `The validated request is missing: ${(missing.length ? missing : serverMissing).join(", ")}.` };
  }
  return { request: req, ticket: view };
}

/** Approved recipients only: approved internal catalog vendors + approved candidates. */
export async function listApprovedRecipients() {
  const data = await searchVendors({ status: "approved", limit: 200 });
  const out = [];
  for (const v of data?.internal_vendors || []) {
    if (v?.approved && v.vendor_id) out.push({ id: v.vendor_id, name: v.name, kind: "Internal catalog" });
  }
  for (const c of data?.candidates || []) {
    if (c?.status === "approved" && c.candidate_id) out.push({ id: c.candidate_id, name: c.name, kind: "Approved candidate" });
  }
  return out;
}

/** "2026-10-22T17:00" (local) -> "2026-10-22T17:00:00+05:30" using the browser's offset for that date. */
export function localToIsoWithOffset(local) {
  if (!local || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(local)) return "";
  const d = new Date(local);
  if (Number.isNaN(d.getTime())) return "";
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const abs = Math.abs(off);
  const pad = (n) => String(n).padStart(2, "0");
  return `${local.slice(0, 16)}:00${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`;
}

export function localOffsetLabel(local) {
  const iso = localToIsoWithOffset(local);
  return iso ? `UTC${iso.slice(-6)}` : "";
}
