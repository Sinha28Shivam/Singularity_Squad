import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import DataTable from "../components/DataTable";
import StatusBadge, { statusLabel } from "../components/StatusBadge";
import ConfirmDialog from "../components/ConfirmDialog";
import { EmptyState, ErrorState, LoadingState, Notice } from "../components/States";
import { useApi } from "../lib/useApi";
import { useHealth } from "../lib/health";
import { formatDateTime, formatMoney, humanize } from "../lib/format";
import {
  createSimulation, getEligibility, getPayment, listApprovedTickets, listPayments, paymentError, paymentEvents,
  retryPayment, ticketInvoice,
} from "../api/payments";
import "../styles/pages/payments.css";

const BANNER = "Simulation only — no money is transferred.";
const REPLAY_NOTICE = "Existing simulated payment returned";
const RETRYABLE = new Set(["failed_simulated"]);

function confirmationText(amount, currency) {
  return `You are about to create a simulated payment instruction for ${formatMoney(amount, currency || "INR")} (${currency || "INR"}). ` +
    "This is a demo only. No bank will be contacted and no money will be transferred.";
}

export default function SimulatedPaymentsPage() {
  const health = useHealth();
  const disabled = health && health.payments_simulation_enabled === false;
  return (
    <div className="stack">
      <PageHeader title="Payments (Simulation)"
        description="Create a simulated payment instruction for the reconciled invoice of an approved request." />
      <div className="payments-banner"><Notice tone="warning" title={BANNER} role="note" /></div>
      {disabled ? (
        <Notice tone="info" title="Simulated payments are turned off">
          The payment simulator is not enabled on this server. Nothing can be created here.
        </Notice>
      ) : <PaymentsWorkspace />}
    </div>
  );
}

function PaymentsWorkspace() {
  const tickets = useApi(() => listApprovedTickets(), []);
  const [ticketId, setTicketId] = useState("");
  const [allKey, setAllKey] = useState(0);
  return (
    <>
      <section className="card stack" aria-labelledby="pick-h">
        <h2 id="pick-h" style={{ margin: 0, fontSize: 16 }}>Choose a request</h2>
        {tickets.loading && !tickets.data ? <LoadingState label="Loading approved requests…" /> :
          tickets.error ? <ErrorBlock err={paymentError(tickets.error)} onRetry={tickets.reload} /> :
            !(tickets.data || []).length ? <EmptyState title="No approved requests yet"
              description="A request must be approved by an eligible approver first." /> : (
              <div className="field" style={{ maxWidth: 480 }}>
                <label htmlFor="pay-ticket" className="required">Approved request</label>
                <select id="pay-ticket" className="select" value={ticketId} onChange={(e) => setTicketId(e.target.value)}>
                  <option value="">Select an approved request</option>
                  {tickets.data.map((t) => <option key={t.ticket_id} value={t.ticket_id}>{t.ticket_id}{t.title ? ` · ${t.title}` : ""}</option>)}
                </select>
              </div>
            )}
      </section>
      {ticketId && <TicketPayment key={ticketId} ticketId={ticketId} onCreated={() => setAllKey((k) => k + 1)} />}
      <details className="disclosure">
        <summary>All simulated payments</summary>
        <AllPayments key={allKey} />
      </details>
    </>
  );
}

/** Renders a payment API error body {error, message, reasons[{code, message}]}. */
function ErrorBlock({ err, onRetry, title }) {
  if (!err) return null;
  if (err.error === "SIMULATION_DISABLED") {
    return (
      <Notice tone="info" title="Simulated payments are turned off">
        {err.message || "The payment simulator is not enabled on this server."} Nothing was created.
      </Notice>
    );
  }
  return (
    <div className="notice notice-error" role="alert">
      <div className="stack-sm" style={{ gap: 4 }}>
        <strong>{title || (err.error ? humanize(err.error) : "Request failed")}</strong>
        {err.message && <div>{err.message}</div>}
        {err.reasons.length > 0 && (
          <ul className="reason-codes">
            {err.reasons.map((r, i) => <li key={`${r.code}-${i}`}><code>{r.code}</code> — {r.message}</li>)}
          </ul>
        )}
        {onRetry && <div><button type="button" className="btn btn-sm" onClick={onRetry}>Try again</button></div>}
      </div>
    </div>
  );
}

