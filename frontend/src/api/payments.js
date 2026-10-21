/**
 * Payments (Simulation) API — SIMULATION ONLY. No bank or gateway is contacted and no money is transferred.
 * Mirrors app/routers/payments.py. The backend re-checks eligibility on every request.
 */
import { api, idempotencyKey } from "./client";

const BASE = "/api/payments/simulation";
const enc = encodeURIComponent;
const IDEM_PREFIX = "workplaceops.payidem.";

export async function listApprovedTickets() {
  const data = await api.get("/api/admin/tickets", { status: "approved", limit: 100 });
  const rows = Array.isArray(data?.tickets) ? data.tickets : [];
  return rows.filter((t) => t?.ticket_id && t.status === "approved");
}

/** Invoice id from the ticket's stored reconciliation (or a post-approval invoice match). */
export async function ticketInvoice(ticketId) {
  const view = await api.get(`/api/admin/tickets/${enc(ticketId)}`);
  const recon = view?.workflow_result?.reconciliation;
  const fromRecon = recon && typeof recon === "object" ? recon.invoice_id : null;
  const fromMatch = view?.invoice_match && typeof view.invoice_match === "object" ? view.invoice_match.invoice_id : null;
  return fromRecon || fromMatch || null;
}

export const getEligibility = (ticketId, invoiceId) =>
  api.get(`${BASE}/eligibility/${enc(invoiceId)}`, { ticket_id: ticketId });

/** One idempotency key per ticket+invoice intent, kept for the browser session so a re-click replays. */
export function intentKey(ticketId, invoiceId) {
  const slot = `${IDEM_PREFIX}${ticketId}|${invoiceId}`;
  try {
    const existing = sessionStorage.getItem(slot);
    if (existing) return existing;
    const key = idempotencyKey("ui");
    sessionStorage.setItem(slot, key);
    return key;
  } catch {
    return idempotencyKey("ui");
  }
}

/** Create the simulated instruction from server eligibility values (amount is never typed by the user). */
export function createSimulation(eligibility) {
  return api.post(BASE, {
    ticket_id: eligibility.ticket_id,
    invoice_id: eligibility.invoice_id,
    purchase_order_id: eligibility.purchase_order_id,
    amount: eligibility.amount,
    currency: eligibility.currency,
    simulation_only: true,
    confirmed_by_user: true,
    idempotency_key: intentKey(eligibility.ticket_id, eligibility.invoice_id),
  });
}

export async function getPayment(paymentId) {
  const data = await api.get(`${BASE}/${enc(paymentId)}`);
  return data?.payment && typeof data.payment === "object" ? data.payment : data;
}

export async function paymentEvents(paymentId) {
  const data = await api.get(`${BASE}/${enc(paymentId)}/events`);
  const rows = Array.isArray(data) ? data : data?.events;
  return Array.isArray(rows) ? rows.filter((e) => e && typeof e === "object") : [];
}

export const retryPayment = (paymentId) => api.post(`${BASE}/${enc(paymentId)}/retry`, { confirmed_by_user: true });

export async function listPayments() {
  const data = await api.get(BASE);
  const rows = Array.isArray(data) ? data : data?.payments || data?.items || [];
  return rows.map((p) => (p?.payment && typeof p.payment === "object" ? p.payment : p)).filter((p) => p?.payment_id);
}

/** Normalise a payment failure to {error, message, reasons[{code, message}]} (body is {error, message, reasons}). */
export function paymentError(err) {
  const body = err?.body;
  if (body && typeof body === "object" && (body.error || body.message)) {
    const reasons = (Array.isArray(body.reasons) ? body.reasons : [])
      .filter((r) => r && typeof r === "object")
      .map((r) => ({ code: String(r.code || "?"), message: String(r.message || "").slice(0, 300) }));
    return { error: body.error || null, message: String(body.message || err.message || "").slice(0, 500), reasons, status: err.status };
  }
  return { error: null, message: err?.message || "Something went wrong.", reasons: [], status: err?.status };
}