function ReasonCodes({ err }) {
  if (!err || !err.reasons.length) return null;
  return (
    <ul className="reason-codes" aria-label="Reasons">
      {err.reasons.map((r, i) => <li key={`${r.code}-${i}`}><code>{r.code}</code> — {r.message}</li>)}
    </ul>
  );
}

function TicketPayment({ ticketId, onCreated }) {
  const invoice = useApi(() => ticketInvoice(ticketId), [ticketId]);
  const invoiceId = invoice.data;
  const elig = useApi(() => (invoiceId ? getEligibility(ticketId, invoiceId) : Promise.resolve(null)), [ticketId, invoiceId]);
  const [confirming, setConfirming] = useState(false);
  const [createError, setCreateError] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const cancelConfirm = useCallback(() => setConfirming(false), []);

  if (invoice.loading && invoice.data === null && !invoice.error) return <section className="card"><LoadingState label="Loading invoice…" /></section>;
  if (invoice.error) return <section className="card"><ErrorBlock err={paymentError(invoice.error)} onRetry={invoice.reload} /></section>;
  if (!invoiceId) {
    return (
      <section className="card">
        <Notice tone="warning" title="No reconciled invoice">
          This request has no reconciled invoice yet. Match the vendor invoice against the approved draft PO on the{" "}
          <Link to={`/admin/requests/${encodeURIComponent(ticketId)}`}>request page</Link>, then come back.
        </Notice>
      </section>
    );
  }
  const e = elig.data ? { ...elig.data, ticket_id: elig.data.ticket_id || ticketId, invoice_id: elig.data.invoice_id || invoiceId } : null;
  const reasons = Array.isArray(e?.reasons) ? e.reasons : [];

  return (
    <>
      <section className="card stack" aria-labelledby="elig-h">
        <div className="row-between">
          <h2 id="elig-h" style={{ margin: 0, fontSize: 16 }}>Eligibility check</h2>
          {e && <span className={`badge ${e.eligible ? "badge-success" : "badge-error"}`}>{e.eligible ? "Eligible" : "Not eligible"}</span>}
        </div>
        {elig.loading && !elig.data ? <LoadingState label="Checking eligibility…" /> :
          elig.error ? <ErrorBlock err={paymentError(elig.error)} onRetry={elig.reload} title="Eligibility could not be checked" /> : e && (
            <>
              <div className="stat">
                <span className="stat-label">Simulated amount (from the reconciled records)</span>
                <span className="payment-amount">{e.amount ? formatMoney(e.amount, e.currency || "INR") : "—"}</span>
                <span className="muted small">{e.currency || "—"}</span>
              </div>
              <dl className="dl">
                <dt>Invoice</dt><dd className="mono">{e.invoice_id}</dd>
                <dt>Draft PO</dt><dd className="mono">{e.purchase_order_id || "—"}</dd>
                <dt>Invoice / PO match</dt><dd>{humanize(e.invoice_match_status)}</dd>
                <dt>Approval</dt><dd>{e.approval_id ? `${e.approval_id} · ${humanize(e.approval_status)}${e.approved_by ? ` by ${e.approved_by}` : ""}` : "—"}</dd>
                <dt>Checked</dt><dd>{formatDateTime(e.checked_at)}</dd>
              </dl>
              {!e.eligible && (
                <div className="notice notice-error" role="alert">
                  <div className="stack-sm" style={{ gap: 4 }}>
                    <strong>Resolve these before simulating:</strong>
                    {reasons.length ? (
                      <ul className="reason-codes">
                        {reasons.map((r, i) => <li key={`${r.code}-${i}`}><code>{r.code}</code> — {r.message}</li>)}
                      </ul>
                    ) : <span>No reason was supplied by the server.</span>}
                  </div>
                </div>
              )}
              <div className="row">
                {e.eligible && (
                  <button type="button" className="btn btn-primary" onClick={() => { setCreateError(null); setConfirming(true); }}>
                    Create simulated payment instruction
                  </button>
                )}
                {e.existing_payment_id && !receipt && (
                  <button type="button" className="btn" onClick={() => setReceipt({ paymentId: e.existing_payment_id, replayed: false })}>
                    View existing simulated payment
                  </button>
                )}
              </div>
            </>
          )}
        {!confirming && <ErrorBlock err={createError} title="The simulation was not created" />}
      </section>
      {receipt && <Receipt key={receipt.paymentId} paymentId={receipt.paymentId} initial={receipt.payment} replayed={receipt.replayed}
        onChanged={() => { elig.reload(); onCreated(); }} />}
      <ConfirmDialog open={confirming} title="Create a simulated payment instruction?"
        description={e ? confirmationText(e.amount, e.currency) : ""}
        confirmLabel="Create simulation" onCancel={cancelConfirm}
        onConfirm={async () => {
          setCreateError(null);
          let out;
          try {
            out = await createSimulation(e);
          } catch (err) {
            setCreateError(paymentError(err));
            throw err;
          }
          const p = out?.payment;
          setConfirming(false);
          if (!p?.payment_id) { setCreateError({ error: null, message: "Unexpected response from the server.", reasons: [] }); return; }
          setReceipt({ paymentId: p.payment_id, payment: p, replayed: Boolean(out.replayed) });
          elig.reload();
          onCreated();
        }}>
        <ReasonCodes err={confirming ? createError : null} />
      </ConfirmDialog>
    </>
  );
}

function Receipt({ paymentId, initial, replayed, onChanged }) {
  const pay = useApi(() => getPayment(paymentId), [paymentId]);
  const events = useApi(() => paymentEvents(paymentId), [paymentId]);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState(null);
  const [retried, setRetried] = useState(false);
  const cancelRetry = useCallback(() => setRetrying(false), []);
  const p = pay.data && pay.data.payment_id ? pay.data : initial;
  useEffect(() => { setRetryError(null); }, [paymentId]);

  if (!p && pay.loading) return <section className="card"><LoadingState label="Loading simulation receipt…" /></section>;
  if (!p && pay.error) return <section className="card"><ErrorBlock err={paymentError(pay.error)} onRetry={pay.reload} /></section>;
  if (!p) return null;
  const evs = [...(events.data || [])].sort((a, b) => String(a.at || "").localeCompare(String(b.at || "")));

  return (
    <section className="card stack" aria-labelledby="receipt-h">
      <div className="row-between">
        <h2 id="receipt-h" style={{ margin: 0, fontSize: 16 }}>Simulation receipt</h2>
        <StatusBadge kind="payment" status={p.status} />
      </div>
      {replayed && <Notice tone="info" title={REPLAY_NOTICE}>The same request was already submitted, so the existing simulated instruction is shown. Nothing new was created.</Notice>}
      {retried && <Notice tone="info">Retry submitted to the simulator. Status reloaded from the server.</Notice>}
      <Notice tone="warning">{BANNER} No bank was contacted.</Notice>
      <dl className="dl">
        <dt>Simulator status</dt><dd>{statusLabel(p.status, "payment")}{p.failure_code ? ` · ${humanize(p.failure_code)}` : ""}</dd>
        <dt>Simulation reference</dt><dd className="mono">{p.provider_reference || "None issued"}</dd>
        <dt>Simulation ID</dt><dd className="mono">{p.payment_id}</dd>
        <dt>Amount</dt><dd>{formatMoney(p.amount, p.currency || "INR")} ({p.currency || "—"})</dd>
        <dt>Method</dt><dd>{p.method ? humanize(p.method) : "Simulated"}</dd>
        <dt>Request / invoice</dt><dd className="mono">{p.ticket_id} · {p.invoice_id}</dd>
        <dt>Draft PO</dt><dd className="mono">{p.purchase_order_id}</dd>
        <dt>Attempts</dt><dd>{p.attempts ?? 1}</dd>
        <dt>Correlation</dt><dd className="mono">{p.correlation_id || "—"}</dd>
        <dt>Created</dt><dd>{p.created_by || "—"} · {formatDateTime(p.created_at)}</dd>
        <dt>Money transferred</dt><dd>No — simulation only</dd>
      </dl>
      {p.audit_complete === false && <Notice tone="warning">The audit trail for this simulation is incomplete. An admin should reconcile it.</Notice>}
      <div className="stack-sm">
        <h3 className="label">Events</h3>
        {events.loading && !events.data ? <LoadingState /> : events.error ? <ErrorBlock err={paymentError(events.error)} onRetry={events.reload} /> :
          !evs.length ? <p className="muted small">No events recorded yet.</p> : (
            <ol className="payment-timeline">
              {evs.map((ev) => (
                <li key={ev.event_id}>
                  <strong>{humanize(ev.event_type)}</strong>
                  {ev.status && <span> · {statusLabel(ev.status, "payment")}</span>}
                  <span className="muted"> · {ev.actor || "—"} · {formatDateTime(ev.at)}</span>
                  {(ev.reason_codes || []).length > 0 && <div className="muted small">{ev.reason_codes.join(", ")}</div>}
                  {ev.detail && <div className="muted small">{ev.detail}</div>}
                </li>
              ))}
            </ol>
          )}
      </div>
      {!retrying && <ErrorBlock err={retryError} title="Retry failed" />}
      {RETRYABLE.has(p.status) && (
        <div><button type="button" className="btn" onClick={() => { setRetryError(null); setRetrying(true); }}>Retry simulation</button></div>
      )}
      <ConfirmDialog open={retrying} title="Retry this simulation?"
        description="Re-submits the same simulated instruction (same simulation ID). No money is moved."
        confirmLabel="Retry simulation" onCancel={cancelRetry}
        onConfirm={async () => {
          setRetryError(null);
          let out;
          try {
            out = await retryPayment(p.payment_id);
          } catch (err) {
            setRetryError(paymentError(err));
            throw err;
          }
          setRetrying(false);
          if (out?.payment?.payment_id) pay.setData(out.payment);
          setRetried(true);
          pay.reload(); events.reload(); onChanged?.();
        }}>
        <ReasonCodes err={retrying ? retryError : null} />
      </ConfirmDialog>
    </section>
  );
}

function AllPayments() {
  const { data, error, loading, reload } = useApi(() => listPayments(), []);
  return (
    <DataTable caption="Simulated payments" rows={data || []} rowKey="payment_id" loading={loading}
      error={error} onRetry={reload} empty={<p className="muted small">No simulated payments recorded yet.</p>}
      columns={[
        { key: "payment_id", header: "Simulation", render: (p) => <span className="mono">{p.payment_id}</span> },
        { key: "ticket_id", header: "Request", render: (p) => <span className="mono">{p.ticket_id}</span> },
        { key: "invoice_id", header: "Invoice", render: (p) => <span className="mono">{p.invoice_id}</span> },
        { key: "amount", header: "Amount", align: "right", render: (p) => formatMoney(p.amount, p.currency || "INR") },
        { key: "status", header: "Status", render: (p) => <StatusBadge kind="payment" status={p.status} /> },
        { key: "created_at", header: "Created", render: (p) => formatDateTime(p.created_at) },
      ]} />
  );
}
